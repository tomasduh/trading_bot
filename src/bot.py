"""
Loop principal del bot con kill-switch, pause/resume y manejo robusto de errores.
Ejecutar con: python -m src.bot
"""
import time
import logging
from pathlib import Path
from datetime import datetime, timezone

from src import config, data_fetcher, strategy, logger as log_setup
from src.database import init_db, Candle, Signal as DbSignal, get_session
from src.executor import TradeExecutor
from src import indicators, analyst, alerts, reconciler
from src import alpaca_fetcher
from src.data_fetcher import DataFetchError, FatalDataFetchError

log_setup.setup("bot")
logger = logging.getLogger("bot")

# ── Kill-switch ───────────────────────────────────────────────────────────────
MAX_CONSECUTIVE_ERRORS = 5
PAUSE_FILE = config.BASE_DIR / "data" / ".paused"


def is_paused() -> bool:
    return PAUSE_FILE.exists()


def persist_candle(symbol: str, df_ind):
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
    with get_session() as session:
        session.add(candle)
        session.commit()


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
        "ema_fast":    float(last["ema_fast"]),
        "ema_slow":    float(last["ema_slow"]),
        "rsi":         float(last["rsi"]),
        "macd":        float(last["macd"]),
        "macd_signal": float(last["macd_signal"]),
        "macd_hist":   float(last["macd_hist"]),
        "bb_upper":    float(last["bb_upper"]),
        "bb_mid":      float(last["bb_mid"]),
        "bb_lower":    float(last["bb_lower"]),
        "adx":         float(last.get("adx", 0) or 0),
        "ema_trend_fast": float(last.get("ema_trend_fast", 0) or 0),
        "ema_trend_slow": float(last.get("ema_trend_slow", 0) or 0),
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
            analyst.log_cycle(live_price, ind_snap, strategy.evaluate(df_ind),
                              trade_action="CLOSED", symbol=symbol)
            return

        # 2. Evaluar señal y actuar
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
            if capital > 10:
                trade = executor.open_trade(symbol, signal, capital)
                if trade:
                    trade_action = "OPENED"
                    acted = True
                    alerts.notify_trade_opened(symbol, trade.entry_price,
                                                trade.quantity, trade.stop_loss,
                                                trade.take_profit, signal.reason)
            else:
                logger.warning(f"  {symbol:<{width}} Balance insuficiente.")

        elif (signal.type == "SELL"
              and executor.has_open_trade(symbol)
              and not config.DISABLE_SIGNAL_EXIT):
            # Cambio quirúrgico v2: SIGNAL solo cierra si hay pérdida REAL (con buffer)
            # Evita cortes por tick rojo cuando el trade está esencialmente breakeven
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


def run_cycle(executor: TradeExecutor):
    if is_paused():
        logger.info("⏸  Bot pausado (existe data/.paused) — saltando ciclo.")
        return

    logger.info("─" * 55)

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


def main():
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

    while True:
        try:
            run_cycle(executor)
            consecutive_fatal = 0  # reset al completar un ciclo OK
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


if __name__ == "__main__":
    main()
