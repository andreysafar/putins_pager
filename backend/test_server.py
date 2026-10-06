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


def test_nezhri_bridge_forwards_with_label(client, monkeypatch):
    """notify_nezhri_telegram posts to NeZhri with the sender's label and key."""
    import asyncio
    c, server = client
    sender = c.post("/register", json={"display_name": "Капитан"}).json()["ss_id"]
    server.NEZHRI_NOTIFY_URL = "https://nezhri.example/api/pager/notify"
    server.NEZHRI_NOTIFY_API_KEY = "k"

    captured = {}

    class _Resp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Resp()

    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeClient)
    asyncio.get_event_loop().run_until_complete(
        server.notify_nezhri_telegram(sender, "ss-deadbeef-pager", "Привет")
    )
    assert captured["url"] == server.NEZHRI_NOTIFY_URL
    assert captured["json"]["to_ssid"] == "ss-deadbeef-pager"
    assert captured["json"]["from_name"] == "Капитан"
    assert captured["headers"]["X-API-Key"] == "k"


def test_message_still_ok_with_bridge_unconfigured(client):
    """/message keeps working when the NeZhri bridge is not configured."""
    c, server = client
    server.NEZHRI_NOTIFY_URL = ""
    r = c.post("/message", json={"from_ss": "ss-aaaa-pager", "target": "ss-bbbb-pager", "text": "hi"})
    assert r.json()["ok"] is True


# --- phone book, sessions, Telegram «продолжить диалог» -------------------
def _capture_notify(server, monkeypatch):
    server.NEZHRI_NOTIFY_URL = "https://nezhri.example/api/pager/notify"
    server.NEZHRI_NOTIFY_API_KEY = "k"
    server._last_notified.clear()
    sent = []

    async def fake(from_ss, to_ss, text, *, msg_id=""):
        sent.append((from_ss, to_ss, text))
        await real(from_ss, to_ss, text, msg_id=msg_id)

    real = server.notify_nezhri_telegram
    posted = []

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None, headers=None):
            posted.append(json)

    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(server, "notify_nezhri_telegram", fake)
    return sent, posted


def _wait_for(items, n=1):
    """The Telegram nudge is fire-and-forget; give the app loop a moment."""
    import time as _t
    for _ in range(100):
        if len(items) >= n:
            return items
        _t.sleep(0.02)
    return items


def test_offline_recipient_gets_telegram_link_that_starts_a_session(client, monkeypatch):
    c, server = client
    sent, posted = _capture_notify(server, monkeypatch)
    athlete = c.post("/register", json={"display_name": "Атлет"}).json()["ss_id"]
    fan = c.post("/register", json={"display_name": "Фанат"}).json()["ss_id"]

    c.post("/message", json={"text": "го плавать", "target": athlete, "from_ss": fan})
    c.post("/message", json={"text": "ау", "target": athlete, "from_ss": fan})
    _wait_for(posted)
    assert len(posted) == 1  # a burst is one Telegram nudge
    p = posted[0]
    assert p["to_ssid"] == athlete and p["from_name"] == "Фанат" and p["text"] == "го плавать"
    assert "login=" in p["continue_url"] and f"to={fan}" in p["continue_url"]

    code = p["continue_url"].split("login=")[1].split("&")[0]
    claim = c.post("/session/claim", json={"code": code}).json()
    assert claim["ok"] and claim["ss_id"] == athlete and claim["to"] == fan
    assert claim["to_name"] == "Фанат"
    # The continued dialog has its history.
    hist = c.get(f"/messages/{athlete}", params={"with": fan}).json()
    assert [m["text"] for m in hist] == ["ау", "го плавать"]
    assert c.post("/session/claim", json={"code": "nope"}).status_code == 404


def test_online_recipient_is_not_nudged_in_telegram(client, monkeypatch):
    c, server = client
    sent, posted = _capture_notify(server, monkeypatch)
    athlete = c.post("/register", json={"display_name": "A"}).json()["ss_id"]
    with c.websocket_connect(f"/ws/{athlete}") as ws:
        r = c.post("/message", json={"text": "hi", "target": athlete, "from_ss": "ss-fan"}).json()
        assert r["route"] == "local"
        assert ws.receive_json()["text"] == "hi"
    assert _wait_for(sent) == []


def test_encrypted_text_is_not_leaked_to_telegram(client, monkeypatch):
    c, server = client
    sent, posted = _capture_notify(server, monkeypatch)
    athlete = c.post("/register", json={"display_name": "A"}).json()["ss_id"]
    c.post("/message", json={"text": '{"enc":1,"n":"x","c":"y"}', "target": athlete, "from_ss": "ss-f"})
    _wait_for(posted)
    assert posted[0]["encrypted"] is True and posted[0]["text"] == ""


def test_athlete_registration_needs_key_and_is_idempotent(client):
    c, server = client
    server.DIRECTORY_API_KEY = "dir-key"
    plain = c.post("/register", json={"display_name": "X", "kind": "athlete", "athlete_id": "42"}).json()
    assert plain["kind"] == "user"
    h = {"X-API-Key": "dir-key"}
    first = c.post("/register", json={"display_name": "Ironman", "athlete_id": "42"}, headers=h).json()
    again = c.post("/register", json={"display_name": "Ironman", "athlete_id": "42"}, headers=h).json()
    assert first["kind"] == "athlete" and first["ss_id"] == again["ss_id"]


def test_master_marks_mongo_athletes(client):
    c, server = client
    ssid = c.post("/register", json={"display_name": "old"}).json()["ss_id"]
    server.upsert_athletes([{"ss_id": ssid, "athlete_id": "7", "display_name": "Ironman"},
                            {"ss_id": "ss-0badc0de-pager", "athlete_id": "8", "display_name": "New"}])
    kinds = {x["ss_id"]: x["kind"] for x in c.get("/contacts").json()}
    assert kinds[ssid] == "athlete" and kinds["ss-0badc0de-pager"] == "athlete"


def test_phonebook_shows_hierarchy_and_favorites(client):
    c, server = client
    me = c.post("/register", json={"display_name": "Me"}).json()["ss_id"]
    buddy = c.post("/register", json={"display_name": "Buddy"}).json()["ss_id"]
    c.post("/mesh/hello", json={
        "node_id": "node-b", "timestamp": 1.0, "self_url": "http://b:9009",
        "contacts": [{"ss_id": "ss-b1", "display_name": "Bee", "status": "online"},
                     {"ss_id": "ss-c1", "display_name": "Sea", "node_id": "node-c", "hops": 1}],
        "neighbors": [{"node_id": "node-c", "url": "http://c:9009"}],
    })
    assert c.post(f"/favorites/{me}", json={"contact": "ss-c1"}).json()["ok"]
    book = c.get("/phonebook", params={"ss_id": me}).json()
    assert [x["ss_id"] for x in book["favorites"]["contacts"]] == ["ss-c1"]
    assert book["favorites"]["contacts"][0]["display_name"] == "Sea"
    here = book["nearby"]["nodes"][0]
    assert here["self"] and [x["ss_id"] for x in here["contacts"]] == [buddy]
    assert [g["node_id"] for g in book["nearby"]["nodes"][1:]] == ["node-b"]
    assert [g["node_id"] for g in book["remote"]["nodes"]] == ["node-c"]
    c.delete(f"/favorites/{me}/ss-c1")
    assert c.get(f"/favorites/{me}").json() == []


def test_hello_reply_carries_hops_and_neighbors(client):
    c, server = client
    c.post("/register", json={"display_name": "Local"})
    c.post("/mesh/hello", json={"node_id": "node-b", "timestamp": 1.0, "self_url": "http://b:9009",
                                "contacts": [{"ss_id": "ss-b1"}]})
    reply = c.post("/mesh/hello", json={"node_id": "node-x", "timestamp": 1.0,
                                        "self_url": "http://x:9009", "contacts": []}).json()
    by_id = {x["ss_id"]: x for x in reply["contacts"]}
    assert by_id["ss-b1"]["hops"] == 1  # node-x learns it as 2 hops away
    assert any(x["hops"] == 0 for x in reply["contacts"])
    assert {n["node_id"] for n in reply["neighbors"]} == {"node-b"}
    # and the inbound peer is remembered for our own hellos
    assert "http://b:9009" in server.peer_urls()


def test_mesh_route_reports_local_and_remote_status(client):
    c, server = client
    me = c.post("/register", json={"display_name": "Me"}).json()["ss_id"]
    assert c.get(f"/mesh/route/{me}").json()["online"] is False
    with c.websocket_connect(f"/ws/{me}"):
        r = c.get(f"/mesh/route/{me}").json()
        assert r["online"] is True and r["locally_registered"] is True
    c.post("/mesh/hello", json={"node_id": "node-b", "timestamp": 1.0, "self_url": "http://b:9009",
                                "contacts": [{"ss_id": "ss-b1", "status": "zombie"}]})
    r = c.get("/mesh/route/ss-b1").json()
    assert (r["status"], r["hops"], r["node_id"]) == ("zombie", 1, "node-b")


def test_clean_disconnect_is_offline_and_dead_sessions_are_reaped(client):
    c, server = client
    me = c.post("/register", json={"display_name": "Me"}).json()["ss_id"]
    with c.websocket_connect(f"/ws/{me}") as ws:
        ws.send_json({"type": "ping", "ts": 1})
        assert ws.receive_json()["type"] == "pong"
        sess = server.ws_mgr.presence.sessions_of(me)[0]
        sess.last_seen -= server.dirmod.ZOMBIE_TIMEOUT + 1
        assert server.ws_mgr.get_status(me) == "zombie"
        import asyncio
        assert asyncio.run(server.ws_mgr.reap()) == 1
        assert server.ws_mgr.get_status(me) == "offline"
    with c.websocket_connect(f"/ws/{me}"):
        pass
    assert server.ws_mgr.get_status(me) == "offline"
    assert server.ws_mgr.presence.sessions == {}


def test_cleanup_drops_unused_anonymous_ssids_only(client):
    import time as _t
    c, server = client
    ghost = c.post("/register", json={"display_name": "ghost"}).json()["ss_id"]
    talker = c.post("/register", json={"display_name": "talker"}).json()["ss_id"]
    starred = c.post("/register", json={"display_name": "starred"}).json()["ss_id"]
    server.upsert_athletes([{"ss_id": "ss-a7a7a7a7-pager", "athlete_id": "1", "display_name": "Ath"}])
    c.post("/message", json={"text": "hi", "target": "ss-nobody", "from_ss": talker})
    c.post(f"/favorites/{talker}", json={"contact": starred})
    later = _t.time() + (server.ANON_TTL_DAYS + 1) * 86400
    removed = server.cleanup_dead(now=later)
    left = {x["ss_id"] for x in c.get("/contacts").json()}
    assert ghost not in left
    assert {talker, starred, "ss-a7a7a7a7-pager"} <= left
    assert removed["ssids"] == 1


def test_sent_message_is_echoed_to_the_senders_other_tabs_with_client_id(client):
    c, server = client
    me = c.post("/register", json={"display_name": "Me"}).json()["ss_id"]
    peer = c.post("/register", json={"display_name": "Peer"}).json()["ss_id"]
    mid = "ab" * 16
    with c.websocket_connect(f"/ws/{me}") as other_tab, c.websocket_connect(f"/ws/{peer}") as p:
        r = c.post("/message", json={"text": "hi", "target": peer, "from_ss": me, "msg_id": mid}).json()
        assert r["msg_id"] == mid
        echo = other_tab.receive_json()
        while echo.get("type") == "presence":  # the peer's arrival comes first
            echo = other_tab.receive_json()
        assert (echo["type"], echo["to_ss"], echo["msg_id"], echo["text"]) == ("sent", peer, mid, "hi")
        got = p.receive_json()
        assert got["msg_id"] == mid and "type" not in got
    # a malformed client id is replaced by a server one
    r = c.post("/message", json={"text": "x", "target": peer, "from_ss": me, "msg_id": "nope"}).json()
    assert len(r["msg_id"]) == 32 and r["msg_id"] != "nope"
