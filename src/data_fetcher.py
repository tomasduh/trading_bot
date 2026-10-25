"""
Wrapper de ccxt para Binance con timeouts y manejo robusto de errores.
ccxt se importa de forma lazy (primera llamada) para ahorrar ~40MB de RAM al startup.
"""
import logging
import pandas as pd
from src import config

logger = logging.getLogger("bot")

REQUEST_TIMEOUT_MS = 15_000

_exchange = None


def _get_exchange():
    global _exchange
    if _exchange is None:
        import ccxt
        ex = ccxt.binance({
            "apiKey":          config.API_KEY,
            "secret":          config.API_SECRET,
            "enableRateLimit": True,
            "timeout":         REQUEST_TIMEOUT_MS,
            "options": {
                "defaultType":             "spot",
                "fetchCurrencies":         False,
                "adjustForTimeDifference": True,
            },
        })
        if config.IS_TESTNET:
            ex.set_sandbox_mode(True)
        _exchange = ex
    return _exchange


# ── Errores específicos para que bot.py los capture ──────────────────────────
class DataFetchError(Exception):
    """Error temporal de red/exchange. El bot puede reintentar."""


class FatalDataFetchError(Exception):
    """Error grave (auth, IP bloqueada). El bot debe detenerse."""


def _wrap_call(fn, *args, **kwargs):
    import ccxt as _ccxt
    try:
        return fn(*args, **kwargs)
    except (_ccxt.NetworkError, _ccxt.RequestTimeout, _ccxt.ExchangeNotAvailable,
            _ccxt.DDoSProtection, _ccxt.RateLimitExceeded) as e:
        logger.warning("Binance error temporal: %s", e)
        raise DataFetchError(str(e)) from e
    except (_ccxt.AuthenticationError, _ccxt.PermissionDenied) as e:
        logger.error("Binance error fatal (auth): %s", e)
        raise FatalDataFetchError(str(e)) from e
    except _ccxt.ExchangeError as e:
        msg = str(e)
        if "451" in msg or "restricted location" in msg.lower():
            logger.error("Binance bloqueó la IP del servidor: %s", e)
            raise FatalDataFetchError(msg) from e
        logger.warning("Binance error: %s", e)
        raise DataFetchError(msg) from e


def fetch_ohlcv(symbol: str = config.SYMBOL,
                timeframe: str = config.TIMEFRAME,
                limit: int = config.CANDLES_LIMIT) -> pd.DataFrame:
    effective_limit = limit if timeframe == config.TIMEFRAME else max(100, limit)
    raw = _wrap_call(_get_exchange().fetch_ohlcv, symbol, timeframe=timeframe,
                     limit=effective_limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp").sort_index()
    return df


def fetch_balance() -> dict:
    return _wrap_call(_get_exchange().fetch_balance)


def fetch_ticker(symbol: str = config.SYMBOL) -> dict:
    return _wrap_call(_get_exchange().fetch_ticker, symbol)


def fetch_open_orders(symbol: str = config.SYMBOL) -> list:
    return _wrap_call(_get_exchange().fetch_open_orders, symbol)


def create_market_order(symbol: str, side: str, amount: float) -> dict:
    """side: 'buy' | 'sell'"""
    return _wrap_call(_get_exchange().create_market_order, symbol, side, amount)


def create_limit_order(symbol: str, side: str, amount: float, price: float) -> dict:
    return _wrap_call(_get_exchange().create_limit_order, symbol, side, amount, price)


def cancel_order(order_id: str, symbol: str = config.SYMBOL) -> dict:
    return _wrap_call(_get_exchange().cancel_order, order_id, symbol)


# ── Protección del lado del exchange (OCO de venta) ──────────────────────────
# Arriba: LIMIT_MAKER al take-profit. Abajo: STOP_LOSS a mercado (no stop-limit)
# para que se ejecute aunque el precio salte por debajo del stop.

_TP_TYPES = {"LIMIT", "LIMIT_MAKER", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"}


def place_protection(symbol: str, qty: float, stop_price: float, take_profit: float) -> str:
    """Coloca una OCO de venta sobre una posición abierta. Devuelve el orderListId."""
    ex = _get_exchange()
    _wrap_call(ex.load_markets)
    base = ex.market(symbol)["base"]
    # Si el fee de la compra se cobró en el activo base, el saldo real es algo
    # menor que la cantidad registrada: protegemos lo que realmente hay.
    free_base = float((fetch_balance().get("free") or {}).get(base) or 0)
    if free_base > 0:
        qty = min(qty, free_base)
    params = {
        "symbol":         ex.market_id(symbol),
        "side":           "SELL",
        "quantity":       ex.amount_to_precision(symbol, qty),
        "aboveType":      "LIMIT_MAKER",
        "abovePrice":     ex.price_to_precision(symbol, take_profit),
        "belowType":      "STOP_LOSS",
        "belowStopPrice": ex.price_to_precision(symbol, stop_price),
    }
    res = _wrap_call(ex.privatePostOrderListOco, params)
    return str(res["orderListId"])


def _oco_status(orders: list[dict]) -> dict:
    """{"state": "open"|"filled"|"canceled", "reason", "exit_price"} a partir de
    las órdenes (formato raw de Binance) de una OCO ya terminada."""
    for o in orders:
        if o.get("status") == "FILLED":
            qty = float(o.get("executedQty") or 0)
            quote = float(o.get("cummulativeQuoteQty") or 0)
            price = quote / qty if qty else float(o.get("price") or 0)
            reason = "TAKE_PROFIT" if o.get("type") in _TP_TYPES else "STOP_LOSS"
            return {"state": "filled", "reason": reason, "exit_price": price}
    return {"state": "canceled"}


def get_protection_status(symbol: str, order_id: str) -> dict:
    ex = _get_exchange()
    lst = _wrap_call(ex.privateGetOrderList, {"orderListId": order_id})
    if lst.get("listOrderStatus") == "EXECUTING":
        return {"state": "open"}
    mid = ex.market_id(symbol)
    orders = [_wrap_call(ex.privateGetOrder, {"symbol": mid, "orderId": o["orderId"]})
              for o in lst.get("orders", [])]
    return _oco_status(orders)


def cancel_protection(symbol: str, order_id: str) -> dict:
    """Cancela la OCO (libera el saldo bloqueado) y devuelve su estado final.
    Si ya se había ejecutado, devuelve 'filled' con el precio del exchange."""
    ex = _get_exchange()
    try:
        _wrap_call(ex.privateDeleteOrderList,
                   {"symbol": ex.market_id(symbol), "orderListId": order_id})
    except DataFetchError as e:
        logger.info("[%s] cancelar OCO #%s: %s (¿ya ejecutada?)", symbol, order_id, e)
    return get_protection_status(symbol, order_id)
