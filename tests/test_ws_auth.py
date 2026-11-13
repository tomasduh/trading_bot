"""
El WebSocket del dashboard se autentica con el token como PRIMER MENSAJE, no en
la URL (una URL con ?token= queda en los logs de acceso).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src import api, bot

TOKEN = "token-de-prueba"
ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api, "DASHBOARD_TOKEN", TOKEN)
    monkeypatch.setattr(api, "WS_AUTH_TIMEOUT", 0.3)
    monkeypatch.setattr(api, "_build_status", lambda: {"ok": True})
    monkeypatch.setattr(api, "_build_signals", lambda: [])
    monkeypatch.setattr(api, "_build_trades", lambda: [])
    monkeypatch.setattr(api, "_build_log", lambda n: [])
    # Sin `with`: no corre el lifespan, así que no arranca el loop del bot.
    return TestClient(api.app)


def _first_message(client, url="/ws", auth=None):
    with client.websocket_connect(url, headers=ORIGIN) as ws:
        if auth is not None:
            ws.send_json(auth)
        return ws.receive_json()


def test_valid_token_in_first_message_gets_data(client):
    msg = _first_message(client, auth={"type": "auth", "token": TOKEN})
    assert msg == {"type": "status", "data": {"ok": True}}


def test_wrong_token_is_rejected(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        _first_message(client, auth={"type": "auth", "token": "otro"})
    assert exc.value.code == 1008


def test_token_in_url_alone_no_longer_authenticates(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        _first_message(client, url=f"/ws?token={TOKEN}")
    assert exc.value.code == 1008


def test_garbage_first_message_is_rejected(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers=ORIGIN) as ws:
            ws.send_text("ping")
            ws.receive_json()


def test_unauthenticated_socket_is_never_registered_for_broadcasts(client):
    before = len(api.manager.active)
    with pytest.raises(WebSocketDisconnect):
        _first_message(client, auth={"type": "auth", "token": "otro"})
    assert len(api.manager.active) == before


def test_foreign_origin_is_rejected_even_with_token(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.send_json({"type": "auth", "token": TOKEN})
            ws.receive_json()


# ── Watchdog ─────────────────────────────────────────────────────────────────

def test_watchdog_is_pinged_while_paused(monkeypatch):
    pings = []
    monkeypatch.setattr(bot, "is_paused", lambda: True)
    monkeypatch.setattr(bot, "manage_exits_while_paused", lambda ex: None)
    monkeypatch.setattr(bot, "_ping_watchdog", lambda: pings.append(1))
    bot.run_cycle(executor=None)
    assert pings == [1]
