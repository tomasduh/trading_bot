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
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, PlainTextResponse

from src.config import BASE_DIR, CRYPTO_SYMBOLS, STOCK_SYMBOLS, TIMEFRAME, LOOP_INTERVAL_SECONDS
from src.database import get_session, Trade, Signal as DbSignal, Candle
from src import analyst, ml_analyst

logger = logging.getLogger("api")

app = FastAPI(title="Trading Bot Dashboard")

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


def _check_origin(origin: str | None) -> bool:
    """Valida que el Origin del WebSocket esté en la allowlist."""
    if not origin:
        return False
    return origin in ALLOWED_ORIGINS


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
    """Tail bot.log y broadcast cada línea nueva."""
    pos = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    while True:
        await asyncio.sleep(1)
        try:
            if not LOG_FILE.exists():
                continue
            size = LOG_FILE.stat().st_size
            if size < pos:
                pos = 0  # rotación detectada
            if size <= pos:
                continue
            with open(LOG_FILE, encoding="utf-8") as f:
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


async def watch_data():
    """Cada 5s actualiza estado, señales, trades."""
    last_cycle_count = 0
    last_trade_count = 0
    consecutive_errors = 0
    while True:
        await asyncio.sleep(5)
        try:
            cycles = analyst.load_cycles()
            if len(cycles) != last_cycle_count:
                last_cycle_count = len(cycles)
                signals = _build_signals()
                status  = _build_status()
                await manager.broadcast({"type": "signals", "data": signals})
                await manager.broadcast({"type": "status",  "data": status})

            with get_session() as s:
                trade_count = s.query(Trade).count()
            if trade_count != last_trade_count:
                last_trade_count = trade_count
                trades = _build_trades()
                await manager.broadcast({"type": "trades", "data": trades})

            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            logger.error("watch_data error (#%d): %s", consecutive_errors, e, exc_info=True)
            await asyncio.sleep(min(60, 5 * consecutive_errors))


async def push_heartbeat():
    """Envía un heartbeat cada 10s para que el cliente detecte stale data."""
    while True:
        await asyncio.sleep(10)
        try:
            await manager.broadcast({
                "type": "heartbeat",
                "ts": datetime.now(timezone.utc).isoformat(),
                "paused": PAUSE_FILE.exists(),
            })
        except Exception as e:
            logger.warning("heartbeat error: %s", e)


@app.on_event("startup")
async def startup():
    asyncio.create_task(watch_log())
    asyncio.create_task(watch_data())
    asyncio.create_task(push_heartbeat())


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str | None = None):
    # Validar Origin contra allowlist (anti Cross-Site WebSocket Hijacking)
    origin = ws.headers.get("origin")
    if not _check_origin(origin):
        await ws.close(code=1008, reason="origin not allowed")
        return
    # Validar token
    if DASHBOARD_TOKEN:
        if token != DASHBOARD_TOKEN:
            await ws.close(code=1008, reason="invalid token")
            return
    elif not ALLOW_NO_AUTH:
        await ws.close(code=1008, reason="auth not configured")
        return

    await manager.connect(ws)
    try:
        await ws.send_json({"type": "status",  "data": _build_status()})
        await ws.send_json({"type": "signals", "data": _build_signals()})
        await ws.send_json({"type": "trades",  "data": _build_trades()})
        await ws.send_json({"type": "log",     "lines": _build_log(40)})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as e:
        logger.warning("WS endpoint error: %s", e)
        manager.disconnect(ws)


# ── Builders ──────────────────────────────────────────────────────────────────

def _build_status() -> dict:
    with get_session() as s:
        closed = s.query(Trade).filter(Trade.status == "CLOSED").all()
        open_trades = s.query(Trade).filter(Trade.status == "OPEN").all()
        wins    = sum(1 for t in closed if (t.pnl_usdt or 0) > 0)
        pnl_sum = sum(t.pnl_usdt or 0 for t in closed)

        # Para cada trade abierto, anexar precio actual y PnL marked-to-market
        open_list = []
        for t in open_trades:
            s.expunge(t)
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

    cycles = analyst.load_cycles()
    return {
        "total_trades":     len(closed),
        "open_trades":      open_list,
        "wins":             wins,
        "losses":           len(closed) - wins,
        "win_rate":         round(wins / len(closed) * 100, 1) if closed else 0,
        "total_pnl_usdt":   round(pnl_sum, 2),
        "total_cycles":     len(cycles),
        "last_cycle":       cycles[-1]["ts"] if cycles else None,
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


def _fetch_stock_price_fallback(symbol: str) -> float:
    """Obtiene el último precio de un stock desde Alpaca histórico (funciona con mercado cerrado)."""
    try:
        from src import alpaca_fetcher
        df = alpaca_fetcher.fetch_ohlcv(symbol, timeframe="1d", limit=1)
        if not df.empty:
            return float(df["close"].iloc[-1])
    except Exception:
        pass
    return 0.0


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

            # Si no hay candle en DB (ej: mercado cerrado), intentar fetch histórico
            price = candle.close if candle else 0
            if price == 0 and symbol in STOCK_SYMBOLS:
                price = _fetch_stock_price_fallback(symbol)

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
def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/candles/{symbol:path}")
def get_candles(symbol: str, limit: int = 100):
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
def get_report():
    return analyst.generate_report()


@app.get("/api/cycles")
def get_cycles(limit: int = 500):
    limit = max(1, min(limit, 5000))  # cap anti-DoS
    cycles = analyst.load_cycles()
    return cycles[-limit:]


@app.get("/api/ml-report")
def get_ml_report():
    return ml_analyst.evaluate_strategy()


@app.get("/api/per-symbol")
def per_symbol_stats():
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
def export_trades_csv():
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
def export_cycles_csv():
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
def pause_bot(authorization: str | None = Header(None),
              content_type: str | None = Header(None)):
    _check_csrf(content_type)
    _check_auth(authorization)
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FILE.touch()
    logger.info("Bot pausado vía API")
    return {"status": "paused"}


@app.post("/api/resume")
def resume_bot(authorization: str | None = Header(None),
               content_type: str | None = Header(None)):
    _check_csrf(content_type)
    _check_auth(authorization)
    if PAUSE_FILE.exists():
        PAUSE_FILE.unlink()
    logger.info("Bot reanudado vía API")
    return {"status": "running"}


@app.get("/api/health")
def health():
    # No revelamos config interna en healthcheck público
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api:app", host="0.0.0.0", port=8000, reload=False)
