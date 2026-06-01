"""
Dashboard API con WebSockets, autenticación opcional, y endpoints completos.
Ejecutar con: python -m src.api
"""
import os
import io
import csv
import json
import logging
import asyncio
import socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Header, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, PlainTextResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from src.config import BASE_DIR, CRYPTO_SYMBOLS, STOCK_SYMBOLS, TIMEFRAME, LOOP_INTERVAL_SECONDS
from src.database import get_session, Trade, Signal as DbSignal, Candle
from src import analyst, ml_analyst
from src.bot import run_bot_forever  # importar aquí para que ccxt/Alpaca se inicialicen antes del lifespan

logger = logging.getLogger("api")

# ── Rate limiting ─────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

# Executor dedicado para price refresh — 1 thread máximo para que los threads
# zombie de Alpaca no saturen el thread pool compartido del event loop.
_price_refresh_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="price-refresh")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Arranca las tareas de background al iniciar la app (reemplaza @on_event).

    El bot corre como asyncio.to_thread dentro de este proceso (no como proceso
    separado) para ahorrar ~150-200MB de RAM: pandas/numpy/ccxt/sqlalchemy se
    cargan una sola vez en lugar de dos veces.
    """
    asyncio.create_task(watch_log())
    asyncio.create_task(watch_data())
    asyncio.create_task(push_heartbeat())
    asyncio.create_task(refresh_prices_loop())  # popula cache de stocks en bg
    asyncio.create_task(_run_bot_in_thread())    # bot loop — mismo proceso
    yield


async def _run_bot_in_thread():
    """Ejecuta el bot loop en un thread separado para no bloquear el event loop.
    El bot usa time.sleep() y llama APIs síncronas → debe correr en thread.
    Al correr en el mismo proceso que uvicorn ahorramos ~150MB de RAM porque
    pandas/numpy/ccxt/sqlalchemy se cargan una sola vez."""
    _log = logging.getLogger("api.bot_launcher")
    _log.info("Lanzando bot loop en thread pool (single-process mode)...")
    try:
        await asyncio.to_thread(run_bot_forever)
    except Exception as e:
        _log.critical("Bot loop terminó con error: %s", e, exc_info=True)


async def refresh_prices_loop():
    """Refresca el cache de precios de stocks cada 30s en background.
    No bloquea el event loop (corre en thread pool). Permite que las
    requests al WS/REST sean instantáneas usando el cache.
    Usa backoff exponencial (hasta 10 min) cuando Alpaca falla repetidamente."""
    # Llenar el cache inmediatamente al arrancar
    try:
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(loop.run_in_executor(_price_refresh_executor, _refresh_price_cache_sync), timeout=15.0)
        logger.info("Stock price cache initialized (%d symbols)", len(_price_cache))
    except Exception as e:
        logger.warning("Initial price cache refresh failed: %s", e)

    consecutive_failures = 0
    while True:
        # Backoff: 30s → 60s → 120s → 240s → cap 600s
        delay = min(30 * (2 ** consecutive_failures), 600)
        await asyncio.sleep(delay)
        try:
            loop = asyncio.get_event_loop()
            await asyncio.wait_for(loop.run_in_executor(_price_refresh_executor, _refresh_price_cache_sync), timeout=15.0)
            if consecutive_failures > 0:
                logger.info("price refresh OK — resetting backoff")
            consecutive_failures = 0
        except asyncio.TimeoutError:
            consecutive_failures += 1
            logger.warning("price refresh timed out (>15s) — backoff %ds", min(30 * (2 ** consecutive_failures), 600))
        except Exception as e:
            consecutive_failures += 1
            logger.warning("price refresh loop error (backoff %ds): %s", min(30 * (2 ** consecutive_failures), 600), e)


app = FastAPI(title="Trading Bot Dashboard", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

STATIC_DIR = BASE_DIR / "static"
LOG_FILE   = BASE_DIR / "logs" / "bot.log"
PAUSE_FILE = BASE_DIR / "data" / ".paused"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── Auth (obligatoria vía DASHBOARD_TOKEN env var) ────────────────────────────
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
ALLOW_NO_AUTH   = os.getenv("DASHBOARD_ALLOW_NO_AUTH", "").lower() == "true"

if not DASHBOARD_TOKEN and not ALLOW_NO_AUTH:
    logger.warning(
        "⚠ DASHBOARD_TOKEN no está set. Endpoints públicos sin auth. "
        "Setea DASHBOARD_TOKEN o DASHBOARD_ALLOW_NO_AUTH=true explícitamente."
    )

# Origins permitidos para WebSocket (anti CSWSH)
_default_origins = "https://tomas-bot-trading.fly.dev,http://localhost:8000,http://127.0.0.1:8000"
ALLOWED_ORIGINS = {o.strip() for o in os.getenv("DASHBOARD_ALLOWED_ORIGINS", _default_origins).split(",") if o.strip()}


def _check_auth(token: str | None):
    """Valida el token contra DASHBOARD_TOKEN. Si no hay token configurado y
    DASHBOARD_ALLOW_NO_AUTH=true, deja pasar (solo para dev local)."""
    if not DASHBOARD_TOKEN:
        if ALLOW_NO_AUTH:
            return
        raise HTTPException(status_code=503, detail="Auth no configurada — set DASHBOARD_TOKEN")
    # Soporta tanto "Bearer X" como "X" directo
    if token and token.lower().startswith("bearer "):
        token = token[7:].strip()
    if token != DASHBOARD_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")


def _check_origin(origin: str | None, host: str | None = None) -> bool:
    """Valida que el Origin del WebSocket esté en la allowlist O coincida con el Host.
    Si no hay Origin (cliente no-browser), permitir cuando hay token (defensa en
    profundidad: el token es el auth real, Origin es belt-and-suspenders)."""
    if not origin:
        # Sin Origin: requerir que el endpoint imponga el token (lo hace abajo)
        return True
    if origin in ALLOWED_ORIGINS:
        return True
    # Auto-permitir same-origin: si Host=tomas-bot-trading.fly.dev y Origin=https://tomas-bot-trading.fly.dev
    if host:
        same_origin_https = f"https://{host}"
        same_origin_http  = f"http://{host}"
        if origin == same_origin_https or origin == same_origin_http:
            return True
    return False


# ── WebSocket manager ─────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, msg: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# ── Watchers en background ────────────────────────────────────────────────────

async def watch_log():
    """Tail bot.log y broadcast cada línea nueva.

    Usa inode tracking para detectar rotaciones de log correctamente:
    - Truncamiento (size < pos) → reset posición
    - Archivo reemplazado (inode distinto) → reabrir desde 0
    Esto evita que al rotar el log el watcher siga leyendo el archivo viejo.
    """
    def _stat():
        try:
            s = LOG_FILE.stat()
            return s.st_size, s.st_ino
        except OSError:
            return 0, None

    pos, inode = _stat()

    while True:
        await asyncio.sleep(1)
        try:
            if not LOG_FILE.exists():
                continue
            size, new_inode = _stat()

            # Rotación detectada: inode cambió O archivo fue truncado
            if new_inode != inode or size < pos:
                pos   = 0
                inode = new_inode

            if size <= pos:
                continue

            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                new_lines = f.read()
            pos = size
            for line in new_lines.splitlines():
                line = line.strip()
                if line:
                    await manager.broadcast({"type": "log", "line": line})
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("watch_log error: %s", e)
            await asyncio.sleep(5)


def _cycles_file_size() -> int:
    """Retorna el tamaño en bytes del archivo cycles.jsonl (0 si no existe).
    Mucho más barato que load_cycles() — solo stat() del filesystem."""
    try:
        return analyst.CYCLES_LOG.stat().st_size
    except OSError:
        return 0


def _count_cycles_cheap() -> tuple[int, str | None]:
    """Cuenta líneas y lee el último ts de cycles.jsonl sin cargar todo en RAM.
    Lee el archivo de atrás hacia adelante (máx 512 bytes) para el último ts,
    y cuenta líneas con un buffer pequeño. O(n) en líneas pero O(1) en RAM."""
    if not analyst.CYCLES_LOG.exists():
        return 0, None
    count = 0
    last_ts = None
    try:
        with open(analyst.CYCLES_LOG, "rb") as f:
            # Contar líneas con buffer de 64KB (evita cargar 8MB+ en RAM)
            buf_size = 65536
            buf = f.read(buf_size)
            while buf:
                count += buf.count(b"\n")
                buf = f.read(buf_size)
            # Leer último ts: buscar la última línea no vacía
            f.seek(0, 2)
            fsize = f.tell()
            tail = min(512, fsize)
            f.seek(fsize - tail)
            last_bytes = f.read(tail).decode("utf-8", errors="ignore")
            last_line = next((l for l in reversed(last_bytes.splitlines()) if l.strip()), None)
            if last_line:
                last_ts = json.loads(last_line).get("ts")
    except Exception:
        pass
    return count, last_ts


async def watch_data():
    """Cada 5s actualiza estado, señales, trades."""
    last_cycle_size = 0
    last_trade_count = 0
    consecutive_errors = 0
    while True:
        await asyncio.sleep(5)
        try:
            cycle_size = _cycles_file_size()
            if cycle_size != last_cycle_size:
                last_cycle_size = cycle_size
                # to_thread evita bloquear el event loop (12+ Alpaca API calls)
                signals = await asyncio.to_thread(_build_signals)
                status  = await asyncio.to_thread(_build_status)
                await manager.broadcast({"type": "signals", "data": signals})
                await manager.broadcast({"type": "status",  "data": status})

            with get_session() as s:
                trade_count = s.query(Trade).count()
            if trade_count != last_trade_count:
                last_trade_count = trade_count
                trades = await asyncio.to_thread(_build_trades)
                await manager.broadcast({"type": "trades", "data": trades})

            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            logger.error("watch_data error (#%d): %s", consecutive_errors, e, exc_info=True)
            await asyncio.sleep(min(60, 5 * consecutive_errors))


async def push_heartbeat():
    """Heartbeat cada 10s. Si hay posiciones abiertas, también empuja status
    con precios live (Alpaca latest_trade) para que MTM/PnL actualicen sin
    esperar al próximo ciclo del bot (10 min)."""
    iter_count = 0
    while True:
        await asyncio.sleep(10)
        iter_count += 1
        try:
            await manager.broadcast({
                "type": "heartbeat",
                "ts": datetime.now(timezone.utc).isoformat(),
                "paused": PAUSE_FILE.exists(),
            })
            # Cada 30s (3 iter) push status si hay open trades — refresca MTM.
            # Ejecutamos _build_status() en thread pool para no bloquear el
            # event loop (hace llamadas síncronas a DB + Alpaca).
            if iter_count % 3 == 0:
                try:
                    with get_session() as s:
                        has_open = s.query(Trade).filter(Trade.status == "OPEN").count() > 0
                    if has_open:
                        status = await asyncio.to_thread(_build_status)
                        await manager.broadcast({"type": "status", "data": status})
                except Exception as e:
                    logger.warning("status refresh error: %s", e)
        except Exception as e:
            logger.warning("heartbeat error: %s", e)


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str | None = None):
    # Validar Origin contra allowlist (anti Cross-Site WebSocket Hijacking)
    origin = ws.headers.get("origin")
    host   = ws.headers.get("host")
    if not _check_origin(origin, host):
        logger.warning("WS rechazado por Origin: origin=%r host=%r allowed=%r",
                       origin, host, ALLOWED_ORIGINS)
        await ws.close(code=1008, reason="origin not allowed")
        return
    # Validar token
    if DASHBOARD_TOKEN:
        if token != DASHBOARD_TOKEN:
            logger.warning("WS rechazado por token inválido (origin=%r)", origin)
            await ws.close(code=1008, reason="invalid token")
            return
    elif not ALLOW_NO_AUTH:
        await ws.close(code=1008, reason="auth not configured")
        return

    await manager.connect(ws)
    logger.info("WS connect accepted, building initial data...")
    try:
        # Ejecutar los 4 builders EN PARALELO (gather) en lugar de uno tras otro.
        # Antes: 4 awaits secuenciales = suma de tiempos.
        # Ahora: max(tiempos) — limita por el más lento.
        t0 = asyncio.get_event_loop().time()
        status, signals, trades, log_data = await asyncio.gather(
            asyncio.to_thread(_build_status),
            asyncio.to_thread(_build_signals),
            asyncio.to_thread(_build_trades),
            asyncio.to_thread(_build_log, 40),
            return_exceptions=True,
        )
        dt = asyncio.get_event_loop().time() - t0
        logger.info("WS builders done in %.2fs", dt)

        # Si algún builder lanzó excepción, log y enviar valor por defecto
        if isinstance(status, Exception):
            logger.warning("_build_status falló: %s", status); status = {}
        if isinstance(signals, Exception):
            logger.warning("_build_signals falló: %s", signals); signals = []
        if isinstance(trades, Exception):
            logger.warning("_build_trades falló: %s", trades); trades = []
        if isinstance(log_data, Exception):
            logger.warning("_build_log falló: %s", log_data); log_data = []

        await ws.send_json({"type": "status",  "data": status})
        await ws.send_json({"type": "signals", "data": signals})
        await ws.send_json({"type": "trades",  "data": trades})
        await ws.send_json({"type": "log",     "lines": log_data})
        logger.info("WS initial data sent OK")

        # Receive loop con timeout activo. Si el cliente no manda nada en
        # WS_RECEIVE_TIMEOUT segundos, enviamos un ping de aplicación para
        # verificar que la conexión sigue viva y evitar que el proxy de
        # Fly.io la mate por idle. Si el send falla, la conexión está muerta.
        WS_RECEIVE_TIMEOUT = 25  # segundos — por debajo del idle timeout de Fly.io (60s)
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=WS_RECEIVE_TIMEOUT)
                if msg == "ping":
                    await ws.send_text("pong")
            except asyncio.TimeoutError:
                try:
                    await ws.send_json({"type": "ping", "ts": datetime.now(timezone.utc).isoformat()})
                except Exception:
                    manager.disconnect(ws)
                    break
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as e:
        logger.warning("WS endpoint error: %s", e, exc_info=True)
        manager.disconnect(ws)


# ── Builders ──────────────────────────────────────────────────────────────────

def _build_status() -> dict:
    with get_session() as s:
        closed = s.query(Trade).filter(Trade.status == "CLOSED").all()
        open_trades = s.query(Trade).filter(Trade.status == "OPEN").all()
        wins    = sum(1 for t in closed if (t.pnl_usdt or 0) > 0)
        pnl_sum = sum(t.pnl_usdt or 0 for t in closed)

        # Para cada trade abierto, anexar precio actual y PnL marked-to-market.
        # Para STOCKS: usar cache de precios (poblado en background, no bloquea).
        # Si cache miss → fallback a último candle de DB.
        # Para CRYPTO: último candle close de la DB (feed Binance es confiable).
        open_list = []
        for t in open_trades:
            s.expunge(t)
            if t.symbol in STOCK_SYMBOLS:
                live = _fetch_stock_price_fallback(t.symbol, allow_blocking=False)
                current_price = live if live > 0 else _last_known_price(s, t.symbol)
            else:
                current_price = _last_known_price(s, t.symbol)
            mtm_pnl = ((current_price or t.entry_price) - t.entry_price) * t.quantity
            mtm_pct = ((current_price or t.entry_price) - t.entry_price) / t.entry_price
            open_list.append({
                "id": t.id, "symbol": t.symbol,
                "entry_price": t.entry_price, "quantity": t.quantity,
                "stop_loss": t.stop_loss, "take_profit": t.take_profit,
                "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                "current_price": round(current_price, 2) if current_price else None,
                "mtm_pnl_usdt": round(mtm_pnl, 2),
                "mtm_pnl_pct":  round(mtm_pct * 100, 3),
            })

    # Contar ciclos y obtener el último ts sin cargar el archivo completo
    cycle_count, last_cycle_ts = _count_cycles_cheap()
    return {
        "total_trades":     len(closed),
        "open_trades":      open_list,
        "wins":             wins,
        "losses":           len(closed) - wins,
        "win_rate":         round(wins / len(closed) * 100, 1) if closed else 0,
        "total_pnl_usdt":   round(pnl_sum, 2),
        "total_cycles":     cycle_count,
        "last_cycle":       last_cycle_ts,
        "crypto_symbols":   CRYPTO_SYMBOLS,
        "stock_symbols":    STOCK_SYMBOLS,
        "timeframe":        TIMEFRAME,
        "loop_interval_s":  LOOP_INTERVAL_SECONDS,
        "paused":           PAUSE_FILE.exists(),
    }


def _last_known_price(session, symbol: str) -> float | None:
    candle = (session.query(Candle)
              .filter(Candle.symbol == symbol)
              .order_by(Candle.timestamp.desc())
              .first())
    return candle.close if candle else None


# Cache de precios live por símbolo: {symbol: (price, expires_at_ts)}
# Evita llamadas redundantes a Alpaca dentro de la ventana TTL.
_price_cache: dict[str, tuple[float, float]] = {}
_PRICE_CACHE_TTL_SECONDS = 60   # refresh cada 60s
_PRICE_LOCK = asyncio.Lock()    # evitar refresh concurrentes


def _fetch_stock_price_fallback(symbol: str, allow_blocking: bool = False) -> float:
    """Obtiene el último precio de un stock.

    Si allow_blocking=False (default): solo usa cache → 0 si miss.
    Si allow_blocking=True: hace llamada síncrona a Alpaca si cache miss.

    En _build_signals() y WebSocket connect inicial usar allow_blocking=False
    (responder rápido). El cache se popula en background via _refresh_price_cache().
    """
    import time
    now = time.time()
    cached = _price_cache.get(symbol)
    if cached and cached[1] > now:
        return cached[0]

    if not allow_blocking:
        return 0.0

    # Slow path (allow_blocking=True): query Alpaca live
    price = 0.0
    try:
        from src import alpaca_fetcher
        live = alpaca_fetcher.get_latest_trade_price(symbol)
        if live and live > 0:
            price = live
    except Exception:
        pass

    if price > 0:
        _price_cache[symbol] = (price, now + _PRICE_CACHE_TTL_SECONDS)
    return price


def _refresh_price_cache_sync():
    """Refresca el cache de precios para todos los stocks en background.
    Usa una sola llamada batch (todos los símbolos a la vez) para evitar
    que conexiones colgadas saturen el thread pool."""
    import time
    from src import alpaca_fetcher
    from alpaca.data.requests import StockLatestTradeRequest
    now = time.time()
    if not STOCK_SYMBOLS:
        return
    # Socket-level timeout: garantiza que el thread termine aunque Alpaca cuelgue.
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(12.0)
    try:
        req = StockLatestTradeRequest(symbol_or_symbols=STOCK_SYMBOLS)
        result = alpaca_fetcher.data_client.get_stock_latest_trade(req)
        for symbol in STOCK_SYMBOLS:
            if symbol in result:
                price = float(result[symbol].price)
                if price > 0:
                    _price_cache[symbol] = (price, now + _PRICE_CACHE_TTL_SECONDS)
        logger.debug("batch price refresh OK: %d symbols", len(result))
    except Exception as e:
        logger.warning("batch price refresh failed: %s", e)
    finally:
        socket.setdefaulttimeout(old_timeout)


def _build_signals() -> list:
    result = []
    with get_session() as s:
        for symbol in CRYPTO_SYMBOLS + STOCK_SYMBOLS:
            sig = (s.query(DbSignal)
                   .filter(DbSignal.symbol == symbol)
                   .order_by(DbSignal.created_at.desc()).first())
            candle = (s.query(Candle)
                      .filter(Candle.symbol == symbol)
                      .order_by(Candle.timestamp.desc()).first())
            if sig: s.expunge(sig)
            if candle: s.expunge(candle)

            # Para STOCKS: usar cache de precios (poblado en bg, no bloquea).
            # Para CRYPTO: el feed Binance es confiable → usar close de la DB directamente.
            if symbol in STOCK_SYMBOLS:
                live_price = _fetch_stock_price_fallback(symbol, allow_blocking=False)
                price = live_price if live_price > 0 else (candle.close if candle else 0)
            else:
                price = candle.close if candle else 0

            result.append({
                "symbol":    symbol,
                "signal":    sig.signal_type if sig else "—",
                "reason":    sig.reason if sig else "sin datos",
                "price":     price,
                "rsi":       candle.rsi if candle else 0,
                "ema_fast":  candle.ema_fast if candle else 0,
                "ema_slow":  candle.ema_slow if candle else 0,
                "macd_hist": candle.macd_hist if candle else 0,
                "bb_upper":  candle.bb_upper if candle else 0,
                "bb_lower":  candle.bb_lower if candle else 0,
                "timestamp": sig.timestamp.isoformat() if sig else None,
                "acted_on":  sig.acted_on if sig else False,
            })
    return result


def _build_trades(limit: int = 50) -> list:
    with get_session() as s:
        trades = (s.query(Trade)
                  .order_by(Trade.created_at.desc())
                  .limit(limit).all())
        return [{
            "id": t.id, "symbol": t.symbol, "side": t.side, "status": t.status,
            "entry_price": t.entry_price, "exit_price": t.exit_price,
            "quantity": t.quantity, "stop_loss": t.stop_loss, "take_profit": t.take_profit,
            "pnl_usdt": round(t.pnl_usdt, 2) if t.pnl_usdt else None,
            "pnl_pct": round((t.pnl_pct or 0) * 100, 2),
            "exit_reason": t.exit_reason,
            "entry_time": t.entry_time.isoformat() if t.entry_time else None,
            "exit_time": t.exit_time.isoformat() if t.exit_time else None,
        } for t in trades]


def _build_log(lines: int = 40) -> list:
    if not LOG_FILE.exists():
        return []
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            all_lines = f.readlines()
        return [l.rstrip() for l in all_lines[-lines:]]
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("_build_log error: %s", e)
        return []


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/")
@limiter.limit("60/minute")
def root(request: Request):
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/favicon.ico")
@limiter.limit("60/minute")
def favicon(request: Request):
    """Sirve el favicon SVG en /favicon.ico (path tradicional que piden browsers)."""
    return FileResponse(str(STATIC_DIR / "favicon.svg"), media_type="image/svg+xml")


@app.get("/api/candles/{symbol:path}")
@limiter.limit("60/minute")
def get_candles(request: Request, symbol: str, limit: int = 100):
    # Validación simple: solo permite símbolos conocidos
    valid_symbols = set(CRYPTO_SYMBOLS) | set(STOCK_SYMBOLS)
    if symbol not in valid_symbols:
        raise HTTPException(status_code=400, detail="Símbolo no válido")
    with get_session() as s:
        candles = (s.query(Candle)
                   .filter(Candle.symbol == symbol)
                   .order_by(Candle.timestamp.desc())
                   .limit(limit).all())
        result = [{"t": c.timestamp.isoformat(),
                   "o": c.open, "h": c.high, "l": c.low, "c": c.close,
                   "v": c.volume, "rsi": c.rsi,
                   "ema_fast": c.ema_fast, "ema_slow": c.ema_slow,
                   "macd_hist": c.macd_hist,
                   "bb_upper": c.bb_upper, "bb_lower": c.bb_lower}
                  for c in candles]
    return list(reversed(result))


@app.get("/api/report")
@limiter.limit("30/minute")
def get_report(request: Request):
    return analyst.generate_report()


@app.get("/api/cycles")
@limiter.limit("30/minute")
def get_cycles(request: Request, limit: int = 500):
    limit = max(1, min(limit, 5000))  # cap anti-DoS
    cycles = analyst.load_cycles()
    return cycles[-limit:]


@app.get("/api/ml-report")
@limiter.limit("10/minute")
def get_ml_report(request: Request):
    return ml_analyst.evaluate_strategy()


@app.get("/api/features/summary")
@limiter.limit("30/minute")
def get_features_summary(request: Request):
    """Estado del feature log para ML: cuántos trades etiquetados, si ya se puede entrenar."""
    return analyst.features_summary()


@app.get("/api/per-symbol")
@limiter.limit("30/minute")
def per_symbol_stats(request: Request):
    """Estadísticas por símbolo para tabla del dashboard."""
    with get_session() as s:
        trades = s.query(Trade).filter(Trade.status == "CLOSED").all()
        for t in trades:
            s.expunge(t)

    by_symbol: dict[str, dict] = {}
    for t in trades:
        d = by_symbol.setdefault(t.symbol, {
            "symbol": t.symbol, "trades": 0, "wins": 0, "losses": 0,
            "pnl_usdt": 0.0, "pnl_pct_sum": 0.0,
        })
        d["trades"] += 1
        d["pnl_usdt"] += t.pnl_usdt or 0
        d["pnl_pct_sum"] += (t.pnl_pct or 0) * 100
        if (t.pnl_usdt or 0) > 0:
            d["wins"] += 1
        else:
            d["losses"] += 1

    result = []
    for sym, d in by_symbol.items():
        n = d["trades"]
        result.append({
            "symbol":     sym,
            "trades":     n,
            "wins":       d["wins"],
            "losses":     d["losses"],
            "win_rate":   round(d["wins"] / n * 100, 1) if n > 0 else 0,
            "pnl_usdt":   round(d["pnl_usdt"], 2),
            "avg_pnl_pct": round(d["pnl_pct_sum"] / n, 2) if n > 0 else 0,
        })
    return sorted(result, key=lambda x: -x["pnl_usdt"])


@app.get("/api/export/trades.csv")
@limiter.limit("10/minute")
def export_trades_csv(request: Request):
    """Descarga histórico de trades como CSV."""
    with get_session() as s:
        trades = s.query(Trade).order_by(Trade.created_at.desc()).all()
        for t in trades:
            s.expunge(t)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id","symbol","side","status","entry_price","exit_price",
                "quantity","stop_loss","take_profit","pnl_usdt","pnl_pct",
                "exit_reason","entry_time","exit_time"])
    for t in trades:
        w.writerow([t.id, t.symbol, t.side, t.status,
                    t.entry_price, t.exit_price, t.quantity,
                    t.stop_loss, t.take_profit,
                    t.pnl_usdt, t.pnl_pct, t.exit_reason,
                    t.entry_time, t.exit_time])

    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"})


@app.get("/api/export/cycles.csv")
@limiter.limit("10/minute")
def export_cycles_csv(request: Request):
    cycles = analyst.load_cycles()
    if not cycles:
        return PlainTextResponse("ts,price\n", media_type="text/csv")

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts","symbol","price","signal_type","score","reason",
                "rsi","macd_hist","bb_upper","bb_lower","trade_action"])
    for c in cycles:
        ind = c.get("indicators", {})
        sig = c.get("signal", {})
        w.writerow([c.get("ts"), c.get("symbol",""), c.get("price"),
                    sig.get("type"), sig.get("score"), sig.get("reason"),
                    ind.get("rsi"), ind.get("macd_hist"),
                    ind.get("bb_upper"), ind.get("bb_lower"),
                    c.get("trade_action")])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=cycles.csv"})


# ── Pause / Resume ────────────────────────────────────────────────────────────

def _check_csrf(content_type: str | None):
    """Forzar JSON content-type → dispara preflight CORS → bloquea form CSRF."""
    if not content_type or "application/json" not in content_type.lower():
        raise HTTPException(status_code=415, detail="Content-Type debe ser application/json")


@app.post("/api/pause")
@limiter.limit("10/minute")
def pause_bot(request: Request,
              authorization: str | None = Header(None),
              content_type: str | None = Header(None)):
    _check_csrf(content_type)
    _check_auth(authorization)
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FILE.touch()
    logger.info("Bot pausado vía API")
    return {"status": "paused"}


@app.post("/api/resume")
@limiter.limit("10/minute")
def resume_bot(request: Request,
               authorization: str | None = Header(None),
               content_type: str | None = Header(None)):
    _check_csrf(content_type)
    _check_auth(authorization)
    if PAUSE_FILE.exists():
        PAUSE_FILE.unlink()
    logger.info("Bot reanudado vía API")
    return {"status": "running"}


@app.get("/api/health")
@limiter.limit("120/minute")
def health(request: Request):
    # No revelamos config interna en healthcheck público
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api:app", host="0.0.0.0", port=8000, reload=False)
