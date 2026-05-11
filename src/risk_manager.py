"""
Gestión de riesgo: calcula tamaño de posición y niveles de SL/TP.

Regla base: arriesgar RISK_PER_TRADE (1%) del capital por cada trade.
  quantity = (capital * risk_pct) / (entry_price * stop_loss_pct)

Ejemplo:
  capital = 1000 USDT, risk_pct = 0.01, entry = 50000, sl_pct = 0.02
  → quantity = (1000 * 0.01) / (50000 * 0.02) = 10 / 1000 = 0.001 BTC
"""
from src import config


def position_size(capital_usdt: float, entry_price: float) -> float:
    """Cantidad de BTC a comprar dado el capital disponible y precio de entrada."""
    risk_usdt = capital_usdt * config.RISK_PER_TRADE
    loss_per_unit = entry_price * config.STOP_LOSS_PCT
    qty = risk_usdt / loss_per_unit
    # Binance requiere mínimo 0.001 BTC y múltiplos de 0.001
    qty = max(qty, 0.001)
    qty = round(qty, 3)
    return qty


def stop_loss_price(entry_price: float) -> float:
    return round(entry_price * (1 - config.STOP_LOSS_PCT), 2)


def take_profit_price(entry_price: float) -> float:
    return round(entry_price * (1 + config.TAKE_PROFIT_PCT), 2)


def check_stop_loss(entry_price: float, current_price: float) -> bool:
    return current_price <= stop_loss_price(entry_price)


def check_take_profit(entry_price: float, current_price: float) -> bool:
    return current_price >= take_profit_price(entry_price)


def pnl(entry_price: float, exit_price: float, quantity: float) -> tuple[float, float]:
    """Devuelve (pnl_usdt, pnl_pct)."""
    pnl_usdt = (exit_price - entry_price) * quantity
    pnl_pct = (exit_price - entry_price) / entry_price
    return round(pnl_usdt, 4), round(pnl_pct, 6)


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
