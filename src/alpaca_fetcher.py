"""
Fetcher para Alpaca Paper Trading (stocks).
Equivalente a data_fetcher.py pero para acciones US.
"""
import os
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
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

API_KEY    = os.getenv("ALPACA_API_KEY", "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client    = StockHistoricalDataClient(API_KEY, SECRET_KEY)

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
    account = trading_client.get_account()
    return {
        "free": {
            "USD": float(account.cash),
        },
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
    order = trading_client.submit_order(req)
    return {"id": str(order.id), "symbol": symbol, "qty": qty, "side": side}


def get_open_positions() -> dict:
    positions = trading_client.get_all_positions()
    return {p.symbol: p for p in positions}


def is_market_open() -> bool:
    clock = trading_client.get_clock()
    return clock.is_open
