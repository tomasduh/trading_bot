"""
Fetcher para Alpaca Paper Trading (stocks).
Equivalente a data_fetcher.py pero para acciones US.
"""
import os
import time
import functools
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (MarketOrderRequest, LimitOrderRequest, StopOrderRequest,
                                     TakeProfitRequest, StopLossRequest, GetOrderByIdRequest)
from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, OrderType, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

logger = logging.getLogger("alpaca_fetcher")

# Si el último bar es más viejo que esto durante horario de mercado,
# consideramos que el feed está stale y rechazamos la data.
# El IEX feed (free) tiene baja cobertura para algunos stocks → bars viejos
# mientras get_stock_latest_trade() sí devuelve precios frescos.
MAX_STALENESS_MINUTES = 60

load_dotenv(Path(__file__).parent.parent / ".env")

from src import config

# Claves y endpoint según ALPACA_MODE (paper usa ALPACA_API_KEY, live ALPACA_LIVE_API_KEY)
API_KEY    = config.ALPACA_API_KEY
SECRET_KEY = config.ALPACA_SECRET_KEY

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=config.ALPACA_PAPER)
data_client    = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# alpaca-py no expone un parámetro de timeout: internamente hace
# self._session.request(method, url, **opts) sin "timeout" en opts, así que
# un hang de red deja el thread bloqueado para siempre (visto en prod: OOM
# 2026-08-04 precedido de ~40min de "price refresh timed out" repetido).
# Se parchea la sesión de requests de cada cliente para forzar timeout real
# en cada llamada HTTP, incluso sobre conexiones reusadas del pool.
for _client in (trading_client, data_client):
    _client._session.request = functools.partial(_client._session.request, timeout=15)

# Mapeo de timeframe string → Alpaca TimeFrame
_TF_MAP = {
    "1m":  TimeFrame(1,  TimeFrameUnit.Minute),
    "5m":  TimeFrame(5,  TimeFrameUnit.Minute),
    "15m": TimeFrame(15, TimeFrameUnit.Minute),
    "30m": TimeFrame(30, TimeFrameUnit.Minute),
    "1h":  TimeFrame(1,  TimeFrameUnit.Hour),
    "4h":  TimeFrame(4,  TimeFrameUnit.Hour),   # para multi-timeframe macro
    "1d":  TimeFrame(1,  TimeFrameUnit.Day),
}


def fetch_ohlcv(symbol: str, timeframe: str = "15m", limit: int = 200) -> pd.DataFrame:
    tf = _TF_MAP.get(timeframe, TimeFrame(15, TimeFrameUnit.Minute))
    end = datetime.now(timezone.utc)

    # ⚠️ BUG de Alpaca: cuando pasas (start, end, limit) y limit < total de bars
    # del rango, devuelve los PRIMEROS limit bars (los más viejos).
    # SOLUCIÓN: ajustar el RANGO `start` para que contenga aprox `limit` bars
    # (con buffer). Así Alpaca devuelve todos los del rango y nosotros tail(limit).
    tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30,
                  "1h": 60, "4h": 240, "1d": 1440}.get(timeframe, 30)
    market_minutes_needed = limit * tf_minutes
    # Mercado abierto 6.5h/día = 1/3.7 del calendario. Factor 1.4 de buffer.
    calendar_days_needed = max(5, int(market_minutes_needed / 60 / 6.5 * 1.4) + 2)
    start = end - timedelta(days=calendar_days_needed)

    # limit ligeramente mayor a `limit` solicitado para asegurar que el rango
    # corto contenga los velas necesarias después de filtrar pre-market.
    # NO usar 10000: hace que Alpaca devuelva miles de bars y tarde 10-15s.
    api_limit = min(limit * 3, 2000)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=tf,
        start=start,
        end=end,
        limit=api_limit,
        feed=DataFeed.IEX,
    )
    bars = data_client.get_stock_bars(req)
    df = bars.df

    if df.empty:
        return pd.DataFrame(columns=["open","high","low","close","volume"])

    # El índice es MultiIndex (symbol, timestamp) cuando hay un símbolo
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")

    df.index = pd.to_datetime(df.index, utc=True)
    df = df[["open","high","low","close","volume"]].sort_index()
    df = df.tail(limit)

    # ── Validación de frescura ─────────────────────────────────────────────
    # El feed IEX (gratis) de Alpaca tiene cobertura limitada para algunos
    # stocks → puede devolver bars del 20 abril aunque hoy sea 14 mayo.
    # Para evitar operar con data stale (lo que causó el bug AMD #9 a $274
    # cuando el precio real era $452), validamos contra get_stock_latest_trade
    # que SÍ funciona en tiempo real con IEX.
    if not df.empty:
        try:
            last_bar_ts = df.index[-1].to_pydatetime()
            if last_bar_ts.tzinfo is None:
                last_bar_ts = last_bar_ts.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            staleness_min = (now - last_bar_ts).total_seconds() / 60

            # Solo validar durante horario probable de mercado (lun-vie 13-22 UTC ≈ 9-17 ET).
            # Fuera de eso, datos "viejos" son normales (cerrado).
            if 0 <= now.weekday() <= 4 and 13 <= now.hour <= 22:
                if staleness_min > MAX_STALENESS_MINUTES:
                    logger.warning(
                        "[%s] Feed STALE: último bar de hace %.0f min (límite %d). "
                        "Rechazando data — no operar este símbolo.",
                        symbol, staleness_min, MAX_STALENESS_MINUTES
                    )
                    return pd.DataFrame(columns=["open","high","low","close","volume"])
        except Exception as e:
            logger.warning("[%s] Error validando frescura: %s", symbol, e)

    return df


def get_latest_trade_price(symbol: str) -> float | None:
    """Devuelve el precio del último trade en tiempo real (independiente de get_stock_bars).

    Útil cuando get_stock_bars devuelve data stale — get_stock_latest_trade
    funciona correctamente con feed IEX gratuito y da precios en tiempo real.
    """
    try:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        result = data_client.get_stock_latest_trade(req)
        if symbol in result:
            return float(result[symbol].price)
    except Exception as e:
        logger.warning("[%s] get_latest_trade_price falló: %s", symbol, e)
    return None


def fetch_balance() -> dict:
    """free.USD = lo que el bot puede usar SIN margen: min(cash, buying power no
    marginable). Alpaca abre todas las cuentas como margen; con < $2,000 el
    multiplicador es 1, pero en paper (o con más capital) buying_power incluye
    apalancamiento y el bot nunca debe usarlo."""
    account = trading_client.get_account()
    cash = float(account.cash or 0)
    nmbp = account.non_marginable_buying_power
    free = min(cash, float(nmbp)) if nmbp is not None else cash
    return {
        "free": {
            "USD": max(0.0, free),
        },
        "cash": cash,
        "buying_power": float(account.buying_power),
        "portfolio_value": float(account.portfolio_value),
    }


def fetch_latest_price(symbol: str) -> float:
    df = fetch_ohlcv(symbol, timeframe="1m", limit=1)
    if df.empty:
        return 0.0
    return float(df["close"].iloc[-1])


def create_market_order(symbol: str, side: str, qty: float) -> dict:
    order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
    )
    order = _wait_for_fill(trading_client.submit_order(req))
    if order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
        raise RuntimeError(f"orden {side} {symbol} {order.status.value}")
    return {
        "id":      str(order.id), "symbol": symbol, "qty": qty, "side": side,
        "status":  order.status.value,
        "average": float(order.filled_avg_price) if order.filled_avg_price else None,
        "filled":  float(order.filled_qty) if order.filled_qty else None,
    }


FILL_TIMEOUT_S = 10.0


def _wait_for_fill(order):
    """Una market order recién enviada suele volver 'accepted' sin precio. Se
    consulta hasta que esté filled (o termine) para registrar el precio real;
    si no llega a tiempo se devuelve tal cual y el caller usa su precio de referencia."""
    deadline = time.monotonic() + FILL_TIMEOUT_S
    done = (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED)
    while order.status not in done and time.monotonic() < deadline:
        time.sleep(0.5)
        order = trading_client.get_order_by_id(order.id)
    if order.status != OrderStatus.FILLED:
        logger.warning("[%s] orden %s sin fill completo tras %.0fs (status=%s)",
                       order.symbol, order.id, FILL_TIMEOUT_S, order.status.value)
    return order


# ── Protección del lado del exchange ──────────────────────────────────────────
# Acciones enteras: OCO de venta GTC (TP limit + stop a mercado). Queda viva en
# Alpaca aunque el bot esté caído o pausado; el stop se ejecuta aunque abra con gap.
# Fracciones: Alpaca solo acepta órdenes DAY y sin OCO → stop a mercado DAY que el
# bot renueva cada sesión (en cola antes de la apertura); el TP lo ejecuta el bot.

_ORDER_DONE = (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED,
               OrderStatus.REPLACED, OrderStatus.DONE_FOR_DAY)
_ORDER_EXPIRED = (OrderStatus.EXPIRED, OrderStatus.DONE_FOR_DAY)
CANCEL_TIMEOUT_S = 5.0

_fractionable_cache: dict[str, bool] = {}


def is_fractionable(symbol: str) -> bool:
    if symbol not in _fractionable_cache:
        asset = trading_client.get_asset(symbol)
        _fractionable_cache[symbol] = bool(asset.fractionable and asset.tradable)
    return _fractionable_cache[symbol]


def _is_whole(qty: float) -> bool:
    return abs(qty - round(qty)) < 1e-9


def place_protection(symbol: str, qty: float, stop_price: float, take_profit: float) -> str:
    """Coloca la protección de una posición abierta y devuelve el id de la orden:
    OCO GTC si la cantidad es entera, stop DAY si es fraccionaria."""
    if _is_whole(qty):
        req = LimitOrderRequest(
            symbol=symbol, qty=round(qty), side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC, order_class=OrderClass.OCO,
            take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
        )
    else:
        req = StopOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY, stop_price=round(stop_price, 2),
        )
    return str(trading_client.submit_order(req).id)


def _oco_status(orders: list) -> dict:
    """{"state": "open"|"filled"|"canceled"|"expired", "reason", "exit_price"} de
    la orden de protección y sus legs (OCO: limit del TP + stop; fracción: solo stop)."""
    for o in orders:
        if o.status == OrderStatus.FILLED:
            reason = "TAKE_PROFIT" if o.type == OrderType.LIMIT else "STOP_LOSS"
            return {"state": "filled", "reason": reason,
                    "exit_price": float(o.filled_avg_price)}
    if all(o.status in _ORDER_DONE for o in orders):
        # DAY vencida al cierre (o GTC a los 90 días): normal, se repone
        if any(o.status in _ORDER_EXPIRED for o in orders):
            return {"state": "expired"}
        return {"state": "canceled"}
    return {"state": "open"}


def minutes_to_open() -> float:
    """Minutos hasta la próxima apertura (0 si el mercado está abierto)."""
    clock = trading_client.get_clock()
    if clock.is_open:
        return 0.0
    return (clock.next_open - clock.timestamp).total_seconds() / 60


def get_protection_status(symbol: str, order_id: str) -> dict:
    order = trading_client.get_order_by_id(order_id, filter=GetOrderByIdRequest(nested=True))
    return _oco_status([order] + list(order.legs or []))


def cancel_protection(symbol: str, order_id: str) -> dict:
    """Cancela la protección (OCO o stop DAY) y espera a que Alpaca libere las
    acciones (si no, el SELL siguiente se rechaza por qty retenida). Si ya se
    había ejecutado, devuelve 'filled'."""
    try:
        trading_client.cancel_order_by_id(order_id)
    except Exception as e:
        logger.info("[%s] cancelar OCO %s: %s (¿ya ejecutada?)", symbol, order_id, e)
    deadline = time.monotonic() + CANCEL_TIMEOUT_S
    while True:
        status = get_protection_status(symbol, order_id)
        if status["state"] != "open" or time.monotonic() >= deadline:
            return status
        time.sleep(0.5)


def get_open_positions() -> dict:
    positions = trading_client.get_all_positions()
    return {p.symbol: p for p in positions}


def is_market_open() -> bool:
    clock = trading_client.get_clock()
    return clock.is_open
