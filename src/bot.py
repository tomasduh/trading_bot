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
from src import indicators, analyst, alerts, reconciler
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
                   fetcher, label: str = "CRYPTO"):
    """
    Procesa un ciclo de análisis y ejecución para un símbolo (crypto o stock).
    `fetcher` debe tener método fetch_ohlcv(symbol, timeframe).
    `label` es "CRYPTO" o "STOCK" — solo para formato de log.
    """
    width = 12 if label == "CRYPTO" else 6
    try:
        df = fetcher.fetch_ohlcv(symbol, timeframe=config.TIMEFRAME) \
            if label == "STOCK" else fetcher.fetch_ohlcv(symbol=symbol)
        if df.empty or len(df) < 30:
            logger.warning(f"  {symbol:<{width}} sin suficientes datos")
            return

        df_ind = indicators.add_all(df)
        live_price = float(df_ind["close"].iloc[-1])
        ind_snap = _indicator_snapshot(df_ind)
        if ind_snap is None:
            logger.warning(f"  {symbol:<{width}} indicadores en warmup")
            return

        logger.info(
            f"  {symbol:<{width}} @ {live_price:>10,.2f} | "
            f"RSI={ind_snap['rsi']:.1f}  MACD_hist={ind_snap['macd_hist']:+.1f}  "
            f"ADX={ind_snap['adx']:.0f}"
        )

        persist_candle(symbol, df_ind)

        # 1. Chequear SL/TP/Trailing
        trigger = executor.check_sl_tp(symbol, live_price)
        if trigger:
            closed = executor.close_trade(symbol, live_price, trigger)
            if closed:
                alerts.notify_trade_closed(symbol, closed.entry_price, live_price,
                                            closed.pnl_usdt or 0, closed.pnl_pct or 0,
                                            trigger)
                # Feature logging: registrar outcome del trade
                analyst.log_outcome(
                    trade_id=closed.id, symbol=symbol,
                    exit_reason=trigger,
                    pnl_pct=closed.pnl_pct or 0,
                    pnl_usdt=closed.pnl_usdt or 0,
                )
            analyst.log_cycle(live_price, ind_snap, strategy.evaluate(df_ind),
                              trade_action="CLOSED", symbol=symbol)
            return

        # 2. Evaluar señal — multi-timeframe si está habilitado
        if config.USE_MTF:
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

        if signal.type != "NONE":
            logger.info(
                f"  {symbol:<{width}} [{signal.type}] score={signal.score}/3 "
                f"trend={signal.trend} → {signal.reason}"
            )

        if signal.type == "BUY" and not executor.has_open_trade(symbol):
            # ── Cap global de exposición ──────────────────────────────────────
            exposure = executor.total_exposure_pct(capital)
            if exposure >= config.MAX_TOTAL_EXPOSURE_PCT:
                logger.warning(
                    f"  {symbol:<{width}} BUY bloqueado — exposición global "
                    f"{exposure*100:.1f}% ≥ {config.MAX_TOTAL_EXPOSURE_PCT*100:.0f}%"
                )
            elif capital > 10:
                trade = executor.open_trade(symbol, signal, capital)
                if trade:
                    trade_action = "OPENED"
                    acted = True
                    alerts.notify_trade_opened(symbol, trade.entry_price,
                                                trade.quantity, trade.stop_loss,
                                                trade.take_profit, signal.reason)
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
              and not config.DISABLE_SIGNAL_EXIT):
            # Cambio quirúrgico v2: SIGNAL solo cierra si hay pérdida REAL (con buffer)
            open_trade = executor.get_open_trade(symbol)
            entry = (open_trade.entry_price if open_trade else live_price) or live_price
            loss_threshold = entry * (1 - config.SIGNAL_EXIT_LOSS_BUFFER_PCT)
            in_real_loss = live_price < loss_threshold
            closed = None
            if not config.SIGNAL_EXIT_ONLY_IN_LOSS or in_real_loss:
                closed = executor.close_trade(symbol, live_price, "SIGNAL")
            if closed:
                trade_action = "CLOSED"
                acted = True
                alerts.notify_trade_closed(symbol, closed.entry_price, live_price,
                                            closed.pnl_usdt or 0, closed.pnl_pct or 0,
                                            "SIGNAL")
                # Feature logging: outcome
                analyst.log_outcome(
                    trade_id=closed.id, symbol=symbol,
                    exit_reason="SIGNAL",
                    pnl_pct=closed.pnl_pct or 0,
                    pnl_usdt=closed.pnl_usdt or 0,
                )

        persist_signal(symbol, signal, acted)
        analyst.log_cycle(live_price, ind_snap, signal,
                          trade_action=trade_action, symbol=symbol)

    except DataFetchError as e:
        logger.warning(f"  [{symbol}] Error temporal de datos: {e}")
    except Exception as e:
        logger.error(f"  [{symbol}] Error inesperado: {e}", exc_info=True)


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
        logger.info("⏸  Bot pausado (existe data/.paused) — saltando ciclo.")
        return

    logger.info("─" * 55)

    # ── Circuit breaker ───────────────────────────────────────────────────────
    # Evalúa condiciones de riesgo antes de operar. Si se activa, pausa el bot
    # tocando PAUSE_FILE (igual que /api/pause) y notifica por alerts.
    breaker = get_breaker(initial_capital)
    should_pause, cb_reason = breaker.check()
    if should_pause:
        logger.critical(f"🚨 CIRCUIT BREAKER activado: {cb_reason}")
        alerts.notify_error(f"🚨 Circuit breaker: {cb_reason}\nBot pausado automáticamente.")
        PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FILE.touch()
        return

    # ── Reconciliación: DB vs exchange ────────────────────────────────────────
    # Verifica que los trades "abiertos" en DB coincidan con posiciones reales.
    # Solo loguea — no cierra posiciones automáticamente.
    reconciler.reconcile_all(executor)

    # ── Crypto ───────────────────────────────────────────────────────────────
    try:
        balance = data_fetcher.fetch_balance()
        usdt_free = float(balance.get("free", {}).get("USDT", 0))
        logger.info(f"[CRYPTO] Balance USDT: {usdt_free:,.2f}")
        for symbol in config.CRYPTO_SYMBOLS:
            process_market(symbol, executor, usdt_free, data_fetcher, "CRYPTO")
    except DataFetchError as e:
        logger.warning(f"[CRYPTO] No se pudo obtener balance: {e}")

    # ── Stocks ───────────────────────────────────────────────────────────────
    try:
        if alpaca_fetcher.is_market_open():
            bal = alpaca_fetcher.fetch_balance()
            usd_free = bal["free"]["USD"]
            logger.info(f"[STOCKS] Mercado abierto | Cash: ${usd_free:,.2f}")
            for symbol in config.STOCK_SYMBOLS:
                process_market(symbol, executor, usd_free, alpaca_fetcher, "STOCK")
        else:
            logger.info("[STOCKS] Mercado cerrado — esperando apertura (9:30 ET)")
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
    logger.info(f"Bot iniciando — modo: {config.TRADING_MODE.upper()}")
    logger.info(f"Crypto: {', '.join(config.CRYPTO_SYMBOLS)}")
    logger.info(f"Stocks: {', '.join(config.STOCK_SYMBOLS)} (solo horario mercado)")
    logger.info(f"Timeframe: {config.TIMEFRAME}")
    logger.info(f"Filtro tendencia: {'ACTIVO' if config.USE_TREND_FILTER else 'inactivo'}")
    logger.info("=" * 55)

    init_db()
    executor = TradeExecutor()
    consecutive_fatal = 0

    # Capital inicial para el circuit breaker (crypto USDT + stocks USD)
    try:
        _cb_capital = float(data_fetcher.fetch_balance().get("free", {}).get("USDT", 10_000))
    except Exception:
        _cb_capital = 10_000.0
    logger.info(f"Circuit breaker inicializado con capital base: {_cb_capital:,.2f} USDT")

    while True:
        try:
            run_cycle(executor, initial_capital=_cb_capital)
            consecutive_fatal = 0
        except KeyboardInterrupt:
            logger.info("Bot detenido por el usuario.")
            break
        except FatalDataFetchError as e:
            consecutive_fatal += 1
            logger.error(f"⚠ Error fatal #{consecutive_fatal}/{MAX_CONSECUTIVE_ERRORS}: {e}")
            alerts.notify_error(f"Error fatal de datos: {e}")
            if consecutive_fatal >= MAX_CONSECUTIVE_ERRORS:
                logger.critical("KILL-SWITCH ACTIVADO — demasiados errores fatales. Deteniendo bot.")
                alerts.notify_error("🚨 KILL-SWITCH: bot detenido por errores fatales")
                break
        except Exception as e:
            logger.error(f"Error en ciclo: {e}", exc_info=True)

        logger.info(f"Esperando {config.LOOP_INTERVAL_SECONDS // 60} min...\n")
        time.sleep(config.LOOP_INTERVAL_SECONDS)


def main():
    """Punto de entrada para ejecución directa (CLI / desarrollo local)."""
    run_bot_forever()


if __name__ == "__main__":
    main()
