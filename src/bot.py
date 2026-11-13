"""
Loop principal del bot con kill-switch, pause/resume y manejo robusto de errores.
Ejecutar con: python -m src.bot
"""
import gc
import time
import logging
from pathlib import Path
from datetime import datetime, timezone

from src import config, data_fetcher, strategy, logger as log_setup
from src.database import init_db, Candle, Signal as DbSignal, get_session
from src.executor import TradeExecutor
from src import indicators, analyst, alerts, reconciler, backup, budget
from src import alpaca_fetcher
from src.circuit_breaker import get_breaker
from src.data_fetcher import DataFetchError, FatalDataFetchError

log_setup.setup("bot")
logger = logging.getLogger("bot")

# ── Kill-switch ───────────────────────────────────────────────────────────────
MAX_CONSECUTIVE_ERRORS = 5
PAUSE_FILE = config.BASE_DIR / "data" / ".paused"


def is_paused() -> bool:
    return PAUSE_FILE.exists()


# ── Resumen diario por Telegram ───────────────────────────────────────────────
# Con una estrategia diaria la mayoría de los días no pasa nada: el resumen
# confirma que el bot sigue vivo y cómo va, sin tener que abrir el dashboard.
SUMMARY_TIME_UTC = (21, 15)    # después del cierre de Wall Street (16:00 ET)
SUMMARY_MARKER = config.BASE_DIR / "data" / ".last_summary"
_last_prices: dict[str, float] = {}     # último precio visto por símbolo


def maybe_daily_summary(executor, now: datetime | None = None) -> bool:
    """Envía el resumen una vez por día, pasada la hora configurada."""
    if not alerts.enabled():
        return False
    now = now or datetime.now(timezone.utc)
    if (now.hour, now.minute) < SUMMARY_TIME_UTC:
        return False
    today = now.date().isoformat()
    try:
        if SUMMARY_MARKER.exists() and SUMMARY_MARKER.read_text().strip() == today:
            return False
        SUMMARY_MARKER.write_text(today)
    except OSError:
        return False
    try:
        alerts.notify_daily_summary(**build_daily_summary(executor, now))
        return True
    except Exception as e:
        logger.warning(f"Resumen diario falló: {e}")
        return False


def build_daily_summary(executor, now: datetime) -> dict:
    """Datos del resumen: cerradas hoy (UTC), abiertas con su flotante y acumulado.
    Con tope de capital activo solo cuentan los trades posteriores al tope."""
    from src.database import Trade

    def aware(dt):
        return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with get_session() as s:
        closed = [dict(symbol=t.symbol, pnl_usdt=t.pnl_usdt, pnl_pct=t.pnl_pct, exit_time=aware(t.exit_time))
                  for t in s.query(Trade).filter(Trade.status == "CLOSED").all() if budget.counts(t)]
    today = sorted((t for t in closed if t["exit_time"] and t["exit_time"] >= day_start),
                   key=lambda t: t["exit_time"])
    return {
        "day": now.strftime("%d/%m/%Y"),
        "closed_today": [{k: t[k] for k in ("symbol", "pnl_usdt", "pnl_pct")} for t in today],
        "open_positions": [{"symbol": t.symbol, "entry": t.entry_price, "qty": t.quantity,
                            "price": _last_prices.get(t.symbol), "opened_at": t.entry_time}
                           for t in executor.open_trades()],
        "realized_total": sum(t["pnl_usdt"] or 0 for t in closed),
        "capital": total_capital(executor),
        "paused": is_paused(),
        "strategy": config.STRATEGY_MODE,
        "now": now,
    }


def persist_candle(symbol: str, df_ind):
    """Persiste la última vela cerrada. Idempotente: el UNIQUE index
    (symbol, timestamp) evita duplicados si el ciclo vuelve a correr antes
    de que cierre la siguiente vela 30m."""
    from sqlalchemy.exc import IntegrityError

    valid = df_ind.dropna()
    if len(valid) < 2:
        return  # warmup todavía
    last = valid.iloc[-2]   # vela cerrada
    candle = Candle(
        symbol=symbol,
        timeframe=config.TIMEFRAME,
        timestamp=last.name.to_pydatetime(),
        open=float(last["open"]),
        high=float(last["high"]),
        low=float(last["low"]),
        close=float(last["close"]),
        volume=float(last["volume"]),
        ema_fast=float(last["ema_fast"]),
        ema_slow=float(last["ema_slow"]),
        rsi=float(last["rsi"]),
        macd=float(last["macd"]),
        macd_signal=float(last["macd_signal"]),
        macd_hist=float(last["macd_hist"]),
        bb_upper=float(last["bb_upper"]),
        bb_mid=float(last["bb_mid"]),
        bb_lower=float(last["bb_lower"]),
    )
    try:
        with get_session() as session:
            session.add(candle)
            session.commit()
    except IntegrityError:
        # Vela ya existe (UNIQUE constraint). Es lo esperado en ciclos repetidos
        # antes de que cierre una nueva vela 30m. Silenciar sin warning.
        pass


def persist_signal(symbol: str, sig, acted_on: bool):
    db_sig = DbSignal(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        signal_type=sig.type,
        reason=sig.reason,
        close_price=sig.price,
        rsi=sig.rsi,
        ema_fast=sig.ema_fast,
        ema_slow=sig.ema_slow,
        acted_on=acted_on,
    )
    with get_session() as session:
        session.add(db_sig)
        session.commit()


def _indicator_snapshot(df_ind) -> dict | None:
    valid = df_ind.dropna()
    if len(valid) < 2:
        return None
    last = valid.iloc[-2]   # vela cerrada
    return {
        "ema_fast":       float(last["ema_fast"]),
        "ema_slow":       float(last["ema_slow"]),
        "rsi":            float(last["rsi"]),
        "macd":           float(last["macd"]),
        "macd_signal":    float(last["macd_signal"]),
        "macd_hist":      float(last["macd_hist"]),
        "bb_upper":       float(last["bb_upper"]),
        "bb_mid":         float(last["bb_mid"]),
        "bb_lower":       float(last["bb_lower"]),
        "adx":            float(last.get("adx", 0) or 0),
        "ema_trend_fast": float(last.get("ema_trend_fast", 0) or 0),
        "ema_trend_slow": float(last.get("ema_trend_slow", 0) or 0),
        "atr":            float(last.get("atr", 0) or 0),
    }


def _extract_ml_features(df_ind, signal, live_price: float) -> dict:
    """
    Extrae el vector de features completo para ML en el momento de decisión.
    Siempre usa la última vela CERRADA (iloc[-2]) para evitar look-ahead.

    Features incluidas:
      - Indicadores técnicos absolutos y relativos (RSI, MACD, BB, EMA, ADX, ATR)
      - Posición del precio dentro de las Bandas de Bollinger (normalizada 0-1)
      - Ratios y diffs para capturar momentum y divergencias
      - Contexto temporal (hora UTC, día de semana) — captura estacionalidad
    """
    valid = df_ind.dropna()
    if len(valid) < 3:
        return {}
    curr = valid.iloc[-2]
    prev = valid.iloc[-3]

    close     = float(curr["close"])
    bb_upper  = float(curr.get("bb_upper", close) or close)
    bb_lower  = float(curr.get("bb_lower", close) or close)
    bb_mid    = float(curr.get("bb_mid",   close) or close)
    bb_range  = max(bb_upper - bb_lower, 1e-10)
    ema_fast  = float(curr.get("ema_fast", close) or close)
    ema_slow  = float(curr.get("ema_slow", close) or close)
    atr       = float(curr.get("atr", 0) or 0)
    rsi       = float(curr.get("rsi", 50) or 50)
    rsi_prev  = float(prev.get("rsi", 50) or 50)
    macd_hist = float(curr.get("macd_hist", 0) or 0)
    mh_prev   = float(prev.get("macd_hist", 0) or 0)
    adx       = float(curr.get("adx", 0) or 0)
    vol       = float(curr.get("volume", 0) or 0)
    vol_prev  = float(prev.get("volume", 1) or 1)

    ts = curr.name if hasattr(curr, "name") else None

    return {
        # RSI
        "rsi":              round(rsi, 2),
        "rsi_diff":         round(rsi - rsi_prev, 4),
        # MACD
        "macd_hist":        round(macd_hist, 6),
        "macd_hist_diff":   round(macd_hist - mh_prev, 6),
        # Bollinger (posición normalizada 0=lower, 1=upper)
        "bb_pos":           round((close - bb_lower) / bb_range, 4),
        "bb_width":         round(bb_range / max(bb_mid, 1e-10), 4),
        # EMAs
        "ema_ratio":        round(ema_fast / max(ema_slow, 1e-10), 6),
        "price_vs_ema_fast": round((live_price - ema_fast) / max(ema_fast, 1e-10), 6),
        # ATR normalizado (volatilidad relativa al precio)
        "atr_pct":          round(atr / max(live_price, 1e-10), 6),
        # ADX (fuerza de tendencia)
        "adx":              round(adx, 2),
        # Volumen relativo vs vela anterior
        "vol_ratio":        round(vol / max(vol_prev, 1e-10), 4),
        # Tendencia del signal
        "trend":            {"up": 1, "down": -1, "neutral": 0}.get(signal.trend, 0),
        "signal_score":     signal.score,
        # Contexto temporal (estacionalidad)
        "hour_utc":         ts.hour         if ts is not None else -1,
        "day_of_week":      ts.dayofweek    if ts is not None else -1,
    }


def process_market(symbol: str, executor: TradeExecutor, capital: float,
                   fetcher, label: str = "CRYPTO", allow_entries: bool = True) -> float:
    """
    Procesa un ciclo de análisis y ejecución para un símbolo (crypto o stock).
    `fetcher` debe tener método fetch_ohlcv(symbol, timeframe).
    `label` es "CRYPTO" o "STOCK" — solo para formato de log.
    `allow_entries=False` → solo gestiona la salida del trade abierto (símbolos
    quitados de config); nunca abre uno nuevo.

    Devuelve el notional (USDT/USD) comprometido si abrió un trade nuevo, o 0.0.
    El caller (run_cycle) lo resta del capital disponible para el resto del ciclo
    — evita que dos símbolos que abren en el mismo ciclo se pisen usando el mismo
    balance ya gastado por el primero.
    """
    width = 12 if label == "CRYPTO" else 6
    try:
        # BUG FIX: pasar limit explícito a Alpaca (su default era 200, ignorando config.CANDLES_LIMIT)
        df = fetcher.fetch_ohlcv(symbol, timeframe=config.TIMEFRAME, limit=config.CANDLES_LIMIT) \
            if label == "STOCK" else fetcher.fetch_ohlcv(symbol=symbol)
        if df.empty or len(df) < 30:
            logger.warning(f"  {symbol:<{width}} sin suficientes datos")
            return 0.0

        df_ind = indicators.add_all(df)
        live_price = float(df_ind["close"].iloc[-1])
        _last_prices[symbol] = live_price
        ind_snap = _indicator_snapshot(df_ind)
        if ind_snap is None:
            logger.warning(f"  {symbol:<{width}} indicadores en warmup")
            return 0.0

        logger.info(
            f"  {symbol:<{width}} @ {live_price:>10,.2f} | "
            f"RSI={ind_snap['rsi']:.1f}  MACD_hist={ind_snap['macd_hist']:+.1f}  "
            f"ADX={ind_snap['adx']:.0f}"
        )

        persist_candle(symbol, df_ind)

        # 1. OCO del exchange + SL/TP/Trailing del bot
        if manage_exits(symbol, executor, live_price):
            analyst.log_cycle(live_price, ind_snap, strategy.evaluate(df_ind),
                              trade_action="CLOSED", symbol=symbol)
            return 0.0

        # 2. Evaluar señal — diaria, o multi-timeframe si está habilitado
        if config.IS_DAILY:
            signal = daily_signal(symbol, fetcher, width)
        elif config.USE_MTF:
            df_macro = None
            try:
                df_macro = fetcher.fetch_ohlcv(
                    symbol, timeframe=config.MTF_TIMEFRAME) \
                    if label == "STOCK" else fetcher.fetch_ohlcv(
                        symbol=symbol, timeframe=config.MTF_TIMEFRAME)
                signal = strategy.evaluate_mtf(df_ind, df_macro)
            except Exception as e:
                logger.warning(f"  {symbol:<{width}} MTF fetch falló ({e}) — usando solo 30m")
                signal = strategy.evaluate(df_ind)
            finally:
                # Liberar memoria: df_macro puede ser un DataFrame grande (200+ velas)
                if df_macro is not None:
                    del df_macro
        else:
            signal = strategy.evaluate(df_ind)

        signal.price = live_price   # precio vivo, no el de la vela cerrada
        trade_action = None
        acted = False
        notional_committed = 0.0

        if signal.type != "NONE":
            logger.info(
                f"  {symbol:<{width}} [{signal.type}] score={signal.score}/3 "
                f"trend={signal.trend} → {signal.reason}"
            )

        if signal.type == "BUY" and allow_entries and not executor.has_open_trade(symbol):
            # ── Cap de exposición del mercado (crypto y stocks por separado) ──
            # Modo diario: no aplica (cada símbolo tiene su fracción fija del capital).
            exposure = 0.0 if config.IS_DAILY else \
                executor.total_exposure_pct(capital, stocks=(label == "STOCK"))
            notional = executor.target_notional(capital, stocks=(label == "STOCK")) \
                if config.IS_DAILY else None
            if config.IS_DAILY and _already_traded_this_bar(symbol, executor, signal):
                logger.info(f"  {symbol:<{width}} BUY omitido — ya se salió con esta misma vela diaria")
            elif exposure >= config.MAX_TOTAL_EXPOSURE_PCT:
                logger.warning(
                    f"  {symbol:<{width}} BUY bloqueado — exposición {label.lower()} "
                    f"{exposure*100:.1f}% ≥ {config.MAX_TOTAL_EXPOSURE_PCT*100:.0f}%"
                )
            elif capital > 10:
                trade = executor.open_trade(symbol, signal, capital, notional=notional)
                if trade:
                    trade_action = "OPENED"
                    acted = True
                    notional_committed = trade.entry_price * trade.quantity
                    alerts.notify_trade_opened(symbol, trade.entry_price,
                                                trade.quantity, trade.stop_loss,
                                                trade.take_profit, signal.reason,
                                                trade_id=getattr(trade, "id", None))
                    # Feature logging: vector completo en el momento de la entrada
                    ml_features = _extract_ml_features(df_ind, signal, live_price)
                    if ml_features:
                        analyst.log_features(
                            symbol=symbol,
                            trade_id=trade.id,
                            price=live_price,
                            features=ml_features,
                            signal_type="BUY",
                        )
            else:
                logger.warning(f"  {symbol:<{width}} Balance insuficiente.")

        elif (signal.type == "SELL"
              and executor.has_open_trade(symbol)
              and (config.IS_DAILY or not config.DISABLE_SIGNAL_EXIT)):
            # Cambio quirúrgico v2: SIGNAL solo cierra si hay pérdida REAL (con buffer)
            # Modo diario: la señal de salida cierra siempre (es la salida de la regla).
            open_trade = executor.get_open_trade(symbol)
            entry = (open_trade.entry_price if open_trade else live_price) or live_price
            loss_threshold = entry * (1 - config.SIGNAL_EXIT_LOSS_BUFFER_PCT)
            in_real_loss = live_price < loss_threshold
            closed = None
            if config.IS_DAILY or not config.SIGNAL_EXIT_ONLY_IN_LOSS or in_real_loss:
                closed = executor.close_trade(symbol, live_price, "SIGNAL")
            if closed:
                trade_action = "CLOSED"
                acted = True
                _notify_closed(symbol, closed)

        persist_signal(symbol, signal, acted)
        analyst.log_cycle(live_price, ind_snap, signal,
                          trade_action=trade_action, symbol=symbol)
        return notional_committed

    except DataFetchError as e:
        logger.warning(f"  [{symbol}] Error temporal de datos: {e}")
    except Exception as e:
        logger.error(f"  [{symbol}] Error inesperado: {e}", exc_info=True)
    return 0.0


def daily_signal(symbol: str, fetcher, width: int = 12):
    """Señal de la estrategia diaria. Si no se pueden traer las velas diarias
    devuelve NONE: sin datos no se entra ni se sale por señal (queda el stop)."""
    try:
        return strategy.evaluate_daily(symbol, fetcher.fetch_daily(symbol))
    except Exception as e:
        logger.warning(f"  {symbol:<{width}} velas diarias no disponibles ({e}) — sin señal este ciclo")
        return strategy.Signal("NONE", f"velas diarias no disponibles: {e}", 0, 0, 0, 0, 0)


def _already_traded_this_bar(symbol: str, executor: TradeExecutor, signal) -> bool:
    """¿Ya se cerró un trade de este símbolo desde que cerró la vela de la señal?
    Evita recomprar el mismo día tras un stop (la señal BUY sigue vigente hasta
    la próxima vela diaria)."""
    if signal.valid_from is None:
        return False
    last_exit = executor.last_exit_time(symbol)
    return last_exit is not None and last_exit >= signal.valid_from


def manage_exits(symbol: str, executor: TradeExecutor, live_price: float) -> bool:
    """Salidas del trade abierto, en orden:
      1. ¿La OCO del exchange ya cerró la posición? → registrar ese fill.
      2. SL/TP/Trailing del bot (respaldo) → cancelar OCO y vender a mercado.
      3. Si sigue abierto sin OCO (trade viejo, OCO cancelada) → colocarla.
    Devuelve True si hubo cierre o intento de cierre en este ciclo."""
    closed = executor.sync_protection(symbol)
    trigger = None
    if not closed:
        trigger = executor.check_sl_tp(symbol, live_price)
        if trigger:
            closed = executor.close_trade(symbol, live_price, trigger)
    if closed:
        _notify_closed(symbol, closed)
        return True
    if trigger:
        return True     # cierre fallido: se reintenta el próximo ciclo
    executor.ensure_protection(symbol)
    return False


def _notify_closed(symbol: str, closed):
    alerts.notify_trade_closed(symbol, closed.entry_price, closed.exit_price,
                                closed.pnl_usdt or 0, closed.pnl_pct or 0,
                                closed.exit_reason,
                                qty=getattr(closed, "quantity", None),
                                opened_at=getattr(closed, "entry_time", None),
                                closed_at=getattr(closed, "exit_time", None),
                                trade_id=getattr(closed, "id", None))
    # Feature logging: registrar outcome del trade
    analyst.log_outcome(
        trade_id=closed.id, symbol=symbol,
        exit_reason=closed.exit_reason,
        pnl_pct=closed.pnl_pct or 0,
        pnl_usdt=closed.pnl_usdt or 0,
    )


PREOPEN_WINDOW_MIN = 30


def prepare_stock_protection_preopen(executor: TradeExecutor):
    """Con el mercado cerrado y la apertura a ≤30 min: renueva los stops DAY de
    las posiciones fraccionarias (vencieron al cierre anterior) y repone OCO
    faltantes, para que el stop esté en el exchange desde el primer print y no
    recién en el primer ciclo con mercado abierto. Si Alpaca no lo acepta antes
    de abrir, se coloca en ese primer ciclo (ahí sí con alerta si falla)."""
    symbols = executor.open_symbols(stocks=True)
    if not symbols:
        return
    try:
        if alpaca_fetcher.minutes_to_open() > PREOPEN_WINDOW_MIN:
            return
    except Exception as e:
        logger.warning(f"[STOCKS] No se pudo consultar el reloj de mercado: {e}")
        return
    for symbol in symbols:
        closed = executor.sync_protection(symbol)
        if closed:
            _notify_closed(symbol, closed)
        else:
            executor.ensure_protection(symbol, alert=False)


def check_exits_only(symbol: str, executor: TradeExecutor, fetcher, label: str = "CRYPTO"):
    """Solo SL/TP/Trailing del trade abierto — sin evaluar señales ni abrir nada.
    Se usa con el bot pausado: la pausa frena entradas nuevas, pero las
    posiciones abiertas no pueden quedar sin stop (antes quedaban días sin
    vigilancia tras un circuit breaker)."""
    width = 12 if label == "CRYPTO" else 6
    try:
        df = fetcher.fetch_ohlcv(symbol, timeframe=config.TIMEFRAME, limit=config.CANDLES_LIMIT) \
            if label == "STOCK" else fetcher.fetch_ohlcv(symbol=symbol)
        if df.empty:
            logger.warning(f"  {symbol:<{width}} sin datos — SL/TP no chequeado")
            return
        live_price = float(df["close"].iloc[-1])
        _last_prices[symbol] = live_price
        if manage_exits(symbol, executor, live_price):
            return
        # Modo diario: la salida normal es la señal, no el stop. Pausado tampoco
        # puede quedar una posición esperando solo el stop de emergencia.
        if config.IS_DAILY and daily_signal(symbol, fetcher, width).type == "SELL":
            closed = executor.close_trade(symbol, live_price, "SIGNAL")
            if closed:
                _notify_closed(symbol, closed)
                return
        logger.info(f"  {symbol:<{width}} @ {live_price:>10,.2f} | posición abierta, SL/TP OK")
    except DataFetchError as e:
        logger.warning(f"  [{symbol}] Error temporal de datos (pausa): {e}")
    except Exception as e:
        logger.error(f"  [{symbol}] Error gestionando salida en pausa: {e}", exc_info=True)


def manage_exits_while_paused(executor: TradeExecutor):
    """Recorre todas las posiciones abiertas y aplica solo SL/TP/Trailing."""
    for symbol in executor.open_symbols(stocks=False):
        check_exits_only(symbol, executor, data_fetcher, "CRYPTO")
    stocks = executor.open_symbols(stocks=True)
    if stocks:
        try:
            if alpaca_fetcher.is_market_open():
                for symbol in stocks:
                    check_exits_only(symbol, executor, alpaca_fetcher, "STOCK")
            else:
                prepare_stock_protection_preopen(executor)
        except Exception as e:
            logger.warning(f"[STOCKS] Error gestionando salidas en pausa: {e}")


def _cap_note(stocks: bool, available: float) -> str:
    c = budget.cap(stocks)
    return f" | tope ${c:,.0f} → disponible ${available:,.2f}" if c > 0 else ""


def total_capital(executor: TradeExecutor) -> float:
    """Capital total para el circuit breaker: suma de los exchanges activos (no
    "off"), cada uno con su tope si lo tiene. Sin tope: Binance = USDT (libre +
    en órdenes) + nocional abierto en crypto; Alpaca = equity. 0 si no se pudo
    calcular (el breaker usa entonces su propio cálculo)."""
    total = 0.0
    if config.BINANCE_MODE != "off":
        if budget.cap(False) > 0:
            total += budget.cap(False)
        else:
            try:
                b = data_fetcher.fetch_balance()
                total += float(b.get("free", {}).get("USDT", 0) or 0) +                          float(b.get("used", {}).get("USDT", 0) or 0) +                          executor.open_notional(stocks=False)
            except Exception as e:
                logger.warning(f"[CB] Balance Binance falló: {e}")
    if config.ALPACA_MODE != "off":
        if budget.cap(True) > 0:
            total += budget.cap(True)
        else:
            try:
                total += float(alpaca_fetcher.trading_client.get_account().equity or 0)
            except Exception as e:
                logger.warning(f"[CB] Balance Alpaca falló: {e}")
    return total


def _ping_watchdog():
    """Ping a healthchecks.io al final de cada ciclo exitoso."""
    if not config.HC_PING_URL:
        return
    try:
        import requests
        requests.get(config.HC_PING_URL, timeout=5)
        logger.debug("Watchdog ping OK")
    except Exception as e:
        logger.warning(f"Watchdog ping falló: {e}")


def run_cycle(executor: TradeExecutor, initial_capital: float = 10_000.0):
    if is_paused():
        logger.info("⏸  Bot pausado (existe data/.paused) — sin entradas, solo SL/TP de posiciones abiertas.")
        manage_exits_while_paused(executor)
        # Pausado no es caído: el ciclo corrió y vigiló las salidas. Sin este
        # ping el watchdog externo alertaría "bot caído" durante cada pausa.
        _ping_watchdog()
        gc.collect()
        return

    logger.info("─" * 55)

    # ── Circuit breaker ───────────────────────────────────────────────────────
    # Evalúa condiciones de riesgo antes de operar. Si se activa, pausa el bot
    # tocando PAUSE_FILE (igual que /api/pause) y notifica por alerts.
    breaker = get_breaker(initial_capital)
    should_pause, cb_reason = breaker.check(total_capital(executor))
    if should_pause:
        logger.critical(f"🚨 CIRCUIT BREAKER activado: {cb_reason}")
        alerts.notify_critical(
            "Circuit breaker — bot pausado", cb_reason,
            action="revisar las posiciones y reanudar desde el dashboard cuando corresponda. "
                   "Mientras tanto no abre nada nuevo; las salidas siguen activas.")
        PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FILE.touch()
        manage_exits_while_paused(executor)
        _ping_watchdog()
        return

    # ── Reconciliación: DB vs exchange ────────────────────────────────────────
    # Verifica que los trades "abiertos" en DB coincidan con posiciones reales.
    # Solo loguea — no cierra posiciones automáticamente.
    reconciler.reconcile_all(executor)

    # ── Crypto ───────────────────────────────────────────────────────────────
    # BINANCE_MODE=off: no abre nada, pero sigue gestionando la salida de lo abierto.
    crypto_on = config.BINANCE_MODE != "off"
    if crypto_on or executor.open_symbols(stocks=False):
        try:
            if crypto_on:
                balance = data_fetcher.fetch_balance()
                usdt_free = float(balance.get("free", {}).get("USDT", 0))
                # Con tope (ensayo/techo de seguridad) el bot ve como máximo ese capital.
                # Se descuenta lo comprometido por cada símbolo dentro del mismo ciclo:
                # sin esto, dos señales BUY en el mismo ciclo sizean ambas contra el
                # balance de ANTES del ciclo, pudiendo pedir en conjunto más notional
                # del que realmente queda libre en la cuenta.
                available_usdt = executor.available_capital(usdt_free, stocks=False)
                logger.info(f"[CRYPTO] Balance USDT: {usdt_free:,.2f}" + _cap_note(False, available_usdt))
                for symbol in config.CRYPTO_SYMBOLS:
                    available_usdt -= process_market(symbol, executor, available_usdt, data_fetcher, "CRYPTO")
            exits_only = executor.legacy_open_symbols(stocks=False) if crypto_on                 else executor.open_symbols(stocks=False)
            for symbol in exits_only:
                process_market(symbol, executor, 0.0, data_fetcher, "CRYPTO", allow_entries=False)
        except DataFetchError as e:
            logger.warning(f"[CRYPTO] No se pudo obtener balance: {e}")
        gc.collect()   # liberar DataFrames de crypto antes de procesar stocks

    # ── Stocks ───────────────────────────────────────────────────────────────
    stocks_on = config.ALPACA_MODE != "off"
    if stocks_on or executor.open_symbols(stocks=True):
        try:
            if alpaca_fetcher.is_market_open():
                if stocks_on:
                    bal = alpaca_fetcher.fetch_balance()
                    usd_free = bal["free"]["USD"]
                    available_usd = executor.available_capital(usd_free, stocks=True)
                    logger.info(f"[STOCKS] Mercado abierto | Cash sin margen: ${usd_free:,.2f}"
                                + _cap_note(True, available_usd))
                    for symbol in config.STOCK_SYMBOLS:
                        available_usd -= process_market(symbol, executor, available_usd, alpaca_fetcher, "STOCK")
                exits_only = executor.legacy_open_symbols(stocks=True) if stocks_on                     else executor.open_symbols(stocks=True)
                for symbol in exits_only:
                    process_market(symbol, executor, 0.0, alpaca_fetcher, "STOCK", allow_entries=False)
            else:
                logger.info("[STOCKS] Mercado cerrado — esperando apertura (9:30 ET)")
                prepare_stock_protection_preopen(executor)
        except Exception as e:
            logger.warning(f"[STOCKS] Error: {e}")

    # ── Watchdog ping ─────────────────────────────────────────────────────────
    _ping_watchdog()

    # GC al final del ciclo — libera DataFrames pandas acumulados.
    # Con 10 símbolos × CANDLES_LIMIT=100 la huella es ~15-20MB por ciclo.
    gc.collect()


def run_bot_forever():
    """Loop principal del bot. Diseñado para correr en un thread separado
    (asyncio.to_thread) dentro del proceso de uvicorn, eliminando el segundo
    proceso Python que saturaba la VM 512MB."""
    logger.info("=" * 55)
    logger.info(f"Bot iniciando — modo: {config.mode_label()} | DB: {config.DB_PATH.name}")
    for w in config.MODE_WARNINGS:
        logger.warning(f"⚠ {w}")
    logger.info(f"Crypto: {', '.join(config.CRYPTO_SYMBOLS)}")
    logger.info(f"Stocks: {', '.join(config.STOCK_SYMBOLS)} (solo horario mercado)")
    logger.info(f"Timeframe: {config.TIMEFRAME} | Estrategia: {config.STRATEGY_MODE}")
    logger.info(f"Filtro tendencia: {'ACTIVO' if config.USE_TREND_FILTER else 'inactivo'}")
    logger.info("=" * 55)

    init_db()
    executor = TradeExecutor()
    consecutive_fatal = 0

    # Capital del circuit breaker: suma de exchanges activos (con tope si lo hay).
    # Se recalcula en cada ciclo; este valor es solo el fallback.
    _cb_capital = total_capital(executor) or 10_000.0
    logger.info(f"Circuit breaker: capital total ${_cb_capital:,.2f}")

    caps = [f"{name} ${budget.cap(st):,.0f}" for name, st in (("Binance", False), ("Alpaca", True))
            if budget.cap(st) > 0]
    n_open = len(executor.open_symbols(stocks=False)) + len(executor.open_symbols(stocks=True))
    alerts.notify_info(
        f"Bot iniciado — Binance {config.BINANCE_MODE}, Alpaca {config.ALPACA_MODE}\n"
        f"Estrategia: {config.STRATEGY_MODE} · {len(config.CRYPTO_SYMBOLS)} crypto + "
        f"{len(config.STOCK_SYMBOLS)} stocks\n"
        f"Posiciones abiertas: {n_open}"
        + (f"\nTope de capital: {', '.join(caps)}" if caps else "")
        + ("\n⏸ Arranca PAUSADO" if is_paused() else "")
    )
    for w in config.MODE_WARNINGS:
        alerts.notify_error(w)
    last_cycle_error = None

    while True:
        try:
            run_cycle(executor, initial_capital=_cb_capital)
            consecutive_fatal = 0
            last_cycle_error = None
            backup.maybe_backup()
            maybe_daily_summary(executor)
        except KeyboardInterrupt:
            logger.info("Bot detenido por el usuario.")
            break
        except FatalDataFetchError as e:
            consecutive_fatal += 1
            logger.error(f"⚠ Error fatal #{consecutive_fatal}/{MAX_CONSECUTIVE_ERRORS}: {e}")
            alerts.notify_error(f"Error fatal de datos: {e}")
            if consecutive_fatal >= MAX_CONSECUTIVE_ERRORS:
                logger.critical("KILL-SWITCH ACTIVADO — demasiados errores fatales. Deteniendo bot.")
                alerts.notify_critical(
                    "Kill-switch — bot detenido",
                    f"{consecutive_fatal} errores fatales de datos seguidos. Último: {e}",
                    action="revisar claves o bloqueo de IP del exchange y reiniciar la máquina en Fly. "
                           "Los stops colocados en el exchange siguen vigentes.")
                break
        except Exception as e:
            logger.error(f"Error en ciclo: {e}", exc_info=True)
            if str(e) != last_cycle_error:   # avisar una vez por error distinto
                last_cycle_error = str(e)
                alerts.notify_error(f"Error en ciclo: {e}")

        logger.info(f"Esperando {config.LOOP_INTERVAL_SECONDS // 60} min...\n")
        time.sleep(config.LOOP_INTERVAL_SECONDS)


def main():
    """Punto de entrada para ejecución directa (CLI / desarrollo local)."""
    run_bot_forever()


if __name__ == "__main__":
    main()
