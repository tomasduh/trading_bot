"""
Backup diario off-site de la DB: snapshot SQLite consistente → gzip → Telegram.

Complementa los snapshots del volumen de Fly (5 días de retención, mismo
proveedor): si se pierde el volumen o la app, el historial queda en el chat.
Corre desde el loop del bot una vez por día, después de BACKUP_HOUR_UTC.
Todo se hace en disco (volumen), en streaming, para no subir la RAM de la VM 512MB.
"""
import gzip
import shutil
import sqlite3
import logging
from datetime import datetime, timezone

from src import config, alerts

logger = logging.getLogger("bot")

BACKUP_HOUR_UTC = 6                       # fuera de horario de mercado US
MARKER_FILE = config.BASE_DIR / "data" / ".last_backup"
TELEGRAM_MAX_BYTES = 49 * 1024 * 1024     # límite de sendDocument para bots: 50MB


def maybe_backup(now: datetime | None = None) -> bool:
    """Hace el backup si toca hoy. Devuelve True si se envió."""
    if not alerts.enabled():
        return False
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    if now.hour < BACKUP_HOUR_UTC:
        return False
    try:
        if MARKER_FILE.exists() and MARKER_FILE.read_text().strip() == today:
            return False
    except OSError:
        pass
    # Se marca antes de intentar: si falla se avisa una vez y se reintenta mañana,
    # en vez de reintentar (y alertar) cada ciclo.
    MARKER_FILE.write_text(today)

    snap = config.DB_PATH.with_name("backup_snapshot.db")
    gz = config.DB_PATH.with_name(f"trades-{today}.db.gz")
    try:
        src = sqlite3.connect(config.DB_PATH)
        dst = sqlite3.connect(snap)
        try:
            src.backup(dst)               # consistente aunque el bot esté escribiendo (WAL)
        finally:
            dst.close()
            src.close()
        with open(snap, "rb") as fin, gzip.open(gz, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout, length=1024 * 1024)

        size = gz.stat().st_size
        if size > TELEGRAM_MAX_BYTES:
            alerts.notify_error(f"Backup diario no enviado: {size/1e6:.1f}MB supera el límite "
                                f"de Telegram. Queda el snapshot de Fly; hay que podar la DB.")
            return False
        ok = alerts.send_document(gz, caption=f"🗄 Backup trades.db {today} ({size/1e6:.1f}MB)")
        if ok:
            logger.info(f"Backup diario enviado a Telegram ({size/1e6:.1f}MB)")
        else:
            alerts.notify_error(f"Backup diario {today}: Telegram rechazó el archivo.")
        return ok
    except Exception as e:
        logger.error(f"Backup diario falló: {e}", exc_info=True)
        alerts.notify_error(f"Backup diario {today} falló: {e}")
        return False
    finally:
        for p in (snap, gz):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
