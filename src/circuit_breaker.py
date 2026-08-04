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

CB_RESUME_FILE = config.BASE_DIR / "data" / ".cb_resumed_at"


def _resumed_at() -> Optional[datetime]:
    """Timestamp del último /api/resume, si existe. Se usa para que la
    condición de racha de pérdidas no siga contando contra trades cerrados
    antes del resume — si no, el bot queda en loop resume → repause en el
    siguiente ciclo con la misma racha que causó la pausa original."""
    try:
        return _aware(datetime.fromisoformat(CB_RESUME_FILE.read_text().strip()))
    except (FileNotFoundError, ValueError):
        return None


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normaliza datetime a timezone-aware UTC (los viejos en SQLite no tienen tz)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class CircuitBreaker:
    """Evalúa condiciones de riesgo y decide si pausar el bot.

    El capital base se calcula DINÁMICAMENTE como la suma de:
      - Binance USDT free + used (crypto trading capital)
      - Alpaca USD cash + portfolio value (stocks trading capital)

    Esto evita el falso-positivo donde un trade de stock (Alpaca) generaba
    un drawdown % "enorme" cuando se medía solo contra el balance crypto.

    Uso:
        breaker = CircuitBreaker()
        should_pause, reason = breaker.check()
    """

    def __init__(self, initial_capital: float = 10_000.0):
        # Mantenido por compatibilidad — pero check() recalcula capital combinado
        self.initial_capital = initial_capital

    def _combined_capital(self) -> float:
        """Suma capital de Binance (USDT) + Alpaca (USD).
        Si algún fetch falla, usa initial_capital como fallback conservador."""
        total = 0.0
        try:
            from src import data_fetcher
            b = data_fetcher.fetch_balance()
            usdt = float(b.get("free", {}).get("USDT", 0) or 0) + \
                   float(b.get("used", {}).get("USDT", 0) or 0)
            total += usdt
        except Exception as e:
            logger.warning(f"[CB] Binance balance falló: {e}")

        try:
            from src import alpaca_fetcher
            acc = alpaca_fetcher.trading_client.get_account()
            # Usar equity (cash + posiciones marked-to-market) para el capital real
            total += float(acc.equity or 0)
        except Exception as e:
            logger.warning(f"[CB] Alpaca balance falló: {e}")

        # Fallback: si ambos fetches fallan o suman 0, usar capital inicial
        if total <= 0:
            return self.initial_capital
        return total

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

        # Capital base combinado (Binance USDT + Alpaca USD)
        capital = self._combined_capital()

        # ── Condición 1: Drawdown diario ─────────────────────────────────────
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        today_trades = [
            t for t in trades
            if t.exit_time and _aware(t.exit_time) >= today_start
        ]
        daily_pnl = sum(t.pnl_usdt or 0 for t in today_trades)
        daily_dd_pct = abs(daily_pnl) / capital * 100 if daily_pnl < 0 else 0

        if daily_dd_pct >= config.CB_DAILY_DRAWDOWN_PCT:
            reason = (f"Drawdown diario {daily_dd_pct:.1f}% ≥ "
                      f"{config.CB_DAILY_DRAWDOWN_PCT}% "
                      f"(PnL hoy: {daily_pnl:+.2f} sobre capital combinado ${capital:,.0f})")
            logger.warning(f"[CB] 🚨 CONDICIÓN 1: {reason}")
            return True, reason

        # ── Condición 2: Pérdida total acumulada ────────────────────────────
        total_pnl    = sum(t.pnl_usdt or 0 for t in trades)
        total_loss_pct = abs(total_pnl) / capital * 100 if total_pnl < 0 else 0

        if total_loss_pct >= config.CB_TOTAL_LOSS_PCT:
            reason = (f"Pérdida total {total_loss_pct:.1f}% ≥ "
                      f"{config.CB_TOTAL_LOSS_PCT}% del capital "
                      f"(PnL acumulado: {total_pnl:+.2f} sobre ${capital:,.0f})")
            logger.warning(f"[CB] 🚨 CONDICIÓN 2: {reason}")
            return True, reason

        # ── Condición 3: Trades perdedores consecutivos ──────────────────────
        # Solo considera trades cerrados después del último resume manual,
        # para no repausar con la misma racha que ya causó la pausa anterior.
        resumed_at = _resumed_at()
        closed_sorted = sorted(
            [t for t in trades
             if t.exit_time and (resumed_at is None or _aware(t.exit_time) >= resumed_at)],
            key=lambda t: _aware(t.exit_time),
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
            f"[CB] OK | capital=${capital:,.0f} | DD diario={daily_dd_pct:.2f}% | "
            f"PnL total={total_pnl:+.2f} | streak={consecutive} pérdidas consec.")
        return False, ""

    def _load_recent_trades(self) -> list:
        """Carga trades cerrados de los últimos 30 días.
        Cargamos sin filtro de fecha en SQL y normalizamos timezones en Python
        para evitar mezclas tz-naive vs tz-aware (SQLite no preserva tz)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        with get_session() as s:
            trades = (
                s.query(Trade)
                .filter(Trade.status == "CLOSED")
                .all()
            )
            for t in trades:
                s.expunge(t)
        # Filtrar en Python (post-fetch) con datetime normalizado
        return [t for t in trades if t.exit_time and _aware(t.exit_time) >= cutoff]


# Instancia global — se crea en bot.py con el capital correcto
_breaker: Optional[CircuitBreaker] = None


def get_breaker(initial_capital: float = 10_000.0) -> CircuitBreaker:
    """Retorna la instancia global (singleton lazy). Crea una si no existe."""
    global _breaker
    if _breaker is None:
        _breaker = CircuitBreaker(initial_capital)
    return _breaker
