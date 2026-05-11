"""
Alertas vía Discord webhook. Solo envía si DISCORD_WEBHOOK_URL está configurado.

Cómo crear el webhook:
  1. Servidor Discord → Configuración del canal → Integraciones → Webhooks
  2. Nuevo webhook → copiar URL
  3. Añadir a .env: DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
"""
import os
import logging
import threading
import requests

logger = logging.getLogger("bot")

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
TIMEOUT = 5  # segundos


def _post(content: str, embed: dict | None = None):
    """Envía un mensaje a Discord en background thread (no bloquea el bot)."""
    if not WEBHOOK_URL:
        return

    payload = {"content": content}
    if embed:
        payload["embeds"] = [embed]

    def _send():
        try:
            requests.post(WEBHOOK_URL, json=payload, timeout=TIMEOUT)
        except Exception as e:
            logger.warning("Discord webhook falló: %s", e)

    threading.Thread(target=_send, daemon=True).start()


def notify_trade_opened(symbol: str, entry: float, qty: float,
                        sl: float, tp: float, reason: str):
    embed = {
        "title": f"🟢 BUY abierto — {symbol}",
        "color": 0x10B981,  # verde
        "fields": [
            {"name": "Entrada",    "value": f"${entry:,.2f}", "inline": True},
            {"name": "Cantidad",   "value": f"{qty}",         "inline": True},
            {"name": "Stop Loss",  "value": f"${sl:,.2f}",    "inline": True},
            {"name": "Take Profit","value": f"${tp:,.2f}",    "inline": True},
            {"name": "Razón",      "value": reason,           "inline": False},
        ],
    }
    _post("", embed)


def notify_trade_closed(symbol: str, entry: float, exit_price: float,
                        pnl_usdt: float, pnl_pct: float, reason: str):
    color = 0x10B981 if pnl_usdt >= 0 else 0xEF4444
    emoji = "✅" if pnl_usdt >= 0 else "❌"
    embed = {
        "title": f"{emoji} Trade cerrado — {symbol}",
        "color": color,
        "fields": [
            {"name": "Entrada",  "value": f"${entry:,.2f}",      "inline": True},
            {"name": "Salida",   "value": f"${exit_price:,.2f}", "inline": True},
            {"name": "Razón",    "value": reason,                 "inline": True},
            {"name": "PnL USDT", "value": f"{pnl_usdt:+,.2f}",   "inline": True},
            {"name": "PnL %",    "value": f"{pnl_pct*100:+.2f}%", "inline": True},
        ],
    }
    _post("", embed)


def notify_error(message: str):
    embed = {
        "title": "⚠️ Bot — Error",
        "color": 0xF59E0B,
        "description": message[:1900],
    }
    _post("", embed)
