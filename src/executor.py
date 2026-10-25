"""
Ejecuta órdenes y gestiona el estado de trades abiertos por símbolo.
Toda acción queda registrada en la base de datos.
"""
import logging
from datetime import datetime, timezone
from src import config, risk_manager, data_fetcher, alerts
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


def _stock_fractional(symbol: str) -> bool:
    """¿Se puede comprar este stock en fracciones? (config + asset fractionable en Alpaca)"""
    if "/" in symbol or not config.STOCK_FRACTIONAL or not _has_api_credentials_for(symbol):
        return False
    try:
        return bool(_get_fetcher_for(symbol).is_fractionable(symbol))
    except Exception as e:
        logger.warning(f"[{symbol}] No se pudo verificar si es fraccionable: {e} — acciones enteras")
        return False


def _avg_price(order: dict) -> float:
    """Precio promedio de ejecución reportado por el exchange (0 si no vino)."""
    try:
        return float(order.get("average") or 0)
    except (TypeError, ValueError):
        return 0.0


def _buy_fill(symbol: str, order: dict, fallback_price: float,
              fallback_qty: float) -> tuple[float, float]:
    """(precio, cantidad neta) realmente ejecutados en un BUY.

    Binance cobra el fee de una compra en el activo comprado (salvo que se pague
    con BNB): si compramos 0.01 BTC recibimos 0.01 − fee. Guardar 0.01 haría
    fallar el SELL por saldo insuficiente, así que se descuenta y se redondea al step.
    """
    price = _avg_price(order) or fallback_price
    try:
        qty = float(order.get("filled") or 0) or fallback_qty
    except (TypeError, ValueError):
        qty = fallback_qty
    if "/" in symbol:
        base = symbol.split("/")[0]
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        fee_in_base = sum(float(f.get("cost") or 0) for f in fees
                          if f and f.get("currency") == base)
        if fee_in_base:
            step = risk_manager._get_limits(symbol)["step"]
            qty = risk_manager._floor_to_step(qty - fee_in_base, step)
    return price, qty


class TradeExecutor:
    def __init__(self):
        # Un trade abierto por símbolo: {"BTC/USDT": Trade | None, ...}
        self._open_trades: dict[str, Trade | None] = {}
        # Intentos fallidos de cierre por símbolo (para no spamear alertas)
        self._close_failures: dict[str, int] = {}
        # Símbolos cuya OCO no se pudo colocar (para alertar una sola vez)
        self._protect_failures: set[str] = set()
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

    def open_symbols(self, stocks: bool) -> list[str]:
        """Símbolos (stocks o crypto) con trade abierto, estén o no en config."""
        return [t.symbol for t in self._open_trades_of(stocks)]

    def legacy_open_symbols(self, stocks: bool) -> list[str]:
        """Símbolos con trade abierto que ya no están en config (quitados del
        universo). Hay que seguir chequeando su SL/TP para poder cerrarlos."""
        configured = set(config.CRYPTO_SYMBOLS) | set(config.STOCK_SYMBOLS)
        return [s for s in self.open_symbols(stocks) if s not in configured]

    def has_open_trade(self, symbol: str) -> bool:
        return self._open_trades.get(symbol) is not None

    def get_open_trade(self, symbol: str) -> Trade | None:
        return self._open_trades.get(symbol)

    def _open_trades_of(self, stocks: bool) -> list[Trade]:
        return [t for s, t in self._open_trades.items()
                if t is not None and ("/" not in s) == stocks]

    def open_notional(self, stocks: bool) -> float:
        """Nocional (a precio de entrada) de las posiciones abiertas de un mercado."""
        return sum((t.entry_price or 0) * (t.quantity or 0) for t in self._open_trades_of(stocks))

    def total_exposure_pct(self, free_cash: float, stocks: bool) -> float:
        """
        % del capital de UN mercado (crypto en Binance o stocks en Alpaca) en riesgo
        en sus trades abiertos. Cada mercado es una cuenta distinta: mezclarlos
        hacía que el riesgo de los stocks (~$70k) dividido por el USDT libre
        bloqueara las entradas crypto.

        Riesgo por trade = (entry_price - stop_loss) × quantity
        → es el importe máximo que se perdería si todos los SL se activan hoy.
        Capital del mercado = cash libre + nocional abierto (a precio de entrada):
        así no se infla a medida que el cash se invierte.

        Se usa para bloquear nuevas entradas cuando la exposición
        supera MAX_TOTAL_EXPOSURE_PCT (config.py).
        """
        capital = free_cash + self.open_notional(stocks)
        if capital <= 0:
            return 0.0
        open_risk = sum(
            max(0.0, (t.entry_price - (t.stop_loss or 0)) * (t.quantity or 0))
            for t in self._open_trades_of(stocks)
        )
        return open_risk / capital

    # ── Apertura ──────────────────────────────────────────────────────────────

    def open_trade(self, symbol: str, signal: Signal, capital_usdt: float) -> Trade | None:
        if self.has_open_trade(symbol):
            logger.warning(f"[{symbol}] Ya hay trade abierto. Ignorando BUY.")
            return None

        qty = risk_manager.position_size(capital_usdt, signal.price, symbol,
                                         fractional=_stock_fractional(symbol))
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
        entry_price = signal.price
        if _has_api_credentials_for(symbol):
            fetcher = _get_fetcher_for(symbol)
            try:
                order = fetcher.create_market_order(symbol, "buy", qty)
            except Exception as e:
                logger.error(f"[{symbol}] Error al ejecutar orden: {e}")
                return None
            entry_price, qty = _buy_fill(symbol, order, signal.price, qty)
            # SL/TP relativos al precio real de ejecución, no al de la señal
            sl, tp = risk_manager.get_sl_tp(entry_price, atr, symbol)
            if abs(entry_price - signal.price) / signal.price > 0.001:
                logger.info(f"[{symbol}] Fill @ {entry_price:,.4f} (señal {signal.price:,.4f}) "
                            f"qty={qty} | SL={sl:,.2f} TP={tp:,.2f}")

        trade = Trade(
            symbol=symbol,
            side="BUY",
            status="OPEN",
            entry_price=entry_price,
            quantity=qty,
            stop_loss=sl,
            take_profit=tp,
            entry_time=datetime.now(timezone.utc),
            order_id=str(order.get("id", "simulated")),
            highest_price=entry_price,   # init para trailing stop
        )

        with get_session() as session:
            session.add(trade)
            session.commit()
            session.refresh(trade)
            session.expunge(trade)

        self._open_trades[symbol] = trade
        logger.info(f"[{symbol}] Trade #{trade.id} abierto.")
        self.ensure_protection(symbol)
        return trade

    # ── Protección del lado del exchange (OCO SL/TP) ──────────────────────────
    # El SL/TP vive como orden OCO en el exchange: se ejecuta aunque el bot esté
    # caído o pausado, y sin esperar al próximo ciclo. check_sl_tp del bot queda
    # como respaldo (OCO rechazada, o trailing stop que la OCO no sigue).

    def _set_protect_id(self, trade: Trade, order_id: str | None):
        trade.protect_order_id = order_id
        with get_session() as session:
            db_trade = session.get(Trade, trade.id)
            if db_trade:
                db_trade.protect_order_id = order_id
                session.commit()

    def ensure_protection(self, symbol: str, alert: bool = True):
        """Coloca la protección si el trade abierto no tiene una (trade nuevo, trades
        previos a esta feature, stop DAY vencido, o una OCO cancelada fuera del bot).
        alert=False: intento oportunista (antes de la apertura), un fallo solo se loguea."""
        trade = self._open_trades.get(symbol)
        if not trade or trade.protect_order_id or not _has_api_credentials_for(symbol):
            return
        sl = trade.stop_loss or risk_manager.stop_loss_price(trade.entry_price, symbol)
        tp = trade.take_profit or risk_manager.take_profit_price(trade.entry_price, symbol)
        try:
            order_id = _get_fetcher_for(symbol).place_protection(symbol, trade.quantity, sl, tp)
        except Exception as e:
            if not alert:
                logger.info(f"[{symbol}] Protección de #{trade.id} no colocada todavía: {e}")
                return
            if symbol not in self._protect_failures:   # avisar una vez, reintentar cada ciclo
                self._protect_failures.add(symbol)
                logger.warning(f"[{symbol}] No se pudo colocar la OCO de #{trade.id}: {e} "
                               f"— queda el SL/TP del bot como respaldo")
                alerts.notify_error(f"{symbol} #{trade.id} SIN stop en el exchange: {e}\n"
                                    f"El bot sigue vigilando SL/TP cada ciclo; se reintenta.")
            return
        if symbol in self._protect_failures:
            self._protect_failures.discard(symbol)
            alerts.notify_info(f"{symbol} #{trade.id} ya tiene su stop en el exchange.")
        self._set_protect_id(trade, order_id)
        logger.info(f"[{symbol}] 🛡 Protección #{order_id} en exchange | qty={trade.quantity} SL={sl:,.4f} TP={tp:,.4f}")

    def sync_protection(self, symbol: str) -> Trade | None:
        """Si la OCO se ejecutó en el exchange, registra el cierre con ese fill y
        devuelve el trade cerrado. Si alguien la canceló, la olvida para reponerla."""
        trade = self._open_trades.get(symbol)
        if not trade or not trade.protect_order_id:
            return None
        try:
            status = _get_fetcher_for(symbol).get_protection_status(symbol, trade.protect_order_id)
        except Exception as e:
            logger.warning(f"[{symbol}] No se pudo consultar la OCO #{trade.protect_order_id}: {e}")
            return None
        if status["state"] == "filled":
            return self._record_close(trade, status["exit_price"], status["reason"])
        if status["state"] == "expired":
            # Stop DAY de una fracción al cierre (o GTC a los 90 días): esperado
            logger.info(f"[{symbol}] Protección #{trade.protect_order_id} vencida — se renueva")
            self._set_protect_id(trade, None)
        elif status["state"] == "canceled":
            logger.warning(f"[{symbol}] Protección #{trade.protect_order_id} cancelada fuera del bot — se repone")
            self._set_protect_id(trade, None)
        return None

    # ── Cierre ────────────────────────────────────────────────────────────────

    def close_trade(self, symbol: str, exit_price: float, reason: str) -> Trade | None:
        trade = self._open_trades.get(symbol)
        if not trade:
            return None

        if _has_api_credentials_for(symbol):
            fetcher = _get_fetcher_for(symbol)
            try:
                if trade.protect_order_id:
                    # La OCO retiene las unidades: hay que cancelarla antes de vender.
                    status = fetcher.cancel_protection(symbol, trade.protect_order_id)
                    if status["state"] == "filled":
                        # El exchange cerró primero: vale su fill, no hay nada que vender
                        return self._record_close(trade, status["exit_price"], status["reason"])
                    if status["state"] == "open":
                        raise RuntimeError(f"la OCO #{trade.protect_order_id} no se pudo cancelar")
                    # Si el SELL falla, ensure_protection repone la OCO el próximo ciclo
                    self._set_protect_id(trade, None)
                order = fetcher.create_market_order(symbol, "sell", trade.quantity)
            except Exception as e:
                # La posición sigue abierta en el exchange: NO marcarla CLOSED en la
                # DB (quedaría sin stop y sin que nadie la vea). check_sl_tp vuelve a
                # disparar el próximo ciclo y se reintenta.
                n = self._close_failures.get(symbol, 0) + 1
                self._close_failures[symbol] = n
                logger.error(f"[{symbol}] Error al cerrar #{trade.id} ({reason}), intento {n}: {e} "
                             f"— sigue OPEN, se reintenta el próximo ciclo")
                if n == 1 or n % 6 == 0:   # primera vez y luego ~cada hora
                    alerts.notify_error(
                        f"No se pudo cerrar {symbol} #{trade.id} ({reason}), intento {n}: {e}\n"
                        f"La posición sigue ABIERTA en el exchange; se reintenta cada ciclo."
                    )
                return None
            exit_price = _avg_price(order) or exit_price

        return self._record_close(trade, exit_price, reason)

    def _record_close(self, trade: Trade, exit_price: float, reason: str) -> Trade:
        """Marca el trade CLOSED en DB con el precio de salida real."""
        symbol = trade.symbol
        if self._close_failures.pop(symbol, 0):
            alerts.notify_info(f"{symbol} #{trade.id} cerrado tras reintentos.")
        # Fees: Alpaca 0; Binance ~0.1% por lado (estimado de config, igual que el backtest)
        pnl_usdt, pnl_pct = risk_manager.pnl(trade.entry_price, exit_price, trade.quantity,
                                             fee_pct=config.FEE_PCT.get(symbol, 0.0))
        emoji = "✅" if pnl_usdt >= 0 else "❌"
        logger.info(
            f"[{symbol}] {emoji} Cerrando #{trade.id} @ {exit_price:,.2f} | "
            f"{reason} | PnL: {pnl_usdt:+.2f} USDT ({pnl_pct*100:+.2f}%)"
        )

        with get_session() as session:
            db_trade = session.get(Trade, trade.id)
            db_trade.status     = "CLOSED"
            db_trade.exit_price = exit_price
            db_trade.exit_time  = datetime.now(timezone.utc)
            db_trade.pnl_usdt   = pnl_usdt
            db_trade.pnl_pct    = pnl_pct
            db_trade.exit_reason = reason
            db_trade.protect_order_id = None
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
