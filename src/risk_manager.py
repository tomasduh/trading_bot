"""
Gestión de riesgo: calcula tamaño de posición y niveles de SL/TP.

Regla base: arriesgar RISK_PER_TRADE (1%) del capital por cada trade.
  quantity = (capital * risk_pct) / (entry_price * stop_loss_pct)

Ejemplo:
  capital = 1000 USDT, risk_pct = 0.01, entry = 50000, sl_pct = 0.02
  → quantity = (1000 * 0.01) / (50000 * 0.02) = 10 / 1000 = 0.001 BTC
"""
from src import config


# Límites por mercado.
#   - Crypto (Binance spot): qty step y minNotional aprox.
#   - Stocks (Alpaca paper): 1 share entero (paper acepta fracciones pero por
#     simplicidad y para evitar errores en órdenes market, usamos enteros).
# Estos defaults se pueden refinar con exchange.markets[symbol]['limits'].
_SYMBOL_LIMITS = {
    "BTC/USDT":   {"step": 0.00001, "min_qty": 0.00001, "min_notional": 5.0, "price_decimals": 2},
    "ETH/USDT":   {"step": 0.0001,  "min_qty": 0.0001,  "min_notional": 5.0, "price_decimals": 2},
    "SOL/USDT":   {"step": 0.001,   "min_qty": 0.001,   "min_notional": 5.0, "price_decimals": 3},
    "BNB/USDT":   {"step": 0.001,   "min_qty": 0.001,   "min_notional": 5.0, "price_decimals": 2},
    "XRP/USDT":   {"step": 0.1,     "min_qty": 0.1,     "min_notional": 5.0, "price_decimals": 4},
    # Agregados (hybrid_C sprint)
    "ADA/USDT":   {"step": 0.1,     "min_qty": 0.1,     "min_notional": 5.0, "price_decimals": 4},
    "DOT/USDT":   {"step": 0.01,    "min_qty": 0.01,    "min_notional": 5.0, "price_decimals": 3},
    "AVAX/USDT":  {"step": 0.01,    "min_qty": 0.01,    "min_notional": 5.0, "price_decimals": 2},
    "LINK/USDT":  {"step": 0.01,    "min_qty": 0.01,    "min_notional": 5.0, "price_decimals": 3},
    "POL/USDT":   {"step": 0.1,     "min_qty": 0.1,     "min_notional": 5.0, "price_decimals": 4},
}
# Stocks: shares enteras
_STOCK_DEFAULT = {"step": 1.0, "min_qty": 1.0, "min_notional": 1.0, "price_decimals": 2}


def _get_limits(symbol: str) -> dict:
    if "/" in symbol:
        return _SYMBOL_LIMITS.get(symbol, {"step": 0.001, "min_qty": 0.001,
                                          "min_notional": 5.0, "price_decimals": 2})
    return _STOCK_DEFAULT


def _floor_to_step(qty: float, step: float) -> float:
    """Trunca qty al múltiplo más cercano de step (hacia abajo).
    Usa epsilon para evitar bugs de floating point (0.01/0.00001 = 999.999...)"""
    if step <= 0:
        return qty
    eps = step * 1e-9
    n = int((qty + eps) / step)
    return n * step


def position_size(capital_usdt: float, entry_price: float, symbol: str | None = None) -> float:
    """Cantidad a comprar respetando los límites del símbolo (step, min_qty, min_notional)."""
    risk_usdt = capital_usdt * config.RISK_PER_TRADE
    loss_per_unit = entry_price * config.STOP_LOSS_PCT
    if loss_per_unit <= 0:
        return 0.0
    qty_raw = risk_usdt / loss_per_unit

    # Cap adicional: ningún trade individual compromete más de
    # MAX_POSITION_PCT_OF_CAPITAL del capital, sin importar el ratio riesgo/stop.
    if entry_price > 0:
        max_qty_by_capital = (capital_usdt * config.MAX_POSITION_PCT_OF_CAPITAL) / entry_price
        qty_raw = min(qty_raw, max_qty_by_capital)

    limits = _get_limits(symbol) if symbol else _SYMBOL_LIMITS["BTC/USDT"]
    step       = limits["step"]
    min_qty    = limits["min_qty"]
    min_notional = limits["min_notional"]

    qty = _floor_to_step(qty_raw, step)
    qty = max(qty, min_qty)

    # Verificar minNotional: si qty * price < min_notional, qty queda en 0 (no operable)
    if qty * entry_price < min_notional:
        return 0.0

    # Precisión decimal según step (ej: step=0.001 → 3 decimales)
    decimals = max(0, -int(round(_log10_step(step))))
    return round(qty, decimals)


def _log10_step(step: float) -> float:
    import math
    return math.log10(step) if step > 0 else 0.0


def _round_price(price: float, symbol: str | None = None) -> float:
    decimals = _get_limits(symbol)["price_decimals"] if symbol else 2
    return round(price, decimals)


def stop_loss_price(entry_price: float, symbol: str | None = None) -> float:
    return _round_price(entry_price * (1 - config.STOP_LOSS_PCT), symbol)


def take_profit_price(entry_price: float, symbol: str | None = None) -> float:
    return _round_price(entry_price * (1 + config.TAKE_PROFIT_PCT), symbol)


def stop_loss_price_atr(entry_price: float, atr: float,
                        symbol: str | None = None) -> float:
    """SL dinámico: entry − ATR_MULTIPLIER_SL × ATR(14).

    Ventaja vs % fijo: en mercados volátiles el SL se ensancha naturalmente
    y no se activa por ruido normal; en mercados tranquilos se estrecha.
    El ratio SL:TP se mantiene en 1:2 con take_profit_price_atr.
    """
    sl = entry_price - config.ATR_MULTIPLIER_SL * atr
    return _round_price(max(sl, entry_price * 0.001), symbol)  # mínimo safety


def take_profit_price_atr(entry_price: float, atr: float,
                          symbol: str | None = None) -> float:
    """TP dinámico: entry + ATR_MULTIPLIER_TP × ATR(14). Ratio 2:1 por defecto."""
    tp = entry_price + config.ATR_MULTIPLIER_TP * atr
    return _round_price(tp, symbol)


def get_sl_tp(entry_price: float, atr: float = 0.0,
              symbol: str | None = None) -> tuple[float, float]:
    """Helper unificado: devuelve (sl, tp) según USE_ATR_STOPS."""
    if config.USE_ATR_STOPS and atr > 0:
        return (stop_loss_price_atr(entry_price, atr, symbol),
                take_profit_price_atr(entry_price, atr, symbol))
    return (stop_loss_price(entry_price, symbol),
            take_profit_price(entry_price, symbol))


def check_stop_loss(entry_price: float, current_price: float) -> bool:
    return current_price <= stop_loss_price(entry_price)


def check_take_profit(entry_price: float, current_price: float) -> bool:
    return current_price >= take_profit_price(entry_price)


def pnl(entry_price: float, exit_price: float, quantity: float,
        fee_pct: float = 0.0) -> tuple[float, float]:
    """Devuelve (pnl_usdt, pnl_pct) neto de fees.

    fee_pct se aplica sobre el notional de entrada Y salida (round-trip):
      total_fees = (entry_price + exit_price) * quantity * fee_pct

    En producción (executor.py) fee_pct=0 porque el exchange ya lo descuenta.
    En backtest se pasa config.FEE_PCT[symbol] + config.SLIPPAGE_PCT para
    simular el coste real de cada trade.
    """
    gross_pnl = (exit_price - entry_price) * quantity
    fees      = (entry_price + exit_price) * quantity * fee_pct
    net_pnl   = gross_pnl - fees
    notional  = entry_price * quantity
    net_pct   = net_pnl / notional if notional > 0 else 0.0
    return round(net_pnl, 4), round(net_pct, 6)


def trailing_stop_price(entry_price: float, highest_price: float) -> float | None:
    """
    Devuelve el precio de trailing stop si está activado, o None si aún no.
    Se activa cuando el precio máximo alcanzado supera entrada + TRAILING_ACTIVATE_PCT.
    Una vez activo, el SL queda a (highest - TRAILING_DISTANCE_PCT).
    """
    if not config.USE_TRAILING_STOP:
        return None
    activation_price = entry_price * (1 + config.TRAILING_ACTIVATE_PCT)
    if highest_price < activation_price:
        return None
    return round(highest_price * (1 - config.TRAILING_DISTANCE_PCT), 2)


def check_trailing_stop(entry_price: float, highest_price: float,
                        current_price: float) -> bool:
    ts = trailing_stop_price(entry_price, highest_price)
    if ts is None:
        return False
    return current_price <= ts
