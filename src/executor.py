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
        if config.API_KEY:
            try:
                order = data_fetcher.create_market_order(symbol, "buy", qty)
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

        if config.API_KEY:
            try:
                data_fetcher.create_market_order(symbol, "sell", trade.quantity)
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

        if risk_manager.check_stop_loss(trade.entry_price, current_price):
            return "STOP_LOSS"
        if risk_manager.check_take_profit(trade.entry_price, current_price):
            return "TAKE_PROFIT"
        if risk_manager.check_trailing_stop(trade.entry_price,
                                             trade.highest_price or trade.entry_price,
                                             current_price):
            return "TRAILING_STOP"
        return None
