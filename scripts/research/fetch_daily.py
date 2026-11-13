import sys, os
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import pandas as pd
from src import config

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily")
os.makedirs(OUT, exist_ok=True)


def crypto(sym):
    import ccxt
    ex = ccxt.binance({"enableRateLimit": True, "timeout": 20000})
    since = ex.parse8601("2018-01-01T00:00:00Z")
    rows = []
    while True:
        r = ex.fetch_ohlcv(sym, "1d", since=since, limit=1000)
        if not r:
            break
        rows += r
        if len(r) < 1000:
            break
        since = r[-1][0] + 86400000
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"]).drop_duplicates("timestamp")
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.set_index("timestamp").sort_index().iloc[:-1]  # sin la vela en curso


def stock(sym):
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.enums import DataFeed, Adjustment
    from alpaca.data.timeframe import TimeFrame
    cl = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY", "").strip(), os.getenv("ALPACA_SECRET_KEY", "").strip())
    end = datetime.now(timezone.utc) - timedelta(days=1)
    last = None
    for feed in (DataFeed.SIP, DataFeed.IEX):
        try:
            req = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Day,
                                   start=datetime(2016, 1, 1, tzinfo=timezone.utc), end=end,
                                   feed=feed, adjustment=Adjustment.ALL)
            df = cl.get_stock_bars(req).df
            if isinstance(df.index, pd.MultiIndex):
                df = df.xs(sym, level="symbol")
            df.index = pd.to_datetime(df.index, utc=True)
            return df[["open", "high", "low", "close", "volume"]].sort_index(), feed.value
        except Exception as e:
            last = e
    raise last


CRY = list(config.CRYPTO_SYMBOLS)
STO = list(config.STOCK_SYMBOLS) + ["QQQ"]
for s in CRY + STO:
    p = os.path.join(OUT, s.replace("/", "-") + ".pkl")
    try:
        if "/" in s:
            df, feed = crypto(s), "binance"
        else:
            df, feed = stock(s)
        df.to_pickle(p)
        print(s, feed, len(df), str(df.index[0])[:10], str(df.index[-1])[:10], flush=True)
    except Exception as e:
        print(s, "ERROR", type(e).__name__, str(e)[:200], flush=True)
