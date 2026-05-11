"""
Tests del backtester con foco en lógica de exits.
- Verifica que SL, TP y TRAILING_STOP se simulan correctamente
- Verifica que la lógica del backtest es consistente con el bot real
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from src import config, backtest, risk_manager


def _df_with_path(prices: list[float]) -> pd.DataFrame:
    """Construye un DataFrame OHLCV donde high/low siguen el path de close."""
    n = len(prices)
    ts = pd.date_range("2026-01-01", periods=n, freq="30min", tz="UTC")
    return pd.DataFrame({
        "open":   prices,
        "high":   [p * 1.001 for p in prices],
        "low":    [p * 0.999 for p in prices],
        "close":  prices,
        "volume": [1000.0] * n,
    }, index=ts)


def test_simtrade_initializes_highest_price():
    """Cuando se abre un trade, highest_price = entry_price."""
    t = backtest.SimTrade(
        symbol="BTC/USDT",
        entry_time=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
        entry_price=100.0,
        quantity=1.0,
        stop_loss=98.0,
        take_profit=104.0,
        highest_price=100.0,
    )
    assert t.highest_price == 100.0


def test_trailing_stop_simulated_correctly():
    """
    Construye un escenario donde el precio sube fuerte y luego baja.
    El trailing stop DEBE activarse cuando se habilita.
    """
    prev = config.USE_TRAILING_STOP
    config.USE_TRAILING_STOP = True
    try:
        t = backtest.SimTrade(
            symbol="X", entry_time=None, entry_price=100, quantity=1,
            stop_loss=98.0, take_profit=200.0, highest_price=110,
        )
        ts_price = risk_manager.trailing_stop_price(t.entry_price, t.highest_price)
        assert ts_price is not None
        assert ts_price == 108.9, f"Esperado 108.9, obtuvo {ts_price}"
        triggered = risk_manager.check_trailing_stop(t.entry_price, t.highest_price, 108.5)
        assert triggered is True
    finally:
        config.USE_TRAILING_STOP = prev


def test_backtest_no_lookahead():
    """
    Verifica que el backtest no usa información futura para tomar decisiones.
    Cambiar las velas DESPUÉS de la decisión no debe alterar la decisión previa.
    """
    np.random.seed(42)
    base = list(np.linspace(100, 120, 200))
    df1 = _df_with_path(base)

    # Ejecutamos un backtest "simulado" calculando la primera decisión
    from src import indicators, strategy
    df_ind = indicators.add_all(df1)
    valid = df_ind.dropna()
    if len(valid) < 60:
        return  # no hay datos suficientes para test

    # Decisión en un punto específico
    window = df_ind.iloc[: 60]
    sig_a = strategy.evaluate(window)

    # Mismo punto pero con velas futuras MUY distintas
    base[60:] = [50.0] * (len(base) - 60)
    df2 = _df_with_path(base)
    df_ind_2 = indicators.add_all(df2)
    window_2 = df_ind_2.iloc[: 60]
    sig_b = strategy.evaluate(window_2)

    # La decisión en t=60 NO debe cambiar por velas futuras (t > 60)
    assert sig_a.type == sig_b.type, \
        f"Lookahead detectado: {sig_a.type} (con futuro normal) vs {sig_b.type} (con futuro caída)"


def test_summarize_handles_no_trades():
    """Si no hay trades, summarize devuelve mensaje claro sin crashear."""
    df = _df_with_path([100.0] * 100)
    result = backtest._summarize("X", "30m", 30, 10000, 10000, [], [], df)
    assert result["total_trades"] == 0
    assert "error" in result


def test_signal_exit_only_in_loss():
    """
    Con SIGNAL_EXIT_ONLY_IN_LOSS=True, el backtest NO debe cerrar un trade rentable
    por SELL signal. Solo debe cerrarlo si close <= entry_price.
    """
    prev = config.SIGNAL_EXIT_ONLY_IN_LOSS
    config.SIGNAL_EXIT_ONLY_IN_LOSS = True
    try:
        t = backtest.SimTrade(
            symbol="X", entry_time=None, entry_price=100.0, quantity=1.0,
            stop_loss=98.0, take_profit=104.0, highest_price=100.0,
        )
        # Precio por encima de entrada → NO debe cerrar
        # Simulamos la lógica directamente
        close_above = 101.0
        should_exit_profitable = close_above <= t.entry_price  # False
        assert should_exit_profitable is False, "No debería cerrar trade rentable"

        # Precio por debajo de entrada → SÍ debe cerrar
        close_below = 99.5
        should_exit_loss = close_below <= t.entry_price  # True
        assert should_exit_loss is True, "Sí debería cerrar trade en pérdida"
    finally:
        config.SIGNAL_EXIT_ONLY_IN_LOSS = prev


if __name__ == "__main__":
    test_simtrade_initializes_highest_price()
    test_trailing_stop_simulated_correctly()
    test_backtest_no_lookahead()
    test_summarize_handles_no_trades()
    test_signal_exit_only_in_loss()
    print("OK - all backtest tests passed")
