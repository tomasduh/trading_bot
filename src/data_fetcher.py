"""
Wrapper de ccxt para Binance con timeouts y manejo robusto de errores.
"""
import logging
import ccxt
import pandas as pd
from src import config

logger = logging.getLogger("bot")

# ── Configuración global ──────────────────────────────────────────────────────
REQUEST_TIMEOUT_MS = 15_000   # 15s timeout por request a Binance


def _build_exchange() -> ccxt.binance:
    ex = ccxt.binance({
        "apiKey":         config.API_KEY,
        "secret":         config.API_SECRET,
        "enableRateLimit": True,
        "timeout":        REQUEST_TIMEOUT_MS,
        "options": {
            "defaultType":             "spot",
            "fetchCurrencies":         False,
            "adjustForTimeDifference": True,
        },
    })
    if config.IS_TESTNET:
        ex.set_sandbox_mode(True)
    return ex


exchange = _build_exchange()


# ── Errores específicos para que bot.py los capture ──────────────────────────
class DataFetchError(Exception):
    """Error temporal de red/exchange. El bot puede reintentar."""


class FatalDataFetchError(Exception):
    """Error grave (auth, IP bloqueada). El bot debe detenerse."""


def _wrap_call(fn, *args, **kwargs):
    """
    Ejecuta una llamada de ccxt traduciendo excepciones a DataFetchError /
    FatalDataFetchError según el tipo. Permite que el bot decida si reintentar
    o detenerse.
    """
    try:
        return fn(*args, **kwargs)
    except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable,
            ccxt.DDoSProtection, ccxt.RateLimitExceeded) as e:
        logger.warning("Binance error temporal: %s", e)
        raise DataFetchError(str(e)) from e
    except (ccxt.AuthenticationError, ccxt.PermissionDenied) as e:
        logger.error("Binance error fatal (auth): %s", e)
        raise FatalDataFetchError(str(e)) from e
    except ccxt.ExchangeError as e:
        msg = str(e)
        if "451" in msg or "restricted location" in msg.lower():
            logger.error("Binance bloqueó la IP del servidor: %s", e)
            raise FatalDataFetchError(msg) from e
        logger.warning("Binance error: %s", e)
        raise DataFetchError(msg) from e


def fetch_ohlcv(symbol: str = config.SYMBOL,
                timeframe: str = config.TIMEFRAME,
                limit: int = config.CANDLES_LIMIT) -> pd.DataFrame:
    # Para timeframes más largos (4h) usamos menos velas pero suficientes para indicadores
    effective_limit = limit if timeframe == config.TIMEFRAME else max(100, limit)
    raw = _wrap_call(exchange.fetch_ohlcv, symbol, timeframe=timeframe,
                     limit=effective_limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp").sort_index()
    return df


def fetch_balance() -> dict:
    return _wrap_call(exchange.fetch_balance)


def fetch_ticker(symbol: str = config.SYMBOL) -> dict:
    return _wrap_call(exchange.fetch_ticker, symbol)


def fetch_open_orders(symbol: str = config.SYMBOL) -> list:
    return _wrap_call(exchange.fetch_open_orders, symbol)


def create_market_order(symbol: str, side: str, amount: float) -> dict:
    """side: 'buy' | 'sell'"""
    return _wrap_call(exchange.create_market_order, symbol, side, amount)


def create_limit_order(symbol: str, side: str, amount: float, price: float) -> dict:
    return _wrap_call(exchange.create_limit_order, symbol, side, amount, price)


def cancel_order(order_id: str, symbol: str = config.SYMBOL) -> dict:
    return _wrap_call(exchange.cancel_order, order_id, symbol)
