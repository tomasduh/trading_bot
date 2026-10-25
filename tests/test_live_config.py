"""
Modo por exchange, DB separada para live, tope de capital y circuit breaker
con el capital total. config se evalúa al importar, así que los modos se
prueban en un subproceso con el entorno armado.
"""
import os
import sys
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

ROOT = Path(__file__).parent.parent

_PROBE = """
import json
from src import config
print(json.dumps({
    "binance": config.BINANCE_MODE, "alpaca": config.ALPACA_MODE,
    "live": config.IS_LIVE, "testnet": config.IS_TESTNET,
    "db": config.DB_PATH.name, "alpaca_paper": config.ALPACA_PAPER,
    "binance_key": config.API_KEY, "alpaca_key": config.ALPACA_API_KEY,
    "caps": [config.BINANCE_CAPITAL_CAP, config.ALPACA_CAPITAL_CAP],
    "warnings": config.MODE_WARNINGS,
}))
"""


def _config_with(**env) -> dict:
    full = {k: v for k, v in os.environ.items()
            if k not in ("BINANCE_MODE", "ALPACA_MODE", "BINANCE_CAPITAL_CAP", "ALPACA_CAPITAL_CAP")}
    full.update({"TRADING_MODE": "testnet",
                 "BINANCE_API_KEY": "live-b", "BINANCE_TESTNET_API_KEY": "test-b",
                 "ALPACA_API_KEY": "paper-a", "ALPACA_LIVE_API_KEY": "live-a"})
    full.update(env)
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT, env=full,
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip().splitlines()[-1])
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_default_is_paper_on_both_with_the_existing_db():
    c = _config_with()
    assert (c["binance"], c["alpaca"], c["live"]) == ("testnet", "paper", False)
    assert c["db"] == "trades.db"
    assert c["binance_key"] == "test-b" and c["alpaca_key"] == "paper-a" and c["alpaca_paper"]


def test_binance_live_alone_uses_live_keys_and_live_db():
    c = _config_with(BINANCE_MODE="live", ALPACA_MODE="off")
    assert c["live"] and not c["testnet"]
    assert c["db"] == "trades_live.db"
    assert c["binance_key"] == "live-b"
    assert c["warnings"] == []


def test_live_and_paper_are_never_mixed():
    c = _config_with(BINANCE_MODE="live")                 # Alpaca queda en paper por defecto
    assert c["alpaca"] == "off" and c["warnings"]
    c = _config_with(ALPACA_MODE="live")                  # Binance queda en testnet por defecto
    assert c["binance"] == "off" and c["warnings"]


def test_alpaca_live_uses_its_own_keys_and_live_endpoint():
    c = _config_with(ALPACA_MODE="live", BINANCE_MODE="off")
    assert c["alpaca_key"] == "live-a" and not c["alpaca_paper"]
    assert c["db"] == "trades_live.db"


def test_legacy_trading_mode_live_still_means_binance_live():
    c = _config_with(TRADING_MODE="live", ALPACA_MODE="off")
    assert c["binance"] == "live"


def test_invalid_mode_refuses_to_start():
    with pytest.raises(RuntimeError, match="BINANCE_MODE inválido"):
        _config_with(BINANCE_MODE="prod")


def test_capital_caps_are_read():
    assert _config_with(BINANCE_CAPITAL_CAP="100", ALPACA_CAPITAL_CAP="100")["caps"] == [100, 100]


# ── Tope de capital (budget) ──────────────────────────────────────────────────

from src import budget, config, bot, circuit_breaker, alpaca_fetcher
from src.database import Trade

T0 = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def capped(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BINANCE_CAPITAL_CAP", 100.0)
    monkeypatch.setattr(config, "ALPACA_CAPITAL_CAP", 100.0)
    monkeypatch.setattr(budget, "SINCE_FILE", tmp_path / "trades.cap_since")
    (tmp_path / "trades.cap_since").write_text(T0.isoformat())
    budget.reset_cache()
    yield
    budget.reset_cache()


def _trade(symbol, notional, entered, pnl=None, exited=None):
    return Trade(symbol=symbol, side="BUY", status="CLOSED" if exited else "OPEN",
                 entry_price=notional, quantity=1.0, stop_loss=notional * 0.98,
                 entry_time=entered, exit_time=exited, pnl_usdt=pnl)


def test_cap_limits_capital_and_ignores_positions_from_before(capped):
    from tests.test_executor import _executor_with
    ex = _executor_with({
        "AAPL": _trade("AAPL", 16_000.0, T0 - timedelta(days=5)),   # posición vieja de paper
        "MSFT": _trade("MSFT", 20.0, T0 + timedelta(hours=1)),      # trade del ensayo
    })
    assert ex.open_notional(stocks=True) == 20.0                    # la vieja no consume tope
    assert ex.available_capital(95_000.0, stocks=True) == 80.0      # $100 − $20
    assert ex.available_capital(50.0, stocks=True) == 50.0          # nunca más que lo real


def test_since_is_set_on_first_activation_and_cleared_when_cap_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(budget, "SINCE_FILE", tmp_path / "x.cap_since")
    monkeypatch.setattr(config, "BINANCE_CAPITAL_CAP", 100.0)
    budget.reset_cache()
    first = budget.since()
    assert (tmp_path / "x.cap_since").exists()
    budget.reset_cache()
    assert budget.since() == first                                  # sobrevive reinicios
    monkeypatch.setattr(config, "BINANCE_CAPITAL_CAP", 0.0)
    monkeypatch.setattr(config, "ALPACA_CAPITAL_CAP", 0.0)
    assert budget.since() is None and not (tmp_path / "x.cap_since").exists()


def test_circuit_breaker_uses_total_capital_and_ignores_pre_cap_trades(capped, monkeypatch):
    now = datetime.now(timezone.utc)
    old_big_loss = _trade("AAPL", 16_000.0, T0 - timedelta(days=5), pnl=-320.0, exited=now)
    small_loss = _trade("BTC/USDT", 20.0, T0 + timedelta(hours=1), pnl=-0.4, exited=now)
    cb = circuit_breaker.CircuitBreaker()
    monkeypatch.setattr(cb, "_load_recent_trades", lambda: [old_big_loss, small_loss])

    assert cb.check(capital=200.0) == (False, "")                  # −$0.40 sobre $200

    small_loss.pnl_usdt = -7.0                                      # 3.5% de $200 en el día
    paused, reason = cb.check(capital=200.0)
    assert paused and "Drawdown diario" in reason


def test_total_capital_sums_active_exchanges_with_their_caps(capped, monkeypatch):
    from tests.test_executor import _executor_with
    ex = _executor_with({})
    assert bot.total_capital(ex) == 200.0
    monkeypatch.setattr(config, "ALPACA_MODE", "off")
    assert bot.total_capital(ex) == 100.0


def test_total_capital_without_cap_uses_real_balances(monkeypatch):
    from tests.test_executor import _executor_with
    monkeypatch.setattr(config, "BINANCE_CAPITAL_CAP", 0.0)
    monkeypatch.setattr(config, "ALPACA_CAPITAL_CAP", 0.0)
    monkeypatch.setattr(bot.data_fetcher, "fetch_balance",
                        lambda: {"free": {"USDT": 70.0}, "used": {"USDT": 10.0}})
    monkeypatch.setattr(bot.alpaca_fetcher.trading_client, "get_account",
                        lambda: SimpleNamespace(equity="100.0"))
    ex = _executor_with({"BTC/USDT": _trade("BTC/USDT", 20.0, T0)})
    assert bot.total_capital(ex) == 200.0                           # 70 + 10 + 20 en BTC + 100


# ── Cash sin margen en Alpaca ─────────────────────────────────────────────────

@pytest.mark.parametrize("cash, nmbp, expected", [
    ("100.0", "100.0", 100.0),     # cuenta chica: multiplicador 1
    ("95000", "90000", 90000.0),   # paper con margen: nunca más que el buying power no marginable
    ("100.0", None, 100.0),
    ("-50", "0", 0.0),
])
def test_alpaca_free_cash_never_uses_margin(monkeypatch, cash, nmbp, expected):
    monkeypatch.setattr(alpaca_fetcher.trading_client, "get_account", lambda: SimpleNamespace(
        cash=cash, non_marginable_buying_power=nmbp, buying_power="400000",
        portfolio_value="100000"))
    assert alpaca_fetcher.fetch_balance()["free"]["USD"] == expected


# ── Exchange en "off": solo gestiona la salida de lo abierto ─────────────────

def test_exchange_off_only_manages_open_positions(monkeypatch):
    from tests.test_executor import _executor_with
    calls = []
    monkeypatch.setattr(bot, "is_paused", lambda: False)
    monkeypatch.setattr(bot, "get_breaker", lambda c: SimpleNamespace(check=lambda cap: (False, "")))
    monkeypatch.setattr(bot.reconciler, "reconcile_all", lambda ex: [])
    monkeypatch.setattr(bot, "_ping_watchdog", lambda: None)
    monkeypatch.setattr(bot, "total_capital", lambda ex: 100.0)
    monkeypatch.setattr(bot, "process_market",
                        lambda sym, ex, cap, f, label, allow_entries=True:
                        calls.append((sym, allow_entries)) or 0.0)
    monkeypatch.setattr(config, "BINANCE_MODE", "off")
    monkeypatch.setattr(config, "ALPACA_MODE", "off")
    monkeypatch.setattr(bot.data_fetcher, "fetch_balance",
                        lambda: pytest.fail("Binance off no debe consultar balance"))

    ex = _executor_with({"ETH/USDT": _trade("ETH/USDT", 20.0, T0)})
    bot.run_cycle(ex)

    assert calls == [("ETH/USDT", False)]                           # nada de entradas nuevas
