"""
Con el bot pausado (circuit breaker o /api/pause) no se abren trades, pero las
posiciones abiertas deben seguir cerrándose por SL/TP.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import pytest

from src import bot


class FakeExecutor:
    def __init__(self, open_trades: dict, triggers: dict):
        self.open_trades = open_trades     # {symbol: entry_price}
        self.triggers = triggers           # {symbol: "STOP_LOSS" | None}
        self.checked, self.closed = [], []

    def open_symbols(self, stocks: bool) -> list[str]:
        return [s for s in self.open_trades if ("/" not in s) == stocks]

    def check_sl_tp(self, symbol, price):
        self.checked.append(symbol)
        return self.triggers.get(symbol)

    def close_trade(self, symbol, price, reason):
        self.closed.append((symbol, reason))
        return SimpleNamespace(id=1, entry_price=self.open_trades[symbol],
                               pnl_usdt=-1.0, pnl_pct=-0.02)


def _fetcher(price: float, market_open: bool = True):
    df = pd.DataFrame({"close": [price, price]})
    return SimpleNamespace(fetch_ohlcv=lambda *a, **k: df,
                           is_market_open=lambda: market_open)


@pytest.fixture
def paused(monkeypatch):
    monkeypatch.setattr(bot, "is_paused", lambda: True)
    monkeypatch.setattr(bot.alerts, "notify_trade_closed", lambda *a, **k: None)
    monkeypatch.setattr(bot.analyst, "log_outcome", lambda *a, **k: None)

    def _no_entries(*a, **k):
        raise AssertionError("process_market no debe correr con el bot pausado")
    monkeypatch.setattr(bot, "process_market", _no_entries)
    return monkeypatch


def test_paused_bot_still_closes_on_stop_loss(paused):
    paused.setattr(bot, "data_fetcher", _fetcher(95.0))
    paused.setattr(bot, "alpaca_fetcher", _fetcher(300.0, market_open=True))
    ex = FakeExecutor({"BTC/USDT": 100.0, "TSLA": 365.0, "AAPL": 340.0},
                      {"BTC/USDT": "STOP_LOSS", "TSLA": "STOP_LOSS"})

    bot.run_cycle(ex)

    assert sorted(ex.checked) == ["AAPL", "BTC/USDT", "TSLA"]
    assert sorted(ex.closed) == [("BTC/USDT", "STOP_LOSS"), ("TSLA", "STOP_LOSS")]


def test_paused_bot_skips_stocks_when_market_closed(paused):
    paused.setattr(bot, "data_fetcher", _fetcher(95.0))
    paused.setattr(bot, "alpaca_fetcher", _fetcher(300.0, market_open=False))
    ex = FakeExecutor({"BTC/USDT": 100.0, "TSLA": 365.0}, {})

    bot.run_cycle(ex)

    assert ex.checked == ["BTC/USDT"]
    assert ex.closed == []
