"""
Tope de capital por exchange (BINANCE_CAPITAL_CAP / ALPACA_CAPITAL_CAP).

El bot opera como si la cuenta tuviera como máximo ese capital:
  - ensayo en paper con el tamaño real de live (ej. $100 → trades de $20,
    fracciones, stops DAY) aunque la cuenta paper tenga $100k;
  - techo de seguridad en live aunque la cuenta tenga más fondos.

"Desde cuándo" corre el tope se guarda al activarlo (data/<db>.cap_since). Los
trades abiertos ANTES de esa fecha (ej. las posiciones grandes de paper previas
al ensayo) se siguen gestionando normalmente (SL/TP/OCO), pero no consumen el
tope ni cuentan en el circuit breaker: si no, una sola posición de $16k
bloquearía el ensayo de $100 y su PnL lo pausaría al instante.
"""
import logging
from datetime import datetime, timezone

from src import config

logger = logging.getLogger("bot")

SINCE_FILE = config.DB_PATH.with_name(config.DB_PATH.stem + ".cap_since")
_since: datetime | None = None


def cap(stocks: bool) -> float:
    return config.ALPACA_CAPITAL_CAP if stocks else config.BINANCE_CAPITAL_CAP


def active() -> bool:
    return cap(False) > 0 or cap(True) > 0


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def since() -> datetime | None:
    """Inicio del tope: se fija la primera vez que se activa y se borra al quitarlo."""
    global _since
    if not active():
        if SINCE_FILE.exists():
            SINCE_FILE.unlink()
        _since = None
        return None
    if _since is None:
        try:
            _since = _aware(datetime.fromisoformat(SINCE_FILE.read_text().strip()))
        except (FileNotFoundError, ValueError):
            _since = datetime.now(timezone.utc)
            SINCE_FILE.write_text(_since.isoformat())
            logger.info(f"Tope de capital activado desde {_since.isoformat()}")
    return _since


def counts(trade) -> bool:
    """¿Este trade consume el tope (y cuenta en el circuit breaker)?"""
    if cap("/" not in trade.symbol) <= 0:
        return True
    start = since()
    return trade.entry_time is not None and _aware(trade.entry_time) >= start


def available(real_free: float, stocks: bool, used_notional: float) -> float:
    """Capital libre para el bot: el real, limitado a (tope − nocional abierto del tope)."""
    c = cap(stocks)
    if c <= 0:
        return real_free
    return max(0.0, min(real_free, c - used_notional))


def reset_cache():
    global _since
    _since = None
