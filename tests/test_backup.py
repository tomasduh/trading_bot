"""Backup diario de la DB a Telegram."""
import sys
import gzip
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src import backup


@pytest.fixture
def env(monkeypatch, tmp_path):
    db = tmp_path / "trades.db"
    con = sqlite3.connect(db)
    con.execute("create table trades (id integer primary key, symbol text)")
    con.execute("insert into trades (symbol) values ('BTC/USDT')")
    con.commit()
    con.close()
    monkeypatch.setattr(backup.config, "DB_PATH", db)
    monkeypatch.setattr(backup, "MARKER_FILE", tmp_path / ".last_backup")
    monkeypatch.setattr(backup.alerts, "enabled", lambda: True)
    monkeypatch.setattr(backup.alerts, "notify_error", lambda m: None)

    sent = []

    def send_document(path, caption=""):
        with gzip.open(path) as f:
            restored = tmp_path / "restored.db"
            restored.write_bytes(f.read())
        rows = sqlite3.connect(restored).execute("select symbol from trades").fetchall()
        sent.append(rows)
        return True
    monkeypatch.setattr(backup.alerts, "send_document", send_document)
    return tmp_path, sent


def test_sends_restorable_backup_once_per_day(env):
    tmp_path, sent = env
    morning = datetime(2026, 10, 24, 7, tzinfo=timezone.utc)

    assert backup.maybe_backup(morning) is True
    assert backup.maybe_backup(morning.replace(hour=9)) is False   # ya hecho hoy
    assert sent == [[("BTC/USDT",)]]
    # no deja archivos temporales en el volumen
    assert sorted(p.name for p in tmp_path.iterdir()) == [".last_backup", "restored.db", "trades.db"]


def test_waits_until_backup_hour(env):
    _, sent = env
    assert backup.maybe_backup(datetime(2026, 10, 24, 3, tzinfo=timezone.utc)) is False
    assert sent == []
