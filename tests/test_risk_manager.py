"""Tests para risk_manager incluyendo el nuevo trailing stop."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config, risk_manager


def test_position_size_uses_risk_pct():
    # 1000 USDT * 1% / (50000 * 2%) = 10 / 1000 = 0.01 BTC
    qty = risk_manager.position_size(capital_usdt=1000, entry_price=50_000)
    assert qty == 0.01, f"Esperado 0.01, obtenido {qty}"


def test_stop_loss_below_entry():
    sl = risk_manager.stop_loss_price(50_000)
    assert sl == round(50_000 * (1 - config.STOP_LOSS_PCT), 2)
    assert sl < 50_000


def test_take_profit_above_entry():
    tp = risk_manager.take_profit_price(50_000)
    assert tp > 50_000


def test_pnl_calculation():
    pnl_usdt, pnl_pct = risk_manager.pnl(50_000, 51_000, 0.1)
    assert pnl_usdt == 100.0
    assert abs(pnl_pct - 0.02) < 1e-6


def _with_trailing_enabled():
    """Context manager pattern: fuerza USE_TRAILING_STOP=True para el test."""
    prev = config.USE_TRAILING_STOP
    config.USE_TRAILING_STOP = True
    return prev


def _restore_trailing(prev):
    config.USE_TRAILING_STOP = prev


def test_trailing_stop_inactive_below_activation():
    prev = _with_trailing_enabled()
    try:
        ts = risk_manager.trailing_stop_price(100, 101)
        assert ts is None
    finally:
        _restore_trailing(prev)


def test_trailing_stop_active_after_activation():
    prev = _with_trailing_enabled()
    try:
        ts = risk_manager.trailing_stop_price(100, 102)
        assert ts is not None
        expected = round(102 * (1 - config.TRAILING_DISTANCE_PCT), 2)
        assert ts == expected
    finally:
        _restore_trailing(prev)


def test_trailing_stop_triggers_correctly():
    prev = _with_trailing_enabled()
    try:
        triggered = risk_manager.check_trailing_stop(100, 105, 103.95)
        assert triggered is True
    finally:
        _restore_trailing(prev)


def test_trailing_stop_not_triggered_above():
    prev = _with_trailing_enabled()
    try:
        triggered = risk_manager.check_trailing_stop(100, 105, 104.5)
        assert triggered is False
    finally:
        _restore_trailing(prev)


def test_trailing_stop_disabled_returns_none():
    """Cuando USE_TRAILING_STOP=False, siempre devuelve None."""
    prev = config.USE_TRAILING_STOP
    config.USE_TRAILING_STOP = False
    try:
        ts = risk_manager.trailing_stop_price(100, 200)
        assert ts is None, "TS debe ser None si está desactivado"
    finally:
        config.USE_TRAILING_STOP = prev


if __name__ == "__main__":
    test_position_size_uses_risk_pct()
    test_stop_loss_below_entry()
    test_take_profit_above_entry()
    test_pnl_calculation()
    test_trailing_stop_inactive_below_activation()
    test_trailing_stop_active_after_activation()
    test_trailing_stop_triggers_correctly()
    test_trailing_stop_not_triggered_above()
    test_trailing_stop_disabled_returns_none()
    print("OK - all risk_manager tests passed (9 tests)")
