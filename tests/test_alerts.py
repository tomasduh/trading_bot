"""Formato de los mensajes de Telegram y resumen diario."""
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src import alerts, bot, config

NOW = datetime(2026, 11, 13, 21, 20, tzinfo=timezone.utc)


def test_every_message_says_paper_or_real(monkeypatch):
    monkeypatch.setattr(config, "IS_LIVE", False)
    assert "🧪 PAPER" in alerts.format_trade_opened("AAPL", 350.0, 0.05, 315.0, 700.0, "x")
    monkeypatch.setattr(config, "IS_LIVE", True)
    assert "🔴 REAL" in alerts.format_trade_closed("AAPL", 350.0, 360.0, 0.5, 0.028, "SIGNAL")


def test_opened_shows_money_invested_and_stop_distance():
    msg = alerts.format_trade_opened("BTC/USDT", 70_000.0, 0.00028, 56_000.0, 140_000.0,
                                     "cierre > SMA50", trade_id=330)
    assert "Invertido: <b>$19.60</b>" in msg
    assert "Stop: $56,000.00 (−20.00%)" in msg
    assert "Salida: por señal diaria" in msg       # el TP +100% técnico no se muestra
    assert "$140,000" not in msg
    assert "#330" in msg


def test_opened_shows_target_when_it_is_a_real_one():
    msg = alerts.format_trade_opened("AAPL", 100.0, 1, 98.0, 104.0, "legacy")
    assert "Objetivo: $104.00 (+4.00%)" in msg


def test_closed_explains_result_reason_and_duration():
    opened = datetime(2026, 11, 9, 14, 33, tzinfo=timezone.utc)
    msg = alerts.format_trade_closed("AAPL", 348.09, 359.81, 0.67, 0.0337, "SIGNAL", qty=0.0574,
                                     opened_at=opened, closed_at=opened + timedelta(days=4, hours=0, minutes=23))
    assert msg.startswith("✅ <b>Venta — AAPL</b>")
    assert "Resultado: <b>+$0.67 (+3.37%)</b>" in msg
    assert "Cierre por: señal de salida  ·  duró 4d 0h" in msg


def test_closed_loss_uses_minus_sign_and_naive_dates():
    opened = datetime(2026, 11, 13, 10, 0)                    # SQLite devuelve fechas sin tz
    msg = alerts.format_trade_closed("XRP/USDT", 1.0412, 1.0204, -0.42, -0.0215, "STOP_LOSS",
                                     opened_at=opened, closed_at=opened + timedelta(minutes=95))
    assert msg.startswith("❌")
    assert "−$0.42 (−2.15%)" in msg
    assert "$1.0412 → $1.0204" in msg                         # precios chicos: 4 decimales
    assert "stop loss  ·  duró 1h 35m" in msg


def test_html_in_reasons_is_escaped():
    assert "&lt;b&gt;" in alerts.format_trade_opened("AAPL", 1, 1, 1, 1, "<b>x")


def test_summary_lists_closed_open_and_totals():
    msg = alerts.format_daily_summary(
        day="13/11/2026",
        closed_today=[{"symbol": "AAPL", "pnl_usdt": 0.67, "pnl_pct": 0.0337},
                      {"symbol": "META", "pnl_usdt": -0.15, "pnl_pct": -0.0179}],
        open_positions=[{"symbol": "NVDA", "entry": 163.45, "qty": 0.0625, "price": 161.47,
                         "opened_at": NOW - timedelta(hours=25)},
                        {"symbol": "BTC/USDT", "entry": 70_000.0, "qty": 0.00028, "price": None,
                         "opened_at": None}],
        realized_total=-3.10, capital=200.0, paused=False, strategy="daily", now=NOW)
    assert "Cerradas hoy:</b> 2 (1 ganadas) · +$0.52" in msg
    assert "• NVDA −$0.12 (−1.21%) · 1d 1h" in msg
    assert "• BTC/USDT sin precio" in msg
    assert "Acumulado realizado:</b> −$3.10 (−1.55% sobre $200)" in msg


def test_summary_on_a_quiet_day_and_paused():
    msg = alerts.format_daily_summary(day="14/11/2026", closed_today=[], open_positions=[],
                                      realized_total=0.0, capital=200.0, paused=True)
    assert "Bot pausado" in msg
    assert "Sin operaciones cerradas hoy." in msg and "Sin posiciones abiertas." in msg


def test_critical_alert_says_what_to_do(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "_post", sent.append)
    alerts.notify_critical("Circuit breaker — bot pausado", "Drawdown diario 3.2%", action="reanudar")
    assert sent[0].startswith("🚨 <b>Circuit breaker — bot pausado</b>")
    assert "<b>Qué hacer:</b> reanudar" in sent[0]


# ── Envío una vez por día ────────────────────────────────────────────────────

@pytest.fixture
def summary(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(bot, "SUMMARY_MARKER", tmp_path / ".last_summary")
    monkeypatch.setattr(bot.alerts, "enabled", lambda: True)
    monkeypatch.setattr(bot.alerts, "notify_daily_summary", lambda **k: sent.append(k))
    monkeypatch.setattr(bot, "build_daily_summary", lambda ex, now: {"day": now.strftime("%d/%m/%Y")})
    return sent


def test_summary_waits_for_the_market_close(summary):
    assert bot.maybe_daily_summary(None, now=NOW.replace(hour=20, minute=0)) is False
    assert summary == []


def test_summary_is_sent_once_per_day(summary):
    assert bot.maybe_daily_summary(None, now=NOW) is True
    assert bot.maybe_daily_summary(None, now=NOW + timedelta(minutes=10)) is False
    assert bot.maybe_daily_summary(None, now=NOW + timedelta(days=1)) is True
    assert [s["day"] for s in summary] == ["13/11/2026", "14/11/2026"]


def test_summary_is_skipped_without_telegram(summary, monkeypatch):
    monkeypatch.setattr(bot.alerts, "enabled", lambda: False)
    assert bot.maybe_daily_summary(None, now=NOW) is False
