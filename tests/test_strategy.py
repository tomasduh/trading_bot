"""Tests para strategy: validar look-ahead bias fix y filtro de tendencia."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from src import strategy, config


def _make_df(prices: list[float], periods: int = None) -> pd.DataFrame:
    n = periods or len(prices)
    if len(prices) < n:
        prices = prices + [prices[-1]] * (n - len(prices))
    timestamps = pd.date_range("2026-01-01", periods=n, freq="30min", tz="UTC")
    return pd.DataFrame({
        "open":   prices,
        "high":   [p * 1.005 for p in prices],
        "low":    [p * 0.995 for p in prices],
        "close":  prices,
        "volume": [1000.0] * n,
    }, index=timestamps)


def test_evaluate_returns_signal_object():
    # Datos planos → señal NONE pero sin crashear
    df = _make_df([100.0] * 250)
    sig = strategy.evaluate(df)
    assert sig.type in ("BUY", "SELL", "NONE")
    assert hasattr(sig, "score")
    assert hasattr(sig, "trend")
    assert hasattr(sig, "adx")


def test_no_lookahead_uses_closed_candle():
    """
    El bug anterior: evaluar usaba iloc[-1] (vela en formación, precio actual).
    Fix: usa iloc[-2] (última vela cerrada).
    Test: si modificamos la última vela, la decisión NO debe cambiar.
    """
    base_prices = list(np.linspace(100, 120, 250))  # uptrend
    df_a = _make_df(base_prices)
    sig_a = strategy.evaluate(df_a)

    # Modificamos solo la última vela (que sería la "vela en formación")
    base_prices[-1] = 50.0   # cambio drástico
    df_b = _make_df(base_prices)
    sig_b = strategy.evaluate(df_b)

    # La señal debe ser idéntica porque usamos la vela CERRADA (iloc[-2])
    assert sig_a.type == sig_b.type, \
        f"Look-ahead bias detectado: {sig_a.type} vs {sig_b.type} con última vela diferente"


def test_insufficient_data_returns_none():
    df = _make_df([100.0] * 10)
    sig = strategy.evaluate(df)
    assert sig.type == "NONE"


def test_trend_detection():
    # Uptrend fuerte: precio sube de 100 a 130 en 250 velas
    prices = list(np.linspace(100, 130, 250))
    df = _make_df(prices)
    sig = strategy.evaluate(df)
    # En uptrend, el filtro debe detectar trend="up" (o neutral si ADX < 20)
    assert sig.trend in ("up", "neutral")


if __name__ == "__main__":
    test_evaluate_returns_signal_object()
    test_no_lookahead_uses_closed_candle()
    test_insufficient_data_returns_none()
    test_trend_detection()
    print("OK - all strategy tests passed")
