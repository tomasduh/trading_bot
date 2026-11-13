"""
Estrategia diaria (STRATEGY_MODE=daily): tendencia SMA50 en crypto y compra de
caídas RSI(2) en stocks, sobre velas diarias cerradas.
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import pytest

from src import bot, config, risk_manager, strategy
from src.circuit_breaker import CircuitBreaker

NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)   # viernes, mercado abierto


def _daily(closes, last_day: datetime) -> pd.DataFrame:
    """Velas diarias que terminan en `last_day` (apertura 00:00 UTC de cada día)."""
    idx = pd.date_range(end=last_day, periods=len(closes), freq="D", tz="UTC")
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": 1.0}, index=idx)


TODAY = datetime(2026, 11, 13)
YESTERDAY = TODAY - timedelta(days=1)


# ── Vela en curso ────────────────────────────────────────────────────────────

def test_crypto_ignores_the_candle_still_forming():
    # Ayer cerró arriba de la media; la vela de hoy (en curso) se desploma.
    df = _daily([100.0] * 59 + [110.0, 50.0], TODAY)
    sig = strategy.evaluate_daily("BTC/USDT", df, now=NOW)
    assert sig.type == "BUY"
    assert sig.price == 110.0
    assert sig.valid_from == datetime(2026, 11, 13, tzinfo=timezone.utc)


def test_crypto_uses_last_candle_once_it_closed():
    df = _daily([100.0] * 59 + [110.0, 50.0], YESTERDAY)
    assert strategy.evaluate_daily("BTC/USDT", df, now=NOW).type == "SELL"


def test_stock_drops_todays_bar_even_late_in_the_session():
    closes = [100.0 + i * 0.1 for i in range(230)] + [200.0]
    df = _daily(closes, TODAY)
    df.index = df.index + timedelta(hours=5)            # Alpaca: medianoche de Nueva York
    sig = strategy.evaluate_daily("AAPL", df, now=NOW)
    assert sig.price == pytest.approx(closes[-2])


# ── Crypto: tendencia ────────────────────────────────────────────────────────

def test_crypto_sells_below_the_average():
    df = _daily([100.0] * 59 + [90.0], YESTERDAY)
    sig = strategy.evaluate_daily("ETH/USDT", df, now=NOW)
    assert sig.type == "SELL" and sig.trend == "down"


def test_crypto_needs_enough_history():
    df = _daily([100.0] * 40, YESTERDAY)
    assert strategy.evaluate_daily("BTC/USDT", df, now=NOW).type == "NONE"


# ── Stocks: compra de caídas ─────────────────────────────────────────────────

def _uptrend(tail: list[float]) -> pd.DataFrame:
    base = [100.0 + i * 0.2 for i in range(230)]        # sube de 100 a ~146
    return _daily(base + tail, YESTERDAY)


def test_stock_buys_a_sharp_dip_inside_an_uptrend():
    sig = strategy.evaluate_daily("MSFT", _uptrend([144.0, 142.0, 140.0]), now=NOW)
    assert sig.type == "BUY"
    assert sig.rsi < config.DAILY_RSI_ENTRY


def test_stock_exits_when_price_recovers_above_short_average():
    sig = strategy.evaluate_daily("MSFT", _uptrend([144.0, 142.0, 140.0, 141.0, 150.0]), now=NOW)
    assert sig.type == "SELL"


def test_stock_does_not_buy_a_dip_in_a_downtrend():
    falling = [200.0 - i * 0.3 for i in range(230)] + [128.0, 126.0, 124.0]
    sig = strategy.evaluate_daily("MSFT", _daily(falling, YESTERDAY), now=NOW)
    assert sig.type == "NONE" and sig.trend == "down"


def test_stock_holds_while_neither_rule_fires():
    # Cae fuerte (entrada), luego un día casi plano por debajo de la SMA5: ni compra ni vende.
    sig = strategy.evaluate_daily("MSFT", _uptrend([144.0, 142.0, 140.0, 140.3]), now=NOW)
    assert sig.type == "NONE"


# ── Tamaño y stops ───────────────────────────────────────────────────────────

def test_size_for_notional_respects_symbol_limits():
    assert risk_manager.size_for_notional(20.0, 70_000.0, "BTC/USDT") == pytest.approx(0.00028)
    assert risk_manager.size_for_notional(3.0, 70_000.0, "BTC/USDT") == 0.0      # < min notional
    assert risk_manager.size_for_notional(16.0, 500.0, "MSFT", fractional=True) == pytest.approx(0.032)
    assert risk_manager.size_for_notional(16.0, 500.0, "MSFT", fractional=False) == 0.0
    assert risk_manager.size_for_notional(1200.0, 500.0, "MSFT", fractional=True) == 2.0  # enteras


def test_daily_mode_uses_wide_emergency_stops(monkeypatch):
    monkeypatch.setattr(config, "IS_DAILY", True)
    assert risk_manager.get_sl_tp(100.0, symbol="BTC/USDT") == (80.0, 200.0)
    assert risk_manager.get_sl_tp(100.0, symbol="AAPL") == (90.0, 200.0)


def test_legacy_mode_keeps_its_stops(monkeypatch):
    monkeypatch.setattr(config, "IS_DAILY", False)
    assert risk_manager.get_sl_tp(100.0, symbol="AAPL") == (98.0, 104.0)


# ── Bot ──────────────────────────────────────────────────────────────────────

class FakeExecutor:
    def __init__(self, open_trade=None, last_exit=None, open_notional=0.0):
        self._open, self._last_exit, self._notional = open_trade, last_exit, open_notional
        self.opened, self.closed = [], []

    def sync_protection(self, s): return None
    def check_sl_tp(self, s, p): return None
    def ensure_protection(self, s, alert=True): pass
    def has_open_trade(self, s): return self._open is not None
    def get_open_trade(self, s): return self._open
    def last_exit_time(self, s): return self._last_exit
    def open_notional(self, stocks): return self._notional
    def total_exposure_pct(self, *a, **k): raise AssertionError("no aplica en modo diario")

    def target_notional(self, free, stocks):
        from src.executor import TradeExecutor
        return TradeExecutor.target_notional(self, free, stocks)

    def open_trade(self, symbol, signal, capital, notional=None):
        self.opened.append((symbol, notional))
        return SimpleNamespace(id=1, entry_price=signal.price, quantity=notional / signal.price,
                               stop_loss=1.0, take_profit=2.0)

    def close_trade(self, symbol, price, reason):
        self.closed.append((symbol, reason))
        return SimpleNamespace(id=1, entry_price=100.0, exit_price=price, exit_reason=reason,
                               pnl_usdt=1.0, pnl_pct=0.01)


@pytest.fixture
def daily_bot(monkeypatch):
    monkeypatch.setattr(config, "IS_DAILY", True)
    monkeypatch.setattr(config, "CRYPTO_SYMBOLS", ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"])
    for name in ("persist_candle", "persist_signal"):
        monkeypatch.setattr(bot, name, lambda *a, **k: None)
    for name in ("log_cycle", "log_features", "log_outcome"):
        monkeypatch.setattr(bot.analyst, name, lambda *a, **k: None)
    for name in ("notify_trade_opened", "notify_trade_closed"):
        monkeypatch.setattr(bot.alerts, name, lambda *a, **k: None)

    def make(daily_closes):
        intraday = pd.DataFrame(
            {c: [100.0 + i * 0.01 for i in range(80)] for c in ("open", "high", "low", "close")}
            | {"volume": [10.0] * 80},
            index=pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("15min"), periods=80, freq="15min"))
        last_closed = pd.Timestamp.now(tz="UTC").floor("D") - timedelta(days=1)
        return SimpleNamespace(fetch_ohlcv=lambda *a, **k: intraday,
                               fetch_daily=lambda s: _daily(daily_closes, last_closed))
    return make


def test_bot_opens_with_an_equal_share_of_capital(daily_bot):
    ex = FakeExecutor()
    committed = bot.process_market("BTC/USDT", ex, 100.0, daily_bot([100.0] * 59 + [110.0]))
    assert ex.opened == [("BTC/USDT", pytest.approx(100.0 / 5 * 0.98))]
    assert committed == pytest.approx(19.6)


def test_bot_closes_on_exit_signal_even_in_profit(daily_bot):
    ex = FakeExecutor(open_trade=SimpleNamespace(entry_price=50.0))    # +100%: legacy no cerraría
    bot.process_market("BTC/USDT", ex, 100.0, daily_bot([100.0] * 59 + [90.0]))
    assert ex.closed == [("BTC/USDT", "SIGNAL")]


def test_bot_does_not_rebuy_after_exiting_on_the_same_daily_bar(daily_bot):
    ex = FakeExecutor(last_exit=datetime.now(timezone.utc))
    bot.process_market("BTC/USDT", ex, 100.0, daily_bot([100.0] * 59 + [110.0]))
    assert ex.opened == []


def test_bot_rebuys_once_a_new_daily_bar_has_closed(daily_bot):
    ex = FakeExecutor(last_exit=datetime.now(timezone.utc) - timedelta(days=2))
    bot.process_market("BTC/USDT", ex, 100.0, daily_bot([100.0] * 59 + [110.0]))
    assert len(ex.opened) == 1


def test_bot_does_nothing_when_daily_candles_fail(daily_bot):
    fetcher = daily_bot([100.0] * 60)
    fetcher.fetch_daily = lambda s: (_ for _ in ()).throw(RuntimeError("sin red"))
    ex = FakeExecutor(open_trade=SimpleNamespace(entry_price=50.0))
    bot.process_market("BTC/USDT", ex, 100.0, fetcher)
    assert ex.opened == [] and ex.closed == []


def test_paused_bot_still_exits_on_daily_signal(daily_bot):
    ex = FakeExecutor(open_trade=SimpleNamespace(entry_price=100.0))
    bot.check_exits_only("BTC/USDT", ex, daily_bot([100.0] * 59 + [90.0]), "CRYPTO")
    assert ex.closed == [("BTC/USDT", "SIGNAL")]


# ── Circuit breaker ──────────────────────────────────────────────────────────

def _losers(n):
    now = datetime.now(timezone.utc)
    return [SimpleNamespace(symbol="BTC/USDT", pnl_usdt=-0.01, entry_time=now, exit_time=now)
            for _ in range(n)]


def test_losing_streak_does_not_pause_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "CB_CONSECUTIVE_LOSSES", 0)
    monkeypatch.setattr("src.circuit_breaker._resumed_at", lambda: None)
    cb = CircuitBreaker()
    monkeypatch.setattr(cb, "_load_recent_trades", lambda: _losers(8))
    monkeypatch.setattr("src.circuit_breaker.budget.counts", lambda t: True)
    assert cb.check(10_000.0) == (False, "")


def test_losing_streak_still_pauses_in_legacy(monkeypatch):
    monkeypatch.setattr(config, "CB_CONSECUTIVE_LOSSES", 5)
    monkeypatch.setattr("src.circuit_breaker._resumed_at", lambda: None)
    cb = CircuitBreaker()
    monkeypatch.setattr(cb, "_load_recent_trades", lambda: _losers(5))
    monkeypatch.setattr("src.circuit_breaker.budget.counts", lambda t: True)
    assert cb.check(10_000.0)[0] is True
