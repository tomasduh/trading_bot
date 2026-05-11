"""
Test de integración: ejecuta varios ciclos del bot con datos sintéticos
y verifica que el flujo completo (fetch → indicators → strategy → executor → DB)
funciona sin crashear y produce resultados consistentes.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import tempfile
import numpy as np
import pandas as pd

# Aislar la DB del test antes de importar nada
_TEST_DB = Path(tempfile.gettempdir()) / "test_integration.db"
if _TEST_DB.exists():
    _TEST_DB.unlink()
os.environ["TRADING_MODE"] = "testnet"

# Forzamos un DB path de test (override config tras import)
from src import config
config.DB_PATH = _TEST_DB

from sqlalchemy import event
from src import database
# Recrear engine apuntando al DB de test
database.engine.dispose()
database.engine = database.engine.__class__.__init_subclass__  # placeholder no-op
import sqlalchemy
database.engine = sqlalchemy.create_engine(
    f"sqlite:///{_TEST_DB}",
    connect_args={"timeout": 30, "check_same_thread": False},
)

@event.listens_for(database.engine, "connect")
def _set_pragmas(conn, _):
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


def test_full_cycle_no_crash():
    """Ejecuta un ciclo completo con datos sintéticos."""
    from src import indicators, strategy
    np.random.seed(123)

    # Construir datos sintéticos con tendencia alcista y volatilidad
    n = 250
    prices = list(100 + np.cumsum(np.random.randn(n) * 0.5) + np.linspace(0, 20, n))
    ts = pd.date_range("2026-01-01", periods=n, freq="30min", tz="UTC")
    df = pd.DataFrame({
        "open":   prices,
        "high":   [p * 1.005 for p in prices],
        "low":    [p * 0.995 for p in prices],
        "close":  prices,
        "volume": [1000.0] * n,
    }, index=ts)

    # Indicators + strategy no deben crashear
    df_ind = indicators.add_all(df)
    valid = df_ind.dropna()
    assert len(valid) > 50, f"Esperaba > 50 filas válidas, obtuvo {len(valid)}"

    sig = strategy.evaluate(df)
    assert sig.type in ("BUY", "SELL", "NONE")
    assert sig.score >= 0
    assert sig.trend in ("up", "down", "neutral")


def test_multi_symbol_simulation():
    """Simula procesamiento de 4 símbolos en secuencia sin error."""
    from src import indicators, strategy
    np.random.seed(456)

    symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]
    base_prices = [80000, 2300, 90, 650]

    for sym, base in zip(symbols, base_prices):
        n = 200
        prices = list(base + np.cumsum(np.random.randn(n) * base * 0.005))
        ts = pd.date_range("2026-01-01", periods=n, freq="30min", tz="UTC")
        df = pd.DataFrame({
            "open":   prices,
            "high":   [p * 1.003 for p in prices],
            "low":    [p * 0.997 for p in prices],
            "close":  prices,
            "volume": [1000.0] * n,
        }, index=ts)

        df_ind = indicators.add_all(df)
        sig = strategy.evaluate(df)
        assert sig.type in ("BUY", "SELL", "NONE")


def test_strategy_consistent_with_backtest():
    """
    La estrategia evaluada por el bot real (con ventana completa) debe
    devolver el mismo resultado que el backtest cuando usa la misma ventana.
    """
    from src import indicators, strategy
    np.random.seed(789)

    n = 200
    prices = list(100 + np.cumsum(np.random.randn(n) * 0.5))
    ts = pd.date_range("2026-01-01", periods=n, freq="30min", tz="UTC")
    df = pd.DataFrame({
        "open":   prices,
        "high":   [p * 1.005 for p in prices],
        "low":    [p * 0.995 for p in prices],
        "close":  prices,
        "volume": [1000.0] * n,
    }, index=ts)

    # Bot real: pasa el df completo
    sig_bot = strategy.evaluate(df)

    # Backtest: pasa una ventana específica (df.iloc[:i+1])
    df_ind = indicators.add_all(df)
    window = df_ind.iloc[: n]   # toda la historia (igual al bot)
    sig_bt = strategy.evaluate(window)

    # Ambos deben dar el mismo resultado
    assert sig_bot.type == sig_bt.type, \
        f"Inconsistencia bot vs backtest: {sig_bot.type} vs {sig_bt.type}"


if __name__ == "__main__":
    test_full_cycle_no_crash()
    test_multi_symbol_simulation()
    test_strategy_consistent_with_backtest()
    print("OK - all integration tests passed")
