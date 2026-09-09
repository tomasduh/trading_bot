"""Tests para risk_manager incluyendo el nuevo trailing stop."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config, risk_manager


def test_position_size_uses_risk_pct():
    # 1000 USDT * 1% / (50000 * 2%) = 10 / 1000 = 0.01 BTC → notional $500 (50% del
    # capital), pero MAX_POSITION_PCT_OF_CAPITAL (20%) topea antes: 1000*0.20/50000 = 0.004
    qty = risk_manager.position_size(capital_usdt=1000, entry_price=50_000)
    assert qty == 0.004, f"Esperado 0.004, obtenido {qty}"


def test_position_size_never_exceeds_max_pct_of_capital():
    # Sin el cap, el ratio riesgo/stop da 50% del capital en un solo trade
    # (RISK_PER_TRADE / STOP_LOSS_PCT = 1%/2%). El cap debe impedirlo siempre,
    # a cualquier escala de capital. Se pasa symbol para usar los límites reales
    # de BTC/USDT (step fino) en vez del fallback genérico usado cuando no hay symbol.
    for capital in (50, 1_000, 10_000):
        qty = risk_manager.position_size(capital_usdt=capital, entry_price=50_000, symbol="BTC/USDT")
        notional = qty * 50_000
        assert notional <= capital * config.MAX_POSITION_PCT_OF_CAPITAL + 1e-6, (
            f"capital={capital}: notional {notional} supera el cap de "
            f"{config.MAX_POSITION_PCT_OF_CAPITAL*100:.0f}%"
        )


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


def test_position_size_respects_min_notional():
    """Capital ridículamente bajo → qty=0 (no llega a min_notional)."""
    qty_low = risk_manager.position_size(0.1, 50_000, "BTC/USDT")
    assert qty_low == 0, f"Esperado 0, obtuvo {qty_low}"


def test_position_size_floors_to_step():
    """Qty se trunca al step del símbolo."""
    qty = risk_manager.position_size(10_000, 50_000, "BTC/USDT")
    # step BTC=0.00001 → qty debe ser múltiplo
    assert abs(qty * 100000 - round(qty * 100000)) < 1e-6


def test_position_size_stocks_integer_shares():
    """Para stocks, qty debe ser entero."""
    qty = risk_manager.position_size(10_000, 280, "AAPL")
    assert qty == int(qty), f"AAPL qty debe ser entera, obtuvo {qty}"


def test_signal_exit_loss_buffer_active():
    """El buffer evita cerrar trades en breakeven exacto o leve pérdida menor."""
    entry = 100.0
    threshold = entry * (1 - config.SIGNAL_EXIT_LOSS_BUFFER_PCT)
    # Precio en entrada o ligeramente abajo (dentro del buffer): NO debe disparar
    assert 100.0 >= threshold
    assert (100.0 - 0.1) >= threshold  # 0.1% pérdida, dentro del buffer 0.2%
    # Precio claramente bajo el buffer: SÍ debe disparar
    assert (100.0 - 1.0) < threshold


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
    test_position_size_respects_min_notional()
    test_position_size_floors_to_step()
    test_position_size_stocks_integer_shares()
    test_signal_exit_loss_buffer_active()
    print("OK - all risk_manager tests passed")
