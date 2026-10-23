"""
Tests de TradeExecutor que no tocan la DB ni el exchange.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config
from src.database import Trade
from src.executor import TradeExecutor


def _executor_with(open_trades: dict) -> TradeExecutor:
    ex = TradeExecutor.__new__(TradeExecutor)   # salta _load_open_trades (DB)
    ex._open_trades = open_trades
    return ex


def test_legacy_open_symbols_only_returns_unconfigured_open_trades():
    """Un símbolo quitado de config con trade abierto debe seguir gestionándose."""
    assert "TSLA" not in config.STOCK_SYMBOLS
    assert "GOOGL" not in config.STOCK_SYMBOLS
    ex = _executor_with({
        "AAPL":     Trade(symbol="AAPL", side="BUY", status="OPEN"),  # en config
        "TSLA":     Trade(symbol="TSLA", side="BUY", status="OPEN"),  # quitado, abierto
        "GOOGL":    None,                                              # quitado, sin trade
        "ADA/USDT": Trade(symbol="ADA/USDT", side="BUY", status="OPEN"),
    })
    assert ex.legacy_open_symbols(stocks=True) == ["TSLA"]
    assert ex.legacy_open_symbols(stocks=False) == ["ADA/USDT"]
