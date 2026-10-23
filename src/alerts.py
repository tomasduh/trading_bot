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


def notify_trade_opened(symbol: str, entry: float, qty: float,
                        sl: float, tp: float, reason: str):
    _post(
        f"🟢 <b>BUY abierto — {_esc(symbol)}</b>\n"
        f"Entrada: ${entry:,.2f} · Cantidad: {qty}\n"
        f"SL: ${sl:,.2f} · TP: ${tp:,.2f}\n"
        f"<i>{_esc(reason)}</i>"
    )


def notify_trade_closed(symbol: str, entry: float, exit_price: float,
                        pnl_usdt: float, pnl_pct: float, reason: str):
    emoji = "✅" if pnl_usdt >= 0 else "❌"
    _post(
        f"{emoji} <b>Trade cerrado — {_esc(symbol)}</b> ({_esc(reason)})\n"
        f"${entry:,.2f} → ${exit_price:,.2f}\n"
        f"PnL: {pnl_usdt:+,.2f} ({pnl_pct*100:+.2f}%)"
    )


def notify_info(message: str):
    _post(f"ℹ️ {_esc(message)}")


def notify_error(message: str):
    _post(f"⚠️ <b>Bot — Error</b>\n{_esc(message[:3500])}")
