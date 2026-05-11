"""
Fetcher para Alpaca Paper Trading (stocks).
Equivalente a data_fetcher.py pero para acciones US.
"""
import os
import pandas as pd
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

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
    "1h":  TimeFrame(1,  TimeFrameUnit.Hour),
    "1d":  TimeFrame(1,  TimeFrameUnit.Day),
}


def fetch_ohlcv(symbol: str, timeframe: str = "15m", limit: int = 200) -> pd.DataFrame:
    tf = _TF_MAP.get(timeframe, TimeFrame(15, TimeFrameUnit.Minute))
    # Alpaca no acepta 'limit', usa ventana de tiempo — pedimos suficiente historia
    end   = datetime.now(timezone.utc)
    # Para 15m, 200 velas ≈ 50 horas ≈ ~7 días de mercado
    start = end - timedelta(days=14)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=tf,
        start=start,
        end=end,
        limit=limit,
        feed=DataFeed.IEX,   # plan free usa IEX, no SIP
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
    return df.tail(limit)


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
