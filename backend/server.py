#!/usr/bin/env python3
import os
import sqlite3
import secrets
import asyncio
import tarfile
import io
import uuid
import json
import re
import time
import httpx
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Query, UploadFile, File as FastAPIFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mesh_router import MeshMessage, MeshRouter
import directory as dirmod
from directory import Contact, Directory, Presence, valid_ssid

# MongoDB is an optional integration (athlete directory). Import lazily so the
# pager node still runs when pymongo is missing or its TLS backend is broken.
try:
    import pymongo
except BaseException as _e:  # noqa: BLE001 - a broken native TLS backend raises pyo3 panics, not Exception
    pymongo = None

# --- Config ---
PORT = 9009
DB_PATH = "pager.db"
BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/fitness_bot")

# --- Mesh Config ---
# Neighbor nodes: list of base URLs this node can reach for mesh routing
MESH_PEERS = json.loads(os.getenv("MESH_PEERS", "[]"))  # e.g. ["http://10.0.0.2:9009","http://10.0.0.3:9009"]
MAIN_NODE_URL = os.getenv("MAIN_NODE_URL", "").rstrip("/")  # URL главной ноды для авто-регистрации
# The master node (pager.iron-siber.ru) is the one without a MAIN_NODE_URL, or
# any node started with MASTER_NODE=1. It keeps the athlete directory (SSID ↔
# Iron Siber athlete) synced from NeZhri's MongoDB and advertises it to the mesh.
IS_MASTER = os.getenv("MASTER_NODE", "").lower() in ("1", "true", "yes") or not MAIN_NODE_URL
# This node's externally-reachable URL, advertised to peers so they can route
# back to us. Falls back to localhost for single-node dev.
NODE_URL = os.getenv("NODE_URL", f"http://localhost:{PORT}").rstrip("/")
# Where people open the web client (links in Telegram notifications).
PUBLIC_URL = os.getenv("PUBLIC_URL", NODE_URL).rstrip("/")
MESH_PING_INTERVAL = 30  # seconds

# Dead-session housekeeping.
ANON_TTL_DAYS = int(os.getenv("ANON_TTL_DAYS", "14"))  # unused anonymous SSIDs live this long
LOGIN_CODE_TTL = 72 * 3600  # «продолжить диалог» links from Telegram
PEER_FORGET_AFTER = 24 * 3600  # a discovered node silent this long is dropped from the peer list
NOTIFY_COOLDOWN = 300  # one Telegram nudge per sender→recipient pair per 5 min

# --- NeZhri Telegram bridge ---
# When a "написать спортсмену" message is sent from the Iron Siber leaderboard,
# we also forward it to the recipient's Telegram via the NeZhri bot so they get
# it even if they're not on the pager mesh right now. Best-effort, fire-and-forget.
NEZHRI_NOTIFY_URL = os.getenv("NEZHRI_NOTIFY_URL", "")  # e.g. https://safargaleev.com/api/pager/notify
NEZHRI_NOTIFY_API_KEY = os.getenv("NEZHRI_NOTIFY_API_KEY", "")
# NeZhri registers athletes with this key (X-API-Key) so a plain visitor can't
# pose as an athlete. Defaults to the bridge key — it is the same trust pair.
DIRECTORY_API_KEY = os.getenv("DIRECTORY_API_KEY", "") or NEZHRI_NOTIFY_API_KEY

# --- DB ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# --- Mongo Integration (master only) ---
_mongo_client = None


def fetch_athletes_from_mongo() -> list:
    """NeZhri users that have a pager SSID — the Iron Siber athlete links.

    Blocking (pymongo); call from a thread. One cached client: opening a new
    MongoClient per request leaked sockets and cost ~2 s on every /contacts.
    """
    global _mongo_client
    if pymongo is None or not os.getenv("MONGODB_URI"):
        return []
    try:
        if _mongo_client is None:
            _mongo_client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        db = _mongo_client.get_database()
        users = db.users.find(
            {"pager_ssid": {"$nin": [None, ""]}},
            {"_id": 0, "user_id": 1, "username": 1, "first_name": 1, "pager_ssid": 1, "display_name": 1},
        )
        result = []
        for u in users:
            ssid = str(u.get("pager_ssid") or "")
            if not valid_ssid(ssid):
                continue
            result.append({
                "ss_id": ssid,
                "athlete_id": str(u.get("user_id") or ""),
                "display_name": u.get("display_name") or u.get("first_name") or u.get("username") or "ATHLETE",
            })
        return result
    except Exception as e:
        print(f"Mongo error: {e}")
        return []


def upsert_athletes(athletes: list) -> int:
    """Mark SSIDs as athletes in the local directory (insert missing ones)."""
    if not athletes:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    try:
        for a in athletes:
            conn.execute(
                "INSERT INTO ssids (ssid, label, created_at, kind, athlete_id) VALUES (?, ?, ?, 'athlete', ?) "
                "ON CONFLICT(ssid) DO UPDATE SET kind = 'athlete', athlete_id = excluded.athlete_id, "
                "label = CASE WHEN ssids.label = '' THEN excluded.label ELSE ssids.label END",
                (a["ss_id"], a.get("display_name", ""), now, a.get("athlete_id", "")),
            )
        conn.commit()
    finally:
        conn.close()
    return len(athletes)

# --- Sessions & presence ---
class WSManager:
    """Live WebSocket sessions. Status logic lives in `directory.Presence`."""

    def __init__(self):
        self.presence = Presence()

    def get_status(self, ss_id: str) -> str:
        return self.presence.status(ss_id)

    async def send_to(self, ss_id: str, data: dict) -> int:
        sent = 0
        for s in self.presence.sessions_of(ss_id):
            try:
                await s.ws.send_json(data)
                sent += 1
            except Exception:
                self.presence.close(s.sid, abnormal=True)
        return sent

    async def broadcast(self, data: dict, exclude: str = ""):
        for s in list(self.presence.sessions.values()):
            if s.sid == exclude:
                continue
            try:
                await s.ws.send_json(data)
            except Exception:
                self.presence.close(s.sid, abnormal=True)

    async def broadcast_changes(self, exclude: str = ""):
        """Tell everyone whose status changed — only the diff, only when it changes."""
        diff = self.presence.changes()
        if diff:
            await self.broadcast({"type": "presence",
                                  "contacts": [{"ss_id": k, "status": v} for k, v in diff.items()]},
                                 exclude=exclude)

    async def reap(self) -> int:
        """Close sessions that stopped answering — dead sessions must not pile up."""
        dead = self.presence.dead_sessions()
        for s in dead:
            self.presence.close(s.sid)
            try:
                await s.ws.close(code=4000, reason="heartbeat timeout")
            except Exception:
                pass
            touch_ssid(s.ss_id)
        self.presence.prune()
        return len(dead)

    async def kill_all(self):
        """Close all active WebSocket connections."""
        for s in list(self.presence.sessions.values()):
            try:
                await s.ws.close(code=1001, reason="Server shutdown")
            except Exception:
                pass
            self.presence.close(s.sid)


ws_mgr = WSManager()


def resolve_node_id() -> str:
    """NODE_ID from env, else one generated once and kept in the DB.

    A random id per restart made every restart look like a brand-new node to
    the peers, while the old id lingered there as a zombie.
    """
    env = os.getenv("NODE_ID")
    if env:
        return env
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute("SELECT value FROM meta WHERE key = 'node_id'").fetchone()
        if row:
            return row[0]
        node_id = f"node-{secrets.token_hex(3)}"
        conn.execute("INSERT INTO meta (key, value) VALUES ('node_id', ?)", (node_id,))
        conn.commit()
        return node_id
    finally:
        conn.close()


NODE_ID = resolve_node_id()


# --- Mesh Network State ---
class MeshNetwork:
    """Peer nodes and the contacts behind them (see `directory.Directory`)."""

    def __init__(self):
        self.dir = Directory(NODE_ID)

    @property
    def peers(self) -> dict:
        return {nid: {"url": p.url, "last_seen": p.last_seen, "status": self.dir.peer_status(nid)}
                for nid, p in self.dir.peers.items()}

    def update_peer(self, node_id: str, url: str, contacts: list, neighbors: list = ()):
        self.dir.ingest_hello(node_id, url, contacts, neighbors)
        if node_id in self.dir.peers:
            router.set_routes_via(node_id, self.dir.vias_for(node_id))
            save_peer(node_id, self.dir.peers[node_id].url, "seen")

    def available_peer_ids(self) -> list:
        return self.dir.live_peer_ids()

    def url_for(self, node_id: str) -> Optional[str]:
        return self.dir.url_for(node_id)

    def drop_dead(self):
        for nid in self.dir.drop_dead_peers():
            router.forget_node(nid)

    def get_status(self) -> dict:
        nodes = self.dir.known_nodes()
        return {
            "node_id": NODE_ID,
            "is_master": IS_MASTER,
            "total_peers": len(self.dir.peers),
            "online_peers": len(self.dir.live_peer_ids()),
            "known_routes": len(self.dir.remote_contacts()),
            "known_nodes": len(nodes),
            "peers": self.peers,
            "presence": ws_mgr.presence.stats(),
        }


# Router holds the pure routing logic; mesh holds peer transport state.
router = MeshRouter(NODE_ID)
mesh = MeshNetwork()


def local_contacts() -> list[Contact]:
    """People registered on this node, with their live status."""
    conn = get_db()
    rows = conn.execute("SELECT ssid, label, kind, athlete_id FROM ssids").fetchall()
    conn.close()
    return [Contact(ss_id=r["ssid"], display_name=r["label"] or r["ssid"], node_id=NODE_ID,
                    hops=0, status=ws_mgr.get_status(r["ssid"]),
                    kind=r["kind"] or "user", athlete_id=r["athlete_id"] or "")
            for r in rows]


def local_ssids() -> set:
    conn = get_db()
    rows = conn.execute("SELECT ssid FROM ssids").fetchall()
    conn.close()
    return {r["ssid"] for r in rows}


def sync_router_local_ssids() -> None:
    """Keep the router's view of locally-registered contacts current."""
    router.set_local_ssids(local_ssids())


async def forward_to_peers(msg: MeshMessage, node_ids: list) -> list:
    """POST a mesh envelope to each peer's /mesh/ingest. Returns delivered node_ids."""
    delivered = []
    for nid in node_ids:
        url = mesh.url_for(nid)
        if not url:
            continue
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(f"{url}/mesh/ingest", json=msg.to_dict())
                if resp.status_code == 200:
                    delivered.append(nid)
        except Exception:
            # Peer unreachable right now — message stays stored for retry.
            pass
    return delivered


async def deliver_local(msg_id: str, from_ss: str, to_ss: str, text: str, created_at: str) -> bool:
    """Push to the recipient's live sessions; otherwise nudge them in Telegram."""
    sent = await ws_mgr.send_to(to_ss, {
        "text": text, "from_ss": from_ss, "to_ss": to_ss,
        "created_at": created_at, "msg_id": msg_id,
    })
    online = ws_mgr.get_status(to_ss) == dirmod.STATUS_ONLINE
    if sent and online:
        mark_delivered(msg_id, to_ss)
    if not online:
        # Nobody is reading the pager right now (no session, or only a zombie
        # one — whose socket may swallow the frame, so the message also stays
        # pending for the next connect): offer a new session from Telegram.
        asyncio.create_task(notify_nezhri_telegram(from_ss, to_ss, text, msg_id=msg_id))
    return bool(sent) and online


async def route_mesh_message(msg: MeshMessage) -> dict:
    """Run a message through the router and act on the decision."""
    sync_router_local_ssids()
    decision = router.handle(msg, available_peers=mesh.available_peer_ids())
    result = {"msg_id": msg.msg_id, "reason": decision.reason,
              "delivered_local": False, "forwarded": []}

    if decision.deliver_locally:
        # Only mark delivered if a live socket actually receives it; otherwise
        # it stays pending so store-and-forward pushes it on reconnect.
        result["delivered_local"] = await deliver_local(
            msg.msg_id, msg.from_ss, msg.target_ss, msg.text,
            datetime.now(timezone.utc).isoformat())

    if decision.forward_to:
        msg.ttl -= 1
        result["forwarded"] = await forward_to_peers(msg, decision.forward_to)

    return result

# --- Mesh Background Tasks ---
def save_peer(node_id: str, url: str, source: str) -> None:
    """Remember a peer URL across restarts (config/register/inbound/seen)."""
    if not url.startswith("http") or len(url) > 256 or url.rstrip("/") == NODE_URL:
        return
    now = time.time()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO peers (url, node_id, source, first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET node_id = CASE WHEN excluded.node_id != '' "
            "THEN excluded.node_id ELSE peers.node_id END, last_seen = excluded.last_seen",
            (url.rstrip("/"), node_id or "", source, now, now),
        )
        if node_id:
            # One node, one URL: drop the stale address of a node that moved.
            conn.execute("DELETE FROM peers WHERE node_id = ? AND url != ? AND source != 'config'",
                         (node_id, url.rstrip("/")))
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def peer_urls() -> list[str]:
    """Everyone we should say hello to: config, master, and nodes we've met."""
    urls = [u.rstrip("/") for u in MESH_PEERS if isinstance(u, str)]
    if MAIN_NODE_URL:
        urls.append(MAIN_NODE_URL)
    conn = get_db()
    try:
        urls += [r["url"] for r in conn.execute("SELECT url FROM peers").fetchall()]
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    seen, out = set(), []
    for u in urls:
        if u.startswith("http") and u != NODE_URL and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def hello_payload(for_node: str = "") -> dict:
    return {
        "node_id": NODE_ID,
        "contacts": mesh.dir.advert_for(for_node, local_contacts()),
        "neighbors": mesh.dir.neighbors_advert(for_node),
        "is_master": IS_MASTER,
        "timestamp": time.time(),
        "self_url": NODE_URL,
    }


async def hello_once(client: httpx.AsyncClient, peer_url: str) -> None:
    known = next((nid for nid, p in mesh.dir.peers.items() if p.url == peer_url), "")
    try:
        resp = await client.post(f"{peer_url}/mesh/hello", json=hello_payload(known))
        if resp.status_code == 200:
            data = resp.json()
            mesh.update_peer(str(data.get("node_id", "")), peer_url,
                             data.get("contacts", []), data.get("neighbors", []))
    except Exception:
        pass  # silence ages the peer: online → zombie → dropped


async def mesh_ping_loop():
    """Say hello to every known peer, in parallel, and age out the silent ones."""
    while True:
        try:
            urls = peer_urls()
            if urls:
                async with httpx.AsyncClient(timeout=5) as client:
                    await asyncio.gather(*(hello_once(client, u) for u in urls))
            mesh.drop_dead()
        except Exception as e:
            print(f"[mesh] ping loop error: {e}")
        await asyncio.sleep(MESH_PING_INTERVAL)


# --- Pydantic Models ---
class MessageReq(BaseModel):
    text: str
    target: str
    from_ss: str = ""
    # Client-made id (32 hex): the sending tab knows it before the reply, so
    # the echo to its other tabs never shows the message twice.
    msg_id: str = ""

class RegisterReq(BaseModel):
    label: str = ""
    name: str = ""
    display_name: str = ""
    # Honoured only with X-API-Key == DIRECTORY_API_KEY (NeZhri):
    kind: str = ""        # "athlete"
    athlete_id: str = ""  # NeZhri / Telegram user id

class MeshHelloReq(BaseModel):
    node_id: str
    contacts: list
    timestamp: float
    self_url: str = ""  # sender's reachable URL, so we can route back to it
    neighbors: list = []  # sender's own peers — how we find neighbours of neighbours
    is_master: bool = False

class FavoriteReq(BaseModel):
    contact: str
    display_name: str = ""

class ClaimReq(BaseModel):
    code: str

class MeshMessageReq(BaseModel):
    from_ss: str
    target_ss: str
    text: str
    origin_node: str = ""

# --- DB Init ---
def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ssids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ssid TEXT UNIQUE NOT NULL,
            label TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id TEXT,
            from_ss TEXT NOT NULL,
            to_ss TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivered INTEGER DEFAULT 0
        )
    """)
    # Migrate older DBs that predate the msg_id / delivered columns.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "msg_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN msg_id TEXT")
    if "delivered" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN delivered INTEGER DEFAULT 0")
    ssid_cols = {r["name"] for r in conn.execute("PRAGMA table_info(ssids)").fetchall()}
    if "kind" not in ssid_cols:
        conn.execute("ALTER TABLE ssids ADD COLUMN kind TEXT DEFAULT 'user'")
    if "athlete_id" not in ssid_cols:
        conn.execute("ALTER TABLE ssids ADD COLUMN athlete_id TEXT DEFAULT ''")
    if "last_seen_at" not in ssid_cols:
        conn.execute("ALTER TABLE ssids ADD COLUMN last_seen_at TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ssids_athlete ON ssids(athlete_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_from ON messages(from_ss)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_id ON messages(msg_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_to_pending ON messages(to_ss, delivered)")
    # Public keys for E2E encryption (JWK JSON published by clients).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pubkeys (
            ss_id TEXT PRIMARY KEY,
            pubkey TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Phone book: per-owner favourites, kept on the node so they follow the
    # person to a new device (e.g. a session started from Telegram).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            owner TEXT NOT NULL,
            contact TEXT NOT NULL,
            display_name TEXT DEFAULT '',
            added_at TEXT NOT NULL,
            PRIMARY KEY (owner, contact)
        )
    """)
    # «Продолжить диалог» links: a code that starts a session as `ss_id`
    # and opens the chat with `peer`.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS login_codes (
            code TEXT PRIMARY KEY,
            ss_id TEXT NOT NULL,
            peer TEXT DEFAULT '',
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
    """)
    # Peers we've met (survives restarts; MESH_PEERS from env is the seed).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS peers (
            url TEXT PRIMARY KEY,
            node_id TEXT DEFAULT '',
            source TEXT DEFAULT '',
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    conn.close()


def touch_ssid(ss_id: str) -> None:
    """Remember when an SSID last had a session (anonymous-SSID cleanup uses it)."""
    conn = get_db()
    try:
        conn.execute("UPDATE ssids SET last_seen_at = ? WHERE ssid = ?",
                     (datetime.now(timezone.utc).isoformat(), ss_id))
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def cleanup_dead(now: Optional[float] = None) -> dict:
    """Extinguish what nobody uses any more.

    * anonymous SSIDs (not athletes) with no session for ANON_TTL_DAYS, no
      messages and nobody's favourite — every visit from a fresh browser used
      to leave one behind, and they all showed up in the phone book;
    * expired «продолжить диалог» codes;
    * discovered peers silent for PEER_FORGET_AFTER (config peers stay).
    """
    now = now or time.time()
    cutoff = datetime.fromtimestamp(now - ANON_TTL_DAYS * 86400, timezone.utc).isoformat()
    live = {s.ss_id for s in ws_mgr.presence.sessions.values()}
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT ssid FROM ssids s WHERE COALESCE(kind, 'user') != 'athlete' "
            "AND COALESCE(last_seen_at, created_at) < ? "
            "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.from_ss = s.ssid OR m.to_ss = s.ssid) "
            "AND NOT EXISTS (SELECT 1 FROM favorites f WHERE f.contact = s.ssid)",
            (cutoff,),
        ).fetchall()
        dead = [r["ssid"] for r in rows if r["ssid"] not in live]
        for ssid in dead:
            conn.execute("DELETE FROM ssids WHERE ssid = ?", (ssid,))
            conn.execute("DELETE FROM pubkeys WHERE ss_id = ?", (ssid,))
            conn.execute("DELETE FROM favorites WHERE owner = ?", (ssid,))
            conn.execute("DELETE FROM login_codes WHERE ss_id = ?", (ssid,))
        codes = conn.execute("DELETE FROM login_codes WHERE expires_at < ?", (now,)).rowcount
        peers = conn.execute("DELETE FROM peers WHERE source != 'config' AND last_seen < ?",
                             (now - PEER_FORGET_AFTER,)).rowcount
        conn.commit()
    finally:
        conn.close()
    return {"ssids": len(dead), "login_codes": codes, "peers": peers}


def store_message(msg_id: str, from_ss: str, to_ss: str, text: str,
                  created_at: str, delivered: bool = False) -> None:
    """Persist a message, ignoring duplicates by msg_id (store-and-forward)."""
    conn = get_db()
    try:
        if msg_id:
            existing = conn.execute(
                "SELECT 1 FROM messages WHERE msg_id = ?", (msg_id,)
            ).fetchone()
            if existing:
                return
        conn.execute(
            "INSERT INTO messages (msg_id, from_ss, to_ss, text, created_at, delivered) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, from_ss, to_ss, text, created_at, 1 if delivered else 0),
        )
        conn.commit()
    finally:
        conn.close()


def mark_delivered(msg_id: str, to_ss: str) -> None:
    if not msg_id:
        return
    conn = get_db()
    try:
        conn.execute(
            "UPDATE messages SET delivered = 1 WHERE msg_id = ? AND to_ss = ?",
            (msg_id, to_ss),
        )
        conn.commit()
    finally:
        conn.close()


def pending_messages_for(ss_id: str) -> list:
    """Undelivered messages addressed to ss_id (store-and-forward on reconnect)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT msg_id, from_ss, to_ss, text, created_at FROM messages "
            "WHERE to_ss = ? AND delivered = 0 ORDER BY id ASC",
            (ss_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]

def generate_ssid() -> str:
    while True:
        # 4 bytes = 32 bits of entropy. token_hex(2) gave only 16 bits
        # (65k space), brute-forceable in seconds.
        suffix = secrets.token_hex(4)
        ssid = f"ss-{suffix}-pager"
        conn = get_db()
        row = conn.execute("SELECT 1 FROM ssids WHERE ssid = ?", (ssid,)).fetchone()
        conn.close()
        if row is None:
            return ssid


# Bound message size so a single peer/client can't flood the mesh or DB.
MAX_TEXT_LEN = 8192
MAX_SSID_LEN = 64


# --- Rate limiting (in-memory sliding window per key) ---
class RateLimiter:
    """Tiny sliding-window limiter. Keys are caller-defined (ip:endpoint)."""

    def __init__(self):
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str, limit: int, window_s: float) -> bool:
        now = time.time()
        hits = self._hits.setdefault(key, [])
        # Drop entries outside the window.
        while hits and hits[0] < now - window_s:
            hits.pop(0)
        if len(hits) >= limit:
            return False
        hits.append(now)
        # Bound memory across many keys.
        if len(self._hits) > 10000:
            self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - window_s}
        return True


rate_limiter = RateLimiter()


def client_key(request, bucket: str) -> str:
    host = request.client.host if request and request.client else "unknown"
    return f"{host}:{bucket}"

# --- App Lifecycle ---
async def presence_loop():
    """Every 10 s: reap dead sessions, broadcast status changes."""
    while True:
        await asyncio.sleep(10)
        try:
            await ws_mgr.reap()
            await ws_mgr.broadcast_changes()
        except Exception as e:
            print(f"[presence] loop error: {e}")


async def housekeeping_loop():
    """Hourly: drop unused anonymous SSIDs, expired codes, forgotten peers;
    on the master, re-sync the athlete directory from NeZhri."""
    while True:
        try:
            if IS_MASTER:
                athletes = await asyncio.to_thread(fetch_athletes_from_mongo)
                upsert_athletes(athletes)
            removed = cleanup_dead()
            if any(removed.values()):
                print(f"[housekeeping] removed {removed}")
        except Exception as e:
            print(f"[housekeeping] error: {e}")
        await asyncio.sleep(3600 if not IS_MASTER else 600)


async def register_with_master():
    """Non-master nodes announce themselves to the master (and keep doing it:
    the master's peer list used to forget us on every restart)."""
    while MAIN_NODE_URL:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(f"{MAIN_NODE_URL}/mesh/register",
                                  json={"node_id": NODE_ID, "url": NODE_URL})
        except Exception as e:
            print(f"⚠️ Could not register to main node: {e}")
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    conn = get_db()
    conn.execute("DELETE FROM peers WHERE source = 'config'")  # env is the truth for config peers
    conn.commit()
    conn.close()
    for url in MESH_PEERS:
        if isinstance(url, str):
            save_peer("", url, "config")
    tasks = [asyncio.create_task(t()) for t in
             (mesh_ping_loop, presence_loop, housekeeping_loop, register_with_master)]
    yield
    await ws_mgr.kill_all()
    for t in tasks:
        t.cancel()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Mount uploads directory as static files
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

# --- Routes ---

@app.get("/")
def serve_index():
    return FileResponse(BASE_DIR / "index.html")

@app.get("/api/athletes")
def api_athletes():
    """Flat contact list: local (incl. athletes synced on the master) + mesh.

    Kept for the Android app and older clients; the web client uses /phonebook.
    """
    seen = set()
    result = []
    for c in list(local_contacts()) + list(mesh.dir.remote_contacts().values()):
        if c.ss_id not in seen:
            seen.add(c.ss_id)
            result.append(c.public())
    return result

@app.get("/contacts")
def get_contacts():
    """All contacts: local + mesh (same as /api/athletes)."""
    return api_athletes()


def favorites_of(owner: str) -> dict[str, str]:
    conn = get_db()
    rows = conn.execute("SELECT contact, display_name FROM favorites WHERE owner = ? ORDER BY added_at",
                        (owner,)).fetchall()
    conn.close()
    return {r["contact"]: r["display_name"] for r in rows}


@app.get("/phonebook")
def phonebook(ss_id: str = Query(default="")):
    """Hierarchical phone book: Избранное / Ближайшие ноды / Удалённые контакты."""
    favs = favorites_of(ss_id) if valid_ssid(ss_id) else {}
    book = mesh.dir.phonebook(ss_id, local_contacts(), favs)
    book["is_master"] = IS_MASTER
    return book


@app.get("/favorites/{owner}")
def list_favorites(owner: str):
    return [{"ss_id": k, "display_name": v} for k, v in favorites_of(owner).items()]


@app.post("/favorites/{owner}")
def add_favorite(owner: str, req: FavoriteReq):
    if not (valid_ssid(owner) and valid_ssid(req.contact)) or owner == req.contact:
        return JSONResponse({"ok": False, "error": "invalid ssid"}, status_code=400)
    name = (req.display_name or label_for_ssid(req.contact) or req.contact)[:64]
    conn = get_db()
    try:
        if conn.execute("SELECT COUNT(*) FROM favorites WHERE owner = ?", (owner,)).fetchone()[0] >= 200:
            return JSONResponse({"ok": False, "error": "too many favorites"}, status_code=400)
        conn.execute(
            "INSERT INTO favorites (owner, contact, display_name, added_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(owner, contact) DO UPDATE SET display_name = excluded.display_name",
            (owner, req.contact, name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/favorites/{owner}/{contact}")
def remove_favorite(owner: str, contact: str):
    conn = get_db()
    conn.execute("DELETE FROM favorites WHERE owner = ? AND contact = ?", (owner, contact))
    conn.commit()
    conn.close()
    return {"ok": True}


class PubkeyReq(BaseModel):
    ss_id: str
    pubkey: str  # JWK JSON (EC P-256 public key)


@app.post("/keys")
async def publish_key(req: PubkeyReq):
    """Publish a client's public key for E2E encryption.

    Anyone can overwrite any key (the system has no auth by design), so E2E here
    protects against passive reading of the DB/wire, not active impersonation —
    documented limitation until envelopes are signed.
    """
    if not req.ss_id or len(req.ss_id) > MAX_SSID_LEN or len(req.pubkey) > 2048:
        return {"ok": False, "error": "invalid key"}
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO pubkeys (ss_id, pubkey, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(ss_id) DO UPDATE SET pubkey = excluded.pubkey, updated_at = excluded.updated_at",
        (req.ss_id, req.pubkey, now),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/keys/{ss_id}")
def get_key(ss_id: str):
    conn = get_db()
    row = conn.execute("SELECT pubkey FROM pubkeys WHERE ss_id = ?", (ss_id,)).fetchone()
    conn.close()
    if row is None:
        return {"ok": False, "pubkey": None}
    return {"ok": True, "pubkey": row["pubkey"]}


def _trusted(request: Optional[Request]) -> bool:
    """NeZhri (or another trusted service) calling with the directory key."""
    if request is None or not DIRECTORY_API_KEY:
        return False
    return secrets.compare_digest(request.headers.get("X-API-Key", ""), DIRECTORY_API_KEY)


@app.post("/register")
async def register_pager(req: RegisterReq, request: Request = None):
    trusted = _trusted(request)
    if request is not None and not trusted and \
            not rate_limiter.allow(client_key(request, "register"), limit=10, window_s=60):
        return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)
    label = (req.display_name or req.label or req.name or "")[:64]
    now = datetime.now(timezone.utc).isoformat()
    athlete_id = req.athlete_id.strip()[:64] if trusted else ""
    kind = "athlete" if trusted and (req.kind == "athlete" or athlete_id) else "user"
    conn = get_db()
    try:
        if athlete_id:
            # Idempotent for athletes: one athlete, one SSID on the master.
            row = conn.execute("SELECT ssid, label, created_at FROM ssids WHERE athlete_id = ?",
                               (athlete_id,)).fetchone()
            if row:
                return {"ssid": row["ssid"], "ss_id": row["ssid"], "display_name": row["label"],
                        "created_at": row["created_at"], "kind": "athlete"}
        ssid = generate_ssid()
        conn.execute(
            "INSERT INTO ssids (ssid, label, created_at, kind, athlete_id, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ssid, label, now, kind, athlete_id, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ssid": ssid, "ss_id": ssid, "display_name": label, "created_at": now, "kind": kind}

def label_for_ssid(ss_id: str) -> str:
    """Human-readable name for an SSID (used when forwarding to Telegram)."""
    if not ss_id:
        return ""
    try:
        conn = get_db()
        row = conn.execute("SELECT label FROM ssids WHERE ssid = ?", (ss_id,)).fetchone()
        conn.close()
        if row and row["label"]:
            return row["label"]
    except Exception:
        pass
    c = mesh.dir.route(ss_id)
    return c.display_name if c and c.display_name != ss_id else ""


def create_login_code(ss_id: str, peer: str = "") -> str:
    """A code that starts a new pager session as `ss_id` with the chat to `peer` open."""
    code = secrets.token_urlsafe(18)
    now = time.time()
    conn = get_db()
    conn.execute("INSERT INTO login_codes (code, ss_id, peer, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                 (code, ss_id, peer, now, now + LOGIN_CODE_TTL))
    conn.commit()
    conn.close()
    return code


def continue_url(code: str, peer: str) -> str:
    from urllib.parse import urlencode
    return f"{PUBLIC_URL}/?{urlencode({'login': code, 'to': peer})}"


def looks_encrypted(text: str) -> bool:
    """E2E envelope from the web/Android client: {"enc":1,"n":...,"c":...}."""
    if not text.startswith("{") or '"enc"' not in text:
        return False
    try:
        return bool(json.loads(text).get("enc"))
    except (ValueError, AttributeError):
        return False


_last_notified: dict[tuple, float] = {}


async def notify_nezhri_telegram(from_ss: str, to_ss: str, text: str, *, msg_id: str = "") -> None:
    """Tell the recipient in Telegram (via NeZhri) that the pager has a message.

    Sent when they have no live pager session. Carries a «продолжить диалог»
    link that starts a new session as them with this chat open. One nudge per
    sender→recipient pair per NOTIFY_COOLDOWN, so a burst of messages is one
    Telegram message. Best-effort: failures never affect pager delivery.
    """
    if not (NEZHRI_NOTIFY_URL and NEZHRI_NOTIFY_API_KEY):
        return
    now = time.time()
    if now - _last_notified.get((from_ss, to_ss), 0) < NOTIFY_COOLDOWN:
        return
    _last_notified[(from_ss, to_ss)] = now
    if len(_last_notified) > 5000:
        for k in [k for k, t in _last_notified.items() if now - t > NOTIFY_COOLDOWN]:
            _last_notified.pop(k, None)
    encrypted = looks_encrypted(text)
    payload = {
        "to_ssid": to_ss,
        "from_ssid": from_ss,
        "from_name": label_for_ssid(from_ss),
        # An E2E envelope means nothing in Telegram — say it's sealed instead.
        "text": "" if encrypted else text,
        "encrypted": encrypted,
        "msg_id": msg_id,
        "continue_url": continue_url(create_login_code(to_ss, from_ss), from_ss),
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                NEZHRI_NOTIFY_URL,
                json=payload,
                headers={"X-API-Key": NEZHRI_NOTIFY_API_KEY},
            )
    except Exception as e:
        print(f"[nezhri] notify failed for {to_ss}: {e}")


@app.post("/session/claim")
def claim_session(req: ClaimReq, request: Request = None):
    """Resolve a «продолжить диалог» code from Telegram.

    The web client then offers to start a new session as that SSID (the code
    stays valid until it expires, so the link works on a second device too).
    """
    if request is not None and not rate_limiter.allow(client_key(request, "claim"), limit=20, window_s=60):
        return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)
    conn = get_db()
    row = conn.execute("SELECT ss_id, peer, expires_at FROM login_codes WHERE code = ?",
                       (req.code[:64],)).fetchone()
    conn.close()
    if row is None or row["expires_at"] < time.time():
        return JSONResponse({"ok": False, "error": "link expired"}, status_code=404)
    touch_ssid(row["ss_id"])
    return {
        "ok": True,
        "ss_id": row["ss_id"],
        "display_name": label_for_ssid(row["ss_id"]),
        "to": row["peer"],
        "to_name": label_for_ssid(row["peer"]),
    }


@app.post("/message")
async def send_message(req: MessageReq, request: Request = None):
    if request is not None and not rate_limiter.allow(client_key(request, "message"), limit=60, window_s=60):
        return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)
    # Input validation: bound sizes to protect the DB and mesh.
    if not req.text or len(req.text) > MAX_TEXT_LEN:
        return {"ok": False, "error": "text empty or too long"}
    if not req.target or len(req.target) > MAX_SSID_LEN:
        return {"ok": False, "error": "invalid target"}

    msg = MeshMessage.create(req.from_ss, req.target, req.text)
    if re.fullmatch(r"[0-9a-f]{32}", req.msg_id or ""):
        msg.msg_id = req.msg_id
    now = datetime.now(timezone.utc).isoformat()

    # The sender's other tabs/devices on this node see what was sent — one
    # session shows the whole conversation, whichever tab wrote it.
    if req.from_ss and req.from_ss != req.target:
        await ws_mgr.send_to(req.from_ss, {
            "type": "sent", "text": req.text, "from_ss": req.from_ss, "to_ss": req.target,
            "created_at": now, "msg_id": msg.msg_id,
        })

    # Persist immediately (store-and-forward); delivery status updated below.
    store_message(msg.msg_id, req.from_ss, req.target, req.text, now, delivered=False)

    # The router delivers locally (and nudges an absent recipient in Telegram
    # via NeZhri) or forwards across the mesh.
    result = await route_mesh_message(msg)
    if result["delivered_local"]:
        return {"ok": True, "route": "local", "msg_id": msg.msg_id}
    if result["forwarded"]:
        return {"ok": True, "route": "mesh", "msg_id": msg.msg_id, "peers": result["forwarded"]}
    return {"ok": True, "route": "stored", "msg_id": msg.msg_id,
            "note": "Target not reachable yet, message saved for forwarding"}

@app.get("/messages/{ss_id}")
def get_messages(ss_id: str, limit: int = Query(default=100), peer: str = Query(default="", alias="with")):
    """Messages of an SSID (sent or received), newest first; `with` narrows to one chat."""
    limit = max(1, min(limit, 500))
    conn = get_db()
    if peer:
        rows = conn.execute(
            "SELECT msg_id, from_ss, to_ss, text, created_at FROM messages "
            "WHERE (from_ss = ? AND to_ss = ?) OR (from_ss = ? AND to_ss = ?) ORDER BY id DESC LIMIT ?",
            (ss_id, peer, peer, ss_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT msg_id, from_ss, to_ss, text, created_at FROM messages "
            "WHERE from_ss = ? OR to_ss = ? ORDER BY id DESC LIMIT ?",
            (ss_id, ss_id, limit)
        ).fetchall()
    conn.close()
    return [{"msg_id": r["msg_id"], "from_ss": r["from_ss"], "to_ss": r["to_ss"],
             "text": r["text"], "created_at": r["created_at"]} for r in rows]

@app.websocket("/ws/{ss_id}")
async def ws_endpoint(ws: WebSocket, ss_id: str):
    await ws.accept()
    session = ws_mgr.presence.open(ss_id, ws, client=ws.query_params.get("client", ""))
    touch_ssid(ss_id)
    # The newcomer knows it is online; it asks for the rest with get_presence.
    await ws_mgr.broadcast_changes(exclude=session.sid)
    # Store-and-forward: flush any messages that arrived while this SSID was
    # offline, then mark them delivered.
    try:
        pending = pending_messages_for(ss_id)
        for m in pending:
            await ws.send_json({
                "text": m["text"], "from_ss": m["from_ss"], "to_ss": m["to_ss"],
                "created_at": m["created_at"], "msg_id": m["msg_id"],
                "pending": True,
            })
            mark_delivered(m["msg_id"], ss_id)
    except Exception:
        pass
    abnormal = True
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                msg_type = msg.get("type", "")
                # Any frame proves the session alive; "ping" also opts the
                # client into heartbeat tracking (silent → zombie → reaped).
                ws_mgr.presence.touch(session.sid, heartbeat=msg_type == "ping")

                if msg_type == "ping":
                    await ws.send_json({"type": "pong", "ts": msg.get("ts")})

                elif msg_type == "bye":
                    # Clean logout: offline at once, no zombie grace.
                    abnormal = False
                    break

                elif msg_type == "get_presence":
                    # Client requesting current presence data
                    await ws.send_json({
                        "type": "presence",
                        "contacts": [{"ss_id": k, "status": v}
                                     for k, v in ws_mgr.presence.snapshot().items()],
                    })

                elif msg_type == "call_signal":
                    # WebRTC signaling relay: offer/answer/ICE/end between two
                    # SSIDs connected to this node. Media then flows P2P.
                    target = msg.get("target", "")
                    payload = msg.get("payload", {})
                    if target and ws_mgr.presence.is_connected(target):
                        await ws_mgr.send_to(target, {
                            "type": "call_signal",
                            "from_ss": ss_id,
                            "payload": payload,
                        })
                    else:
                        # Tell the caller the target can't take calls right now.
                        await ws.send_json({
                            "type": "call_signal",
                            "from_ss": target,
                            "payload": {"kind": "unavailable"},
                        })

                elif "target" in msg and "text" in msg:
                    # Forward message
                    await send_message(MessageReq(text=msg["text"], target=msg["target"], from_ss=ss_id,
                                                  msg_id=str(msg.get("msg_id", ""))))

            except json.JSONDecodeError:
                ws_mgr.presence.touch(session.sid)
    except WebSocketDisconnect as e:
        # 1000/1001: the client closed on purpose (tab closed, logout) → offline.
        # Anything else (1006: network gone) → zombie for a grace period.
        abnormal = e.code not in (1000, 1001)
    except Exception:
        pass
    finally:
        ws_mgr.presence.close(session.sid, abnormal=abnormal)
        touch_ssid(ss_id)
        if not abnormal:
            try:
                await ws.close(code=1000)
            except Exception:
                pass
        try:
            await ws_mgr.broadcast_changes()
        except Exception:
            pass

# --- Mesh Endpoints ---

@app.post("/mesh/hello")
async def mesh_hello(req: MeshHelloReq):
    """Receive a neighbour's hello: its contacts (with hops/status) and its own
    neighbours. Reply with ours (split horizon: not what we learned from it).

    The caller advertises its own reachable URL via `self_url`; we remember it
    so we can say hello back and route return traffic.
    """
    peer_url = req.self_url.rstrip("/") if req.self_url.startswith("http") else f"peer-{req.node_id}"
    mesh.update_peer(req.node_id, peer_url, req.contacts, req.neighbors)
    return hello_payload(req.node_id)

@app.post("/mesh/register")
async def mesh_register(req: dict):
    """A node announces itself (auto-discovery). Persisted, deduplicated by node."""
    peer_url = str(req.get("url", "")).rstrip("/")
    node_id = str(req.get("node_id", ""))
    if not peer_url.startswith("http") or len(peer_url) > 256:
        return {"ok": False, "reason": "invalid url"}
    save_peer(node_id, peer_url, "register")
    return {"ok": True, "peers": peer_urls()}

@app.get("/mesh/route/{ssid}")
def mesh_route(ssid: str):
    """Is this SSID reachable right now, and where? (NeZhri's online dot.)"""
    locally = ssid in local_ssids()
    if locally or ws_mgr.presence.is_connected(ssid):
        status = ws_mgr.get_status(ssid)
        return {"ssid": ssid, "node_id": NODE_ID, "node_url": NODE_URL, "hops": 0,
                "status": status, "online": status == dirmod.STATUS_ONLINE,
                "locally_registered": locally}
    c = mesh.dir.route(ssid)
    if c is None:
        return {"ssid": ssid, "node_id": None, "node_url": None, "hops": None,
                "status": dirmod.STATUS_OFFLINE, "online": False, "locally_registered": False}
    return {"ssid": ssid, "node_id": c.node_id, "node_url": mesh.url_for(c.via), "hops": c.hops,
            "status": c.status, "online": c.status == dirmod.STATUS_ONLINE,
            "locally_registered": False}

@app.post("/mesh/ingest")
async def mesh_ingest(envelope: dict):
    """Primary mesh entry point: accept a MeshMessage envelope from ANY transport.

    Internet peers, the Wi-Fi/NSD bridge, and the BLE bridge all POST here. The
    router decides whether to deliver locally and/or forward onward, with dedup
    and TTL handled centrally so the same message can arrive over several
    transports without looping or duplicating.
    """
    msg = MeshMessage.from_dict(envelope)
    if not msg.target_ss or len(msg.text) > MAX_TEXT_LEN:
        return {"ok": False, "error": "invalid envelope"}
    # Persist for store-and-forward before routing.
    store_message(msg.msg_id, msg.from_ss, msg.target_ss, msg.text,
                  datetime.now(timezone.utc).isoformat(), delivered=False)
    result = await route_mesh_message(msg)
    return {"ok": True, **result}


@app.post("/mesh/deliver")
async def mesh_deliver(req: MeshMessageReq):
    """Legacy single-hop delivery endpoint — kept for older nodes/clients.

    Wraps the payload into an envelope and routes it through the same path as
    /mesh/ingest so behaviour stays consistent.
    """
    msg = MeshMessage.create(req.from_ss, req.target_ss, req.text)
    if req.origin_node:
        msg.path = [req.origin_node]
    store_message(msg.msg_id, req.from_ss, req.target_ss, req.text,
                  datetime.now(timezone.utc).isoformat(), delivered=False)
    result = await route_mesh_message(msg)
    return {"ok": True, "delivered": result["delivered_local"], **result}

@app.get("/mesh/status")
def mesh_status():
    """Current mesh network status."""
    status = mesh.get_status()
    status["peers_list"] = peer_urls()
    return status

@app.get("/health")
def health():
    return {"status": "ok", "node_id": NODE_ID, "peers": len(mesh.peers)}

@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = FastAPIFile(...)):
    """Upload a file and return its URL."""
    if not rate_limiter.allow(client_key(request, "upload"), limit=20, window_s=60):
        return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)
    # Sanitize filename: keep extension, add uuid prefix
    original_name = file.filename or "unnamed"
    safe_name = re.sub(r'[^\w\.\-]', '_', original_name)
    unique_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
    file_path = UPLOADS_DIR / unique_name
    content = await file.read()
    file_path.write_bytes(content)
    return {"url": f"/uploads/{unique_name}", "filename": original_name}

@app.get("/download_node_kit")
def download_node_kit():
    """Generate and stream a .tar.gz archive for deploying a new Pager node."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        # 1. server.py - read full content
        server_content = (BASE_DIR / "server.py").read_bytes()
        info = tarfile.TarInfo(name="server.py")
        info.size = len(server_content)
        tar.addfile(info, io.BytesIO(server_content))
        
        # 2. index.html - read full content
        html_content = (BASE_DIR / "index.html").read_bytes()
        info = tarfile.TarInfo(name="index.html")
        info.size = len(html_content)
        tar.addfile(info, io.BytesIO(html_content))

        # 2b. mesh_router.py — server.py imports this; a node without it crashes.
        router_content = (BASE_DIR / "mesh_router.py").read_bytes()
        info = tarfile.TarInfo(name="mesh_router.py")
        info.size = len(router_content)
        tar.addfile(info, io.BytesIO(router_content))

        # 2c. directory.py — phone book + presence, also imported by server.py.
        dir_content = (BASE_DIR / "directory.py").read_bytes()
        info = tarfile.TarInfo(name="directory.py")
        info.size = len(dir_content)
        tar.addfile(info, io.BytesIO(dir_content))

        # 3. requirements.txt
        req_content = b"fastapi\nuvicorn[standard]\npymongo\npython-multipart\nhttpx\n"
        info = tarfile.TarInfo(name="requirements.txt")
        info.size = len(req_content)
        tar.addfile(info, io.BytesIO(req_content))
        
        # 4. deploy.sh
        deploy_content = b"""#!/bin/bash
# Deploy script for Pager Node
# Usage: ./deploy.sh [MAIN_NODE_URL]
# Example: ./deploy.sh https://telegram.iron-siber.ru

set -e

MAIN_NODE="${1:-}"
NODE_URL="${NODE_URL:-http://localhost:8000}"

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Auto-register to main node if provided
if [ -n "$MAIN_NODE" ]; then
    export MAIN_NODE_URL="$MAIN_NODE"
    export NODE_URL="$NODE_URL"
    echo "Will register to main node: $MAIN_NODE"
fi

echo "Starting Pager Node..."
uvicorn server:app --host 0.0.0.0 --port 8000
"""
        info = tarfile.TarInfo(name="deploy.sh")
        info.size = len(deploy_content)
        tar.addfile(info, io.BytesIO(deploy_content))
        # Make executable
        import os
        os.chmod("/tmp/deploy.sh", 0o755) if False else None
        
        # 5. README_NODE.txt
        readme_content = """# SAFARANCHO PAGER NODE KIT
========================

## WARNING
This is decentralized mesh network messaging software.
Use at your own risk. Author is not responsible.

## QUICK START
1. chmod +x deploy.sh
2. ./deploy.sh
3. Open http://localhost:8000 in browser

## ENVIRONMENT VARIABLES
NODE_ID=your-unique-node-id
MESH_PEERS='["http://ip1:8000", "http://ip2:8000"]'

## MESH NETWORK
Nodes ping each other every 30 seconds and exchange contacts.
""".encode('utf-8')
        info = tarfile.TarInfo(name="README_NODE.txt")
        info.size = len(readme_content)
        tar.addfile(info, io.BytesIO(readme_content))
    buffer.seek(0)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        buffer,
        media_type="application/gzip",
        headers={"Content-Disposition": "attachment; filename=pager_node_kit.tar.gz"}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
