"""Test que verifica WAL mode + concurrencia básica de SQLite."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database import engine, init_db, get_session, Trade


def test_wal_mode_enabled():
    init_db()
    with engine.connect() as conn:
        result = conn.exec_driver_sql("PRAGMA journal_mode").fetchone()
        assert result[0].lower() == "wal", f"Journal mode debe ser WAL, es {result[0]}"


def test_busy_timeout_set():
    with engine.connect() as conn:
        result = conn.exec_driver_sql("PRAGMA busy_timeout").fetchone()
        assert result[0] >= 30000, f"busy_timeout debe ser >= 30000ms, es {result[0]}"


def test_concurrent_session_open():
    """Dos sesiones simultáneas no deben bloquearse en WAL mode."""
    s1 = get_session()
    s2 = get_session()
    try:
        s1.query(Trade).count()
        s2.query(Trade).count()  # debería funcionar sin bloqueo
    finally:
        s1.close()
        s2.close()


if __name__ == "__main__":
    test_wal_mode_enabled()
    test_busy_timeout_set()
    test_concurrent_session_open()
    print("OK - all database tests passed")
