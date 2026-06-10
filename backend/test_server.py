"""Integration tests for the pager node HTTP/WS API and store-and-forward."""
import os
import tempfile
import importlib

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Isolate each test run with its own SQLite file.
    db_file = tmp_path / "pager_test.db"
    monkeypatch.setenv("NODE_ID", "node-test")
    import server
    importlib.reload(server)
    server.DB_PATH = str(db_file)
    server.init_db()
    from fastapi.testclient import TestClient
    with TestClient(server.app) as c:
        yield c, server


def test_register_returns_high_entropy_ssid(client):
    c, server = client
    r = c.post("/register", json={"display_name": "Neo"})
    assert r.status_code == 200
    ssid = r.json()["ss_id"]
    # ss-XXXXXXXX-pager : 8 hex chars = 32 bits
    hexpart = ssid.split("-")[1]
    assert len(hexpart) == 8


def test_message_rejected_when_too_long(client):
    c, server = client
    r = c.post("/message", json={"text": "x" * 9000, "target": "ss-aaaa-pager", "from_ss": "ss-bbbb-pager"})
    assert r.json()["ok"] is False


def test_store_and_forward_delivers_on_reconnect(client):
    c, server = client
    # Register a recipient locally so the router treats it as a local contact.
    recipient = c.post("/register", json={"display_name": "Trinity"}).json()["ss_id"]

    # Send a message while recipient is offline → should be stored.
    r = c.post("/message", json={"text": "wake up", "target": recipient, "from_ss": "ss-cccc-pager"})
    body = r.json()
    assert body["ok"] is True
    assert body["route"] in ("stored", "local")

    # Recipient connects → pending message flushed over the socket.
    with c.websocket_connect(f"/ws/{recipient}") as ws:
        data = ws.receive_json()
        assert data["text"] == "wake up"
        assert data.get("pending") is True


def test_pending_marked_delivered_after_flush(client):
    c, server = client
    recipient = c.post("/register", json={"display_name": "R"}).json()["ss_id"]
    c.post("/message", json={"text": "msg1", "target": recipient, "from_ss": "ss-dddd-pager"})

    assert len(server.pending_messages_for(recipient)) == 1
    with c.websocket_connect(f"/ws/{recipient}") as ws:
        ws.receive_json()
    # After flush, nothing pending.
    assert server.pending_messages_for(recipient) == []


def test_mesh_ingest_duplicate_is_idempotent(client):
    c, server = client
    recipient = c.post("/register", json={"display_name": "Dup"}).json()["ss_id"]
    envelope = {
        "msg_id": "fixed-id-123",
        "from_ss": "ss-eeee-pager",
        "target_ss": recipient,
        "text": "hello",
        "ttl": 5,
        "path": ["node-other"],
    }
    c.post("/mesh/ingest", json=envelope)
    c.post("/mesh/ingest", json=envelope)  # duplicate
    # Only one row stored despite two ingests.
    conn = server.get_db()
    count = conn.execute("SELECT COUNT(*) AS n FROM messages WHERE msg_id = ?", ("fixed-id-123",)).fetchone()["n"]
    conn.close()
    assert count == 1


def test_mesh_hello_learns_peer_url(client):
    c, server = client
    r = c.post("/mesh/hello", json={
        "node_id": "node-remote",
        "contacts": [{"ss_id": "ss-ffff-pager", "display_name": "Far"}],
        "timestamp": 123.0,
        "self_url": "http://10.0.0.9:9009",
    })
    assert r.status_code == 200
    assert server.mesh.url_for("node-remote") == "http://10.0.0.9:9009"
    # And the contact route was learned by the router.
    assert "node-remote" in server.router.routes.get("ss-ffff-pager", set())


def test_health_ok(client):
    c, server = client
    assert c.get("/health").json()["status"] == "ok"


def test_pubkey_publish_and_fetch(client):
    c, server = client
    r = c.post("/keys", json={"ss_id": "ss-key-pager", "pubkey": '{"kty":"EC","crv":"P-256","x":"a","y":"b"}'})
    assert r.json()["ok"] is True
    r2 = c.get("/keys/ss-key-pager")
    assert r2.json()["ok"] is True
    assert '"P-256"' in r2.json()["pubkey"]
    # Unknown key → ok: False
    assert c.get("/keys/ss-none-pager").json()["ok"] is False


def test_pubkey_overwrite_updates(client):
    c, server = client
    c.post("/keys", json={"ss_id": "ss-k2-pager", "pubkey": "old"})
    c.post("/keys", json={"ss_id": "ss-k2-pager", "pubkey": "new"})
    assert c.get("/keys/ss-k2-pager").json()["pubkey"] == "new"


def test_call_signal_relayed_between_clients(client):
    c, server = client
    with c.websocket_connect("/ws/ss-caller") as caller, \
         c.websocket_connect("/ws/ss-callee") as callee:
        caller.send_json({
            "type": "call_signal",
            "target": "ss-callee",
            "payload": {"kind": "offer", "sdp": "fake-sdp"},
        })
        got = callee.receive_json()
        assert got["type"] == "call_signal"
        assert got["from_ss"] == "ss-caller"
        assert got["payload"]["kind"] == "offer"
        assert got["payload"]["sdp"] == "fake-sdp"


def test_call_signal_unavailable_when_target_offline(client):
    c, server = client
    with c.websocket_connect("/ws/ss-caller") as caller:
        caller.send_json({
            "type": "call_signal",
            "target": "ss-ghost",
            "payload": {"kind": "offer", "sdp": "x"},
        })
        got = caller.receive_json()
        assert got["payload"]["kind"] == "unavailable"
        assert got["from_ss"] == "ss-ghost"


def test_register_rate_limited(client):
    c, server = client
    codes = [c.post("/register", json={"display_name": f"u{i}"}).status_code for i in range(12)]
    assert 429 in codes


def test_message_rate_limit_allows_normal_traffic(client):
    c, server = client
    r = c.post("/message", json={"text": "hi", "target": "ss-aaaa-pager", "from_ss": "ss-bbbb-pager"})
    assert r.status_code == 200
