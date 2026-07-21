"""
Regresión del loop resume→repausa del circuit breaker.

Bug histórico: tras CB_CONSECUTIVE_LOSSES pérdidas seguidas la condición 3
creaba data/.paused; al reanudar, la condición volvía a contar la MISMA racha
vieja y repausaba en el ciclo siguiente → bot detenido permanentemente.

Fix: /api/resume escribe data/.cb_resumed_at y la condición 3 solo cuenta
trades cerrados DESPUÉS de ese timestamp.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import os
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

os.environ.setdefault("TRADING_MODE", "testnet")

from src import config, circuit_breaker
from src.circuit_breaker import CircuitBreaker


def _trade(pnl, days_ago):
    """Trade cerrado sintético con exit_time en el pasado (no hoy, para no
    disparar la condición 1 de drawdown diario)."""
    exit_time = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return SimpleNamespace(pnl_usdt=pnl, exit_time=exit_time,
                           entry_time=exit_time - timedelta(hours=1))


def _make_breaker(monkeypatch, trades, resume_file):
    cb = CircuitBreaker(10_000.0)
    monkeypatch.setattr(cb, "_load_recent_trades", lambda: trades)
    monkeypatch.setattr(cb, "_combined_capital", lambda: 10_000.0)
    monkeypatch.setattr(circuit_breaker, "CB_RESUME_FILE", resume_file)
    return cb


def test_streak_de_perdidas_dispara_pausa(monkeypatch, tmp_path):
    """N pérdidas consecutivas (== CB_CONSECUTIVE_LOSSES) → debe pausar."""
    resume_file = tmp_path / ".cb_resumed_at"  # no existe → sin resume previo
    trades = [_trade(-50.0, days_ago=d) for d in range(config.CB_CONSECUTIVE_LOSSES, 0, -1)]
    cb = _make_breaker(monkeypatch, trades, resume_file)

    should_pause, reason = cb.check()
    assert should_pause is True
    assert "consecutiv" in reason.lower()


def test_resume_evita_repausa_con_racha_vieja(monkeypatch, tmp_path):
    """Tras escribir el marcador de resume DESPUÉS de la racha, la condición 3
    no debe volver a contar esos trades viejos → no repausa."""
    resume_file = tmp_path / ".cb_resumed_at"
    trades = [_trade(-50.0, days_ago=d) for d in range(config.CB_CONSECUTIVE_LOSSES, 0, -1)]
    cb = _make_breaker(monkeypatch, trades, resume_file)

    # Simular /api/resume: marcar "ahora" como el instante de reanudación.
    resume_file.write_text(datetime.now(timezone.utc).isoformat())

    should_pause, reason = cb.check()
    assert should_pause is False, f"repausó indebidamente: {reason}"


def test_resume_no_enmascara_perdidas_nuevas(monkeypatch, tmp_path):
    """Si tras el resume vuelven a acumularse N pérdidas nuevas, debe pausar
    otra vez (el marcador no desactiva permanentemente la protección)."""
    resume_file = tmp_path / ".cb_resumed_at"
    # Resume hace 2 días; luego N pérdidas nuevas (más recientes que el resume).
    resume_time = datetime.now(timezone.utc) - timedelta(days=2)
    resume_file.write_text(resume_time.isoformat())
    trades = [_trade(-50.0, days_ago=h / 24.0)
              for h in range(config.CB_CONSECUTIVE_LOSSES, 0, -1)]  # todas < 2 días
    cb = _make_breaker(monkeypatch, trades, resume_file)

    should_pause, _ = cb.check()
    assert should_pause is True
