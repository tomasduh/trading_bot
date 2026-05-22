"""
Ejecuta órdenes y gestiona el estado de trades abiertos por símbolo.
Toda acción queda registrada en la base de datos.
"""
import logging
from datetime import datetime, timezone
from src import config, risk_manager, data_fetcher
from src.database import Trade, get_session
from src.strategy import Signal

logger = logging.getLogger("bot")


def _get_fetcher_for(symbol: str):
    """Devuelve el fetcher correcto según el tipo de símbolo.

    Crypto (contiene '/') → src.data_fetcher (Binance)
    Stocks (sin '/')      → src.alpaca_fetcher (Alpaca)
    """
    if "/" in symbol:
        return data_fetcher
    # Import perezoso para no romper si Alpaca no está configurado en entornos de test
    from src import alpaca_fetcher
    return alpaca_fetcher


def _has_api_credentials_for(symbol: str) -> bool:
    """¿Hay credenciales válidas para operar este símbolo?"""
    if "/" in symbol:
        return bool(config.API_KEY)
    # Stocks: chequea credenciales de Alpaca
    import os
    return bool(os.getenv("ALPACA_API_KEY", "").strip())


class TradeExecutor:
    def __init__(self):
        # Un trade abierto por símbolo: {"BTC/USDT": Trade | None, ...}
        self._open_trades: dict[str, Trade | None] = {}
        self._load_open_trades()

    def _load_open_trades(self):
        """Recupera todos los trades abiertos de la DB (útil si el bot se reinicia)."""
        all_symbols = list(config.CRYPTO_SYMBOLS) + list(config.STOCK_SYMBOLS)
        with get_session() as session:
            open_trades = session.query(Trade).filter(Trade.status == "OPEN").all()
            for t in open_trades:
                session.expunge(t)
            # Inicializa todos los símbolos a None
            for symbol in all_symbols:
                self._open_trades[symbol] = None
            # Llena los que tienen trade abierto (incluye símbolos que ya no estén
            # en config — los conservamos para poder cerrarlos)
            for t in open_trades:
                self._open_trades[t.symbol] = t

    def has_open_trade(self, symbol: str) -> bool:
        return self._open_trades.get(symbol) is not None

    def get_open_trade(self, symbol: str) -> Trade | None:
        return self._open_trades.get(symbol)

    def total_exposure_pct(self, capital: float) -> float:
        """
        Calcula el % del capital actualmente en riesgo en todos los trades abiertos.

        Riesgo por trade = (entry_price - stop_loss) × quantity
        → es el importe máximo que se perdería si todos los SL se activan hoy.

        Se usa para bloquear nuevas entradas cuando la exposición global
        supera MAX_TOTAL_EXPOSURE_PCT (config.py).
        """
        if capital <= 0:
            return 0.0
        open_risk = sum(
            max(0.0, (t.entry_price - (t.stop_loss or 0)) * (t.quantity or 0))
            for t in self._open_trades.values() if t
        )
        return open_risk / capital

    # ── Apertura ──────────────────────────────────────────────────────────────

    def open_trade(self, symbol: str, signal: Signal, capital_usdt: float) -> Trade | None:
        if self.has_open_trade(symbol):
            logger.warning(f"[{symbol}] Ya hay trade abierto. Ignorando BUY.")
            return None

        qty = risk_manager.position_size(capital_usdt, signal.price, symbol)
        atr = getattr(signal, "atr", 0.0) or 0.0
        sl, tp = risk_manager.get_sl_tp(signal.price, atr, symbol)

        if qty <= 0:
            logger.warning(
                f"[{symbol}] Qty calculada = 0 (capital {capital_usdt:.2f}, "
                f"price {signal.price}, min_notional no alcanzado). Ignorando BUY."
            )
            return None

        logger.info(f"[{symbol}] Abriendo BUY {qty} @ {signal.price:,.2f} | SL={sl:,.2f} TP={tp:,.2f}")

        order = {}
        if _has_api_credentials_for(symbol):
            fetcher = _get_fetcher_for(symbol)
            try:
                order = fetcher.create_market_order(symbol, "buy", qty)
            except Exception as e:
                logger.error(f"[{symbol}] Error al ejecutar orden: {e}")
                return None

        trade = Trade(
            symbol=symbol,
            side="BUY",
            status="OPEN",
            entry_price=signal.price,
            quantity=qty,
            stop_loss=sl,
            take_profit=tp,
            entry_time=datetime.now(timezone.utc),
            order_id=str(order.get("id", "simulated")),
            highest_price=signal.price,   # init para trailing stop
        )

        with get_session() as session:
            session.add(trade)
            session.commit()
            session.refresh(trade)
            session.expunge(trade)

        self._open_trades[symbol] = trade
        logger.info(f"[{symbol}] Trade #{trade.id} abierto.")
        return trade

    # ── Cierre ────────────────────────────────────────────────────────────────

    def close_trade(self, symbol: str, exit_price: float, reason: str) -> Trade | None:
        trade = self._open_trades.get(symbol)
        if not trade:
            return None

        pnl_usdt, pnl_pct = risk_manager.pnl(trade.entry_price, exit_price, trade.quantity)
        emoji = "✅" if pnl_usdt >= 0 else "❌"
        logger.info(
            f"[{symbol}] {emoji} Cerrando #{trade.id} @ {exit_price:,.2f} | "
            f"{reason} | PnL: {pnl_usdt:+.2f} USDT ({pnl_pct*100:+.2f}%)"
        )

        if _has_api_credentials_for(symbol):
            fetcher = _get_fetcher_for(symbol)
            try:
                fetcher.create_market_order(symbol, "sell", trade.quantity)
            except Exception as e:
                logger.error(f"[{symbol}] Error al cerrar orden: {e}")

        with get_session() as session:
            db_trade = session.get(Trade, trade.id)
            db_trade.status     = "CLOSED"
            db_trade.exit_price = exit_price
            db_trade.exit_time  = datetime.now(timezone.utc)
            db_trade.pnl_usdt   = pnl_usdt
            db_trade.pnl_pct    = pnl_pct
            db_trade.exit_reason = reason
            session.commit()
            session.refresh(db_trade)
            session.expunge(db_trade)
            closed = db_trade

        self._open_trades[symbol] = None
        return closed

    # ── SL/TP/Trailing ────────────────────────────────────────────────────────

    def _update_highest_price(self, symbol: str, current_price: float):
        """Actualiza el precio máximo alcanzado para trailing stop."""
        trade = self._open_trades.get(symbol)
        if not trade:
            return
        prev_high = trade.highest_price or trade.entry_price
        if current_price > prev_high:
            trade.highest_price = current_price
            with get_session() as session:
                db_trade = session.get(Trade, trade.id)
                if db_trade:
                    db_trade.highest_price = current_price
                    session.commit()

    def check_sl_tp(self, symbol: str, current_price: float) -> str | None:
        trade = self._open_trades.get(symbol)
        if not trade:
            return None

        # Actualizar highest price (para trailing stop) ANTES de chequear
        self._update_highest_price(symbol, current_price)
        # Refrescar la referencia tras update
        trade = self._open_trades.get(symbol)

        sl = trade.stop_loss or risk_manager.stop_loss_price(trade.entry_price, symbol)
        tp = trade.take_profit or risk_manager.take_profit_price(trade.entry_price, symbol)
        if current_price <= sl:
            return "STOP_LOSS"
        if current_price >= tp:
            return "TAKE_PROFIT"
        if risk_manager.check_trailing_stop(trade.entry_price,
                                             trade.highest_price or trade.entry_price,
                                             current_price):
            return "TRAILING_STOP"
        return None
