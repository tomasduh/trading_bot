"""
Circuit Breaker — pausa automática del bot cuando detecta condiciones de riesgo.

Condiciones que activan el circuit breaker (configurables en config.py):
  1. Drawdown diario > CB_DAILY_DRAWDOWN_PCT (default 3%)
  2. PnL acumulado < −CB_TOTAL_LOSS_PCT del capital inicial (default 10%)
  3. CB_CONSECUTIVE_LOSSES trades perdedores consecutivos (default 5)

Cómo funciona:
  - Al inicio de cada ciclo en bot.py se llama check().
  - Si devuelve (True, reason) → bot crea PAUSE_FILE y notifica.
  - El operador decide manualmente si reanudar.
  - check() es stateless: lee la DB en cada llamada → safe ante reinicios.

Nota: no cierra posiciones abiertas. Solo pausa la apertura de nuevas.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from src import config
from src.database import Trade, get_session

logger = logging.getLogger("circuit_breaker")


class CircuitBreaker:
    """Evalúa condiciones de riesgo y decide si pausar el bot.

    Uso:
        breaker = CircuitBreaker(initial_capital=10_000)
        should_pause, reason = breaker.check()
        if should_pause:
            ...
    """

    def __init__(self, initial_capital: float = 10_000.0):
        self.initial_capital = initial_capital

    def check(self) -> tuple[bool, str]:
        """Evalúa todas las condiciones de pausa.

        Returns
        ───────
        (should_pause: bool, reason: str)
        """
        try:
            trades = self._load_recent_trades()
        except Exception as e:
            logger.warning(f"[CB] Error leyendo trades: {e}")
            return False, ""

        # ── Condición 1: Drawdown diario ─────────────────────────────────────
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        today_trades = [
            t for t in trades
            if t.exit_time and t.exit_time.replace(tzinfo=timezone.utc) >= today_start
        ]
        daily_pnl = sum(t.pnl_usdt or 0 for t in today_trades)
        daily_dd_pct = abs(daily_pnl) / self.initial_capital * 100 if daily_pnl < 0 else 0

        if daily_dd_pct >= config.CB_DAILY_DRAWDOWN_PCT:
            reason = (f"Drawdown diario {daily_dd_pct:.1f}% ≥ "
                      f"{config.CB_DAILY_DRAWDOWN_PCT}% (PnL hoy: {daily_pnl:+.2f} USDT)")
            logger.warning(f"[CB] 🚨 CONDICIÓN 1: {reason}")
            return True, reason

        # ── Condición 2: Pérdida total acumulada ────────────────────────────
        total_pnl    = sum(t.pnl_usdt or 0 for t in trades)
        total_loss_pct = abs(total_pnl) / self.initial_capital * 100 if total_pnl < 0 else 0

        if total_loss_pct >= config.CB_TOTAL_LOSS_PCT:
            reason = (f"Pérdida total {total_loss_pct:.1f}% ≥ "
                      f"{config.CB_TOTAL_LOSS_PCT}% del capital inicial "
                      f"(PnL acumulado: {total_pnl:+.2f} USDT)")
            logger.warning(f"[CB] 🚨 CONDICIÓN 2: {reason}")
            return True, reason

        # ── Condición 3: Trades perdedores consecutivos ──────────────────────
        closed_sorted = sorted(
            [t for t in trades if t.exit_time],
            key=lambda t: t.exit_time,
        )
        consecutive = 0
        for t in reversed(closed_sorted):
            if (t.pnl_usdt or 0) < 0:
                consecutive += 1
            else:
                break

        if consecutive >= config.CB_CONSECUTIVE_LOSSES:
            reason = (f"{consecutive} trades perdedores consecutivos ≥ "
                      f"{config.CB_CONSECUTIVE_LOSSES}")
            logger.warning(f"[CB] 🚨 CONDICIÓN 3: {reason}")
            return True, reason

        logger.debug(
            f"[CB] OK | DD diario={daily_dd_pct:.1f}% | "
            f"PnL total={total_pnl:+.2f} | streak={consecutive} pérdidas consec.")
        return False, ""

    def _load_recent_trades(self) -> list:
        """Carga trades cerrados de los últimos 30 días."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        with get_session() as s:
            trades = (
                s.query(Trade)
                .filter(Trade.status == "CLOSED")
                .filter(Trade.exit_time >= cutoff)
                .all()
            )
            for t in trades:
                s.expunge(t)
        return trades


# Instancia global — se crea en bot.py con el capital correcto
_breaker: Optional[CircuitBreaker] = None


def get_breaker(initial_capital: float = 10_000.0) -> CircuitBreaker:
    """Retorna la instancia global (singleton lazy). Crea una si no existe."""
    global _breaker
    if _breaker is None:
        _breaker = CircuitBreaker(initial_capital)
    return _breaker
