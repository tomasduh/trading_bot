"""
Alertas vía Telegram. Solo envía si TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID están configurados.

Cómo configurarlo:
  1. En Telegram, @BotFather → /newbot → copiar el token
  2. Mandarle un mensaje al bot nuevo y abrir
     https://api.telegram.org/bot<TOKEN>/getUpdates → copiar "chat":{"id": ...}
  3. fly secrets set TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... -a tomas-bot-trading
     (local: añadir ambos a .env)
"""
import os
import html
import logging
import threading
from datetime import datetime, timezone
import requests

logger = logging.getLogger("bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TIMEOUT = 10  # segundos
_API = "https://api.telegram.org/bot{token}/{method}"


def enabled() -> bool:
    return bool(BOT_TOKEN and CHAT_ID)


def _send_message(text: str):
    try:
        r = requests.post(
            _API.format(token=BOT_TOKEN, method="sendMessage"),
            json={"chat_id": CHAT_ID, "text": text[:4000], "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            logger.warning("Telegram respondió %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        # No loguear la URL: contiene el token
        logger.warning("Telegram falló: %s", type(e).__name__)


def _post(text: str):
    """Envía un mensaje a Telegram en background thread (no bloquea el bot)."""
    if not enabled():
        return
    threading.Thread(target=_send_message, args=(text,), daemon=True).start()


def send_document(path, caption: str = "") -> bool:
    """Sube un archivo al chat (bloqueante). Devuelve True si Telegram lo aceptó."""
    if not enabled():
        return False
    try:
        with open(path, "rb") as f:
            r = requests.post(
                _API.format(token=BOT_TOKEN, method="sendDocument"),
                data={"chat_id": CHAT_ID, "caption": caption[:1000]},
                files={"document": (os.path.basename(str(path)), f)},
                timeout=120,
            )
        if r.status_code != 200:
            logger.warning("Telegram sendDocument respondió %s: %s", r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception as e:
        logger.warning("Telegram sendDocument falló: %s", type(e).__name__)
        return False


def _esc(s) -> str:
    return html.escape(str(s))


# ── Formato ───────────────────────────────────────────────────────────────────

_REASONS = {
    "SIGNAL":        "señal de salida",
    "STOP_LOSS":     "stop loss",
    "TAKE_PROFIT":   "take profit",
    "TRAILING_STOP": "trailing stop",
}


def _mode_tag() -> str:
    """Etiqueta en cada mensaje: que nunca haya duda de si es dinero real."""
    from src import config
    return "🔴 REAL" if config.IS_LIVE else "🧪 PAPER"


def _price(x: float) -> str:
    """Precio con decimales según magnitud (XRP a $1.03 necesita 4; BTC, 2)."""
    x = float(x or 0)
    return f"${x:,.2f}" if abs(x) >= 10 else f"${x:,.4f}"


def _money(x: float) -> str:
    """Importe con signo: +$1.25 / −$0.40."""
    x = float(x or 0)
    return f"{'+' if x >= 0 else '−'}${abs(x):,.2f}"


def _pct(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}{abs(x):.2f}%"


def _qty(q: float) -> str:
    return f"{float(q or 0):.6f}".rstrip("0").rstrip(".")


def _duration(start, end) -> str:
    """'3d 4h', '5h 20m', '12m' — vacío si falta alguna fecha."""
    if not start or not end:
        return ""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    minutes = max(0, int((end - start).total_seconds() // 60))
    d, rem = divmod(minutes, 1440)
    h, m = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    return f"{h}h {m}m" if h else f"{m}m"


# ── Mensajes ──────────────────────────────────────────────────────────────────

def format_trade_opened(symbol: str, entry: float, qty: float, sl: float, tp: float,
                        reason: str, trade_id: int | None = None) -> str:
    invested = entry * qty
    stop_dist = (sl / entry - 1) * 100 if entry else 0
    lines = [
        f"🟢 <b>Compra — {_esc(symbol)}</b>  ·  {_mode_tag()}",
        f"Invertido: <b>${invested:,.2f}</b> ({_qty(qty)} a {_price(entry)})",
        f"Stop: {_price(sl)} ({_pct(stop_dist)})",
    ]
    # Modo diario: el take-profit es un +100% técnico (lo exige la OCO), no un objetivo
    if entry and tp and tp < entry * 1.5:
        lines.append(f"Objetivo: {_price(tp)} ({_pct((tp / entry - 1) * 100)})")
    else:
        lines.append("Salida: por señal diaria")
    lines.append(f"<i>Motivo: {_esc(reason)}</i>")
    if trade_id is not None:
        lines.append(f"#{trade_id}")
    return "\n".join(lines)


def format_trade_closed(symbol: str, entry: float, exit_price: float, pnl_usdt: float,
                        pnl_pct: float, reason: str, qty: float | None = None,
                        opened_at=None, closed_at=None, trade_id: int | None = None) -> str:
    win = pnl_usdt >= 0
    held = _duration(opened_at, closed_at)
    lines = [
        f"{'✅' if win else '❌'} <b>Venta — {_esc(symbol)}</b>  ·  {_mode_tag()}",
        f"Resultado: <b>{_money(pnl_usdt)} ({_pct(pnl_pct * 100)})</b>",
        f"{_price(entry)} → {_price(exit_price)}" + (f"  ·  {_qty(qty)} uds" if qty else ""),
        f"Cierre por: {_esc(_REASONS.get(reason, reason))}" + (f"  ·  duró {held}" if held else ""),
    ]
    if trade_id is not None:
        lines.append(f"#{trade_id}")
    return "\n".join(lines)


def format_daily_summary(day: str, closed_today: list[dict], open_positions: list[dict],
                         realized_total: float, capital: float, paused: bool,
                         strategy: str = "", now: datetime | None = None) -> str:
    """Resumen de fin de día.
    closed_today:   [{"symbol", "pnl_usdt", "pnl_pct"}]
    open_positions: [{"symbol", "entry", "qty", "price" (o None), "opened_at"}]"""
    now = now or datetime.now(timezone.utc)
    lines = [f"📊 <b>Resumen {_esc(day)}</b>  ·  {_mode_tag()}"]
    if paused:
        lines.append("⏸ <b>Bot pausado</b> — no abre posiciones nuevas")

    if closed_today:
        day_pnl = sum(t["pnl_usdt"] or 0 for t in closed_today)
        wins = sum(1 for t in closed_today if (t["pnl_usdt"] or 0) >= 0)
        lines.append(f"\n<b>Cerradas hoy:</b> {len(closed_today)} ({wins} ganadas) · {_money(day_pnl)}")
        for t in closed_today:
            lines.append(f"  {'✅' if (t['pnl_usdt'] or 0) >= 0 else '❌'} {_esc(t['symbol'])} "
                         f"{_money(t['pnl_usdt'])} ({_pct((t['pnl_pct'] or 0) * 100)})")
    else:
        lines.append("\nSin operaciones cerradas hoy.")

    if open_positions:
        lines.append(f"\n<b>Abiertas:</b> {len(open_positions)}")
        floating = 0.0
        for p in open_positions:
            held = _duration(p.get("opened_at"), now)
            if p.get("price") and p.get("entry"):
                pnl = (p["price"] - p["entry"]) * p["qty"]
                floating += pnl
                detail = f"{_money(pnl)} ({_pct((p['price'] / p['entry'] - 1) * 100)})"
            else:
                detail = "sin precio"
            lines.append(f"  • {_esc(p['symbol'])} {detail}" + (f" · {held}" if held else ""))
        lines.append(f"Flotante: {_money(floating)}")
    else:
        lines.append("\nSin posiciones abiertas.")

    total_pct = realized_total / capital * 100 if capital else 0
    lines.append(f"\n<b>Acumulado realizado:</b> {_money(realized_total)} "
                 f"({_pct(total_pct)} sobre ${capital:,.0f})")
    if strategy:
        lines.append(f"<i>Estrategia: {_esc(strategy)}</i>")
    return "\n".join(lines)


def notify_trade_opened(symbol: str, entry: float, qty: float,
                        sl: float, tp: float, reason: str, trade_id: int | None = None):
    _post(format_trade_opened(symbol, entry, qty, sl, tp, reason, trade_id))


def notify_trade_closed(symbol: str, entry: float, exit_price: float,
                        pnl_usdt: float, pnl_pct: float, reason: str, **details):
    _post(format_trade_closed(symbol, entry, exit_price, pnl_usdt, pnl_pct, reason, **details))


def notify_daily_summary(**kwargs):
    _post(format_daily_summary(**kwargs))


def notify_info(message: str):
    _post(f"ℹ️ {_esc(message)}\n{_mode_tag()}")


def notify_error(message: str):
    """Algo falló pero el bot sigue funcionando."""
    _post(f"⚠️ <b>Aviso</b>  ·  {_mode_tag()}\n{_esc(message[:3500])}")


def notify_critical(title: str, message: str, action: str = ""):
    """El bot dejó de operar (circuit breaker, kill-switch): alguien tiene que actuar."""
    text = f"🚨 <b>{_esc(title)}</b>  ·  {_mode_tag()}\n{_esc(message[:3000])}"
    if action:
        text += f"\n\n<b>Qué hacer:</b> {_esc(action)}"
    _post(text)
