#!/usr/bin/env python3
import os
import sqlite3
import secrets
import asyncio
import tarfile
import io
import uuid
import json
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
MAIN_NODE_URL = os.getenv("MAIN_NODE_URL", "")  # URL главной ноды для авто-регистрации
NODE_ID = os.getenv("NODE_ID", f"node-{secrets.token_hex(3)}")
# This node's externally-reachable URL, advertised to peers so they can route
# back to us. Falls back to localhost for single-node dev.
NODE_URL = os.getenv("NODE_URL", f"http://localhost:{PORT}")
MESH_PING_INTERVAL = 30  # seconds

# --- NeZhri Telegram bridge ---
# When a "написать спортсмену" message is sent from the Iron Siber leaderboard,
# we also forward it to the recipient's Telegram via the NeZhri bot so they get
# it even if they're not on the pager mesh right now. Best-effort, fire-and-forget.
NEZHRI_NOTIFY_URL = os.getenv("NEZHRI_NOTIFY_URL", "")  # e.g. https://safargaleev.com/api/pager/notify
NEZHRI_NOTIFY_API_KEY = os.getenv("NEZHRI_NOTIFY_API_KEY", "")

# --- DB ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# --- Mongo Integration ---
def get_athletes():
    """Get athletes from MongoDB who have pager_ssid or are monitored."""
    if pymongo is None:
        return []
    try:
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        db = client.get_database()
        # Users with pager_ssid
        users = list(db.users.find(
            {"pager_ssid": {"$exists": True}},
            {"_id": 0, "username": 1, "first_name": 1, "pager_ssid": 1, "display_name": 1}
        ).limit(50))
        if not users:
            users = list(db.monitored_athletes.find(
                {},
                {"_id": 0, "name": 1, "pager_ssid": 1}
            ).limit(50))
        # Normalize: ensure each has ss_id and display_name
        result = []
        for u in users:
            result.append({
                "ss_id": u.get("pager_ssid", u.get("ssid", "")),
                "display_name": u.get("display_name") or u.get("first_name") or u.get("username") or u.get("name", "ATHLETE"),
            })
        return [r for r in result if r["ss_id"]]  # filter empty ssids
    except Exception as e:
        print(f"Mongo error: {e}")
        return []

# --- WebSocket manager with presence ---
class WSManager:
    def __init__(self):
        self.connections: dict[str, set[WebSocket]] = {}
        self.last_ping: dict[str, float] = {}  # ss_id -> timestamp of last ping
    async def connect(self, ss_id: str, ws: WebSocket):
        await ws.accept()
        self.connections.setdefault(ss_id, set()).add(ws)
        self.last_ping[ss_id] = time.time()
    def disconnect(self, ss_id: str, ws: WebSocket):
        if ss_id in self.connections:
            self.connections[ss_id].discard(ws)
            if not self.connections[ss_id]:
                del self.connections[ss_id]
                # Remove last_ping so zombie detection doesn't keep dead sessions
                self.last_ping.pop(ss_id, None)
    def update_ping(self, ss_id: str):
        self.last_ping[ss_id] = time.time()
    def get_status(self, ss_id: str) -> str:
        """online (<30s), zombie (30s-5min), offline (>5min), killed (no data)."""
        ts = self.last_ping.get(ss_id)
        if ts is None:
            return 'offline'
        elapsed = time.time() - ts
        if elapsed < 30:
            return 'online'
        elif elapsed < 300:  # 5 minutes
            return 'zombie'
        else:
            return 'killed'
    def get_all_presence(self) -> list:
        """Return presence for all known SSIDs."""
        all_ssids = set(self.connections.keys()) | set(self.last_ping.keys())
        return [{"ss_id": sid, "status": self.get_status(sid)} for sid in all_ssids]
    async def send_to(self, ss_id: str, data: dict):
        for ws in list(self.connections.get(ss_id, [])):
            try:
                await ws.send_json(data)
            except:
                self.disconnect(ss_id, ws)
    async def broadcast(self, data: dict):
        """Send to all connected clients."""
        for ss_id, wss in list(self.connections.items()):
            for ws in list(wss):
                try:
                    await ws.send_json(data)
                except:
                    self.disconnect(ss_id, ws)

    async def kill_all(self):
        """Close all active WebSocket connections."""
        for ss_id, wss in list(self.connections.items()):
            for ws in list(wss):
                try:
                    await ws.close(code=1001, reason="Server shutdown")
                except:
                    pass
            self.connections.pop(ss_id, None)
            self.last_ping.pop(ss_id, None)

ws_mgr = WSManager()

# --- Mesh Network State ---
class MeshNetwork:
    """Tracks neighbor nodes, their contacts, and routes messages through available peers."""
    def __init__(self):
        self.peers: dict[str, dict] = {}  # node_id -> {url, last_seen, contacts, status}
        self.peer_contacts: dict[str, list] = {}  # node_id -> list of {ss_id, display_name}
        self.route_table: dict[str, str] = {}  # ss_id -> node_id (which node knows this contact)

    def update_peer(self, node_id: str, url: str, contacts: list):
        self.peers[node_id] = {
            "url": url,
            "last_seen": time.time(),
            "status": "online"
        }
        self.peer_contacts[node_id] = contacts
        # Update route table + router topology
        for c in contacts:
            ssid = c.get("ss_id", "")
            if ssid:
                self.route_table[ssid] = node_id
                router.learn_route(ssid, node_id)

    def available_peer_ids(self) -> list:
        """node_ids of peers seen recently enough to be considered reachable."""
        now = time.time()
        return [
            nid for nid, p in self.peers.items()
            if now - p["last_seen"] < MESH_PING_INTERVAL * 3
        ]

    def url_for(self, node_id: str) -> Optional[str]:
        peer = self.peers.get(node_id)
        if peer and peer.get("url", "").startswith("http"):
            return peer["url"]
        return None

    def remove_peer(self, node_id: str):
        self.peers.pop(node_id, None)
        self.peer_contacts.pop(node_id, None)
        # Clean route table
        to_remove = [ssid for ssid, nid in self.route_table.items() if nid == node_id]
        for ssid in to_remove:
            del self.route_table[ssid]
        router.forget_node(node_id)

    def get_all_contacts(self) -> list:
        """Merge local + all peer contacts, deduplicated by ss_id."""
        seen = set()
        result = []
        # Local contacts from SQLite
        conn = get_db()
        rows = conn.execute("SELECT ssid, label FROM ssids").fetchall()
        conn.close()
        for r in rows:
            if r["ssid"] not in seen:
                seen.add(r["ssid"])
                result.append({"ss_id": r["ssid"], "display_name": r["label"] or r["ssid"]})
        # Peer contacts
        for node_id, contacts in self.peer_contacts.items():
            for c in contacts:
                ssid = c.get("ss_id", "")
                if ssid and ssid not in seen:
                    seen.add(ssid)
                    result.append(c)
        return result

    def find_route(self, target_ss: str) -> Optional[str]:
        """Find which peer node can deliver to target_ss. Returns base URL or None."""
        node_id = self.route_table.get(target_ss)
        if node_id and node_id in self.peers:
            peer = self.peers[node_id]
            if time.time() - peer["last_seen"] < MESH_PING_INTERVAL * 3:
                return peer["url"]
        return None

    def get_status(self) -> dict:
        online = sum(1 for p in self.peers.values() if time.time() - p["last_seen"] < MESH_PING_INTERVAL * 3)
        return {
            "node_id": NODE_ID,
            "total_peers": len(self.peers),
            "online_peers": online,
            "known_routes": len(self.route_table),
            "peers": {nid: {"url": p["url"], "last_seen": p["last_seen"], "status": p["status"]}
                      for nid, p in self.peers.items()}
        }

# Router holds the pure routing logic; mesh holds peer transport state.
router = MeshRouter(NODE_ID)
mesh = MeshNetwork()


def sync_router_local_ssids() -> None:
    """Keep the router's view of locally-registered contacts current."""
    router.set_local_ssids(s["ss_id"] for s in get_local_contacts_for_mesh())


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


async def route_mesh_message(msg: MeshMessage) -> dict:
    """Run a message through the router and act on the decision."""
    sync_router_local_ssids()
    decision = router.handle(msg, available_peers=mesh.available_peer_ids())
    result = {"msg_id": msg.msg_id, "reason": decision.reason,
              "delivered_local": False, "forwarded": []}

    if decision.deliver_locally:
        # Only mark delivered if a live socket actually receives it; otherwise
        # leave it pending so store-and-forward pushes it on reconnect.
        if ws_mgr.connections.get(msg.target_ss):
            await ws_mgr.send_to(msg.target_ss, {
                "text": msg.text,
                "from_ss": msg.from_ss,
                "to_ss": msg.target_ss,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "msg_id": msg.msg_id,
            })
            mark_delivered(msg.msg_id, msg.target_ss)
            result["delivered_local"] = True
        else:
            # Target is a known local contact but offline — persist for later.
            store_message(msg.msg_id, msg.from_ss, msg.target_ss, msg.text,
                          datetime.now(timezone.utc).isoformat(), delivered=False)

    if decision.forward_to:
        msg.ttl -= 1
        result["forwarded"] = await forward_to_peers(msg, decision.forward_to)

    return result

# --- Mesh Background Tasks ---
async def mesh_ping_loop():
    """Periodically ping all configured peers and exchange contact lists."""
    while True:
        my_contacts = get_local_contacts_for_mesh()
        for peer_url in MESH_PEERS:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    # Ping + send our contacts
                    resp = await client.post(f"{peer_url}/mesh/hello", json={
                        "node_id": NODE_ID,
                        "contacts": my_contacts,
                        "timestamp": time.time(),
                        "self_url": NODE_URL,
                    })
                    if resp.status_code == 200:
                        data = resp.json()
                        mesh.update_peer(
                            data["node_id"],
                            peer_url,
                            data.get("contacts", [])
                        )
            except Exception as e:
                # Mark peer as potentially offline
                pass
        # Clean stale peers
        for nid in list(mesh.peers.keys()):
            if time.time() - mesh.peers[nid]["last_seen"] > MESH_PING_INTERVAL * 5:
                mesh.remove_peer(nid)
        await asyncio.sleep(MESH_PING_INTERVAL)

def get_local_contacts_for_mesh() -> list:
    """Get all locally registered SSIDs for sharing with peers."""
    conn = get_db()
    rows = conn.execute("SELECT ssid, label FROM ssids").fetchall()
    conn.close()
    return [{"ss_id": r["ssid"], "display_name": r["label"] or r["ssid"]} for r in rows]

# --- Pydantic Models ---
class MessageReq(BaseModel):
    text: str
    target: str
    from_ss: str = ""

class RegisterReq(BaseModel):
    label: str = ""
    name: str = ""
    display_name: str = ""

class MeshHelloReq(BaseModel):
    node_id: str
    contacts: list
    timestamp: float
    self_url: str = ""  # sender's reachable URL, so we can route back to it

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
    conn.commit()
    conn.close()


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
async def start_mesh_tasks(app):
    app.state.mesh_task = asyncio.create_task(mesh_ping_loop())

async def presence_cleanup_loop():
    """Periodically clean stale presence entries and broadcast updates."""
    while True:
        await asyncio.sleep(60)
        # Remove entries older than 5 minutes (zombie threshold)
        cutoff = time.time() - 300
        to_remove = [sid for sid, ts in ws_mgr.last_ping.items() if ts < cutoff]
        for sid in to_remove:
            if sid not in ws_mgr.connections:
                del ws_mgr.last_ping[sid]
        if to_remove:
            try:
                await ws_mgr.broadcast({
                    "type": "presence",
                    "contacts": ws_mgr.get_all_presence()
                })
            except:
                pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    mesh_task = asyncio.create_task(mesh_ping_loop())
    presence_task = asyncio.create_task(presence_cleanup_loop())
    
    # Auto-register to main node if configured
    if MAIN_NODE_URL:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Get this node's external URL from environment or detect
                my_url = os.getenv("NODE_URL", f"http://localhost:{PORT}")
                await client.post(f"{MAIN_NODE_URL}/mesh/register", json={
                    "node_id": NODE_ID,
                    "url": my_url
                })
                print(f"✅ Registered to main node: {MAIN_NODE_URL}")
        except Exception as e:
            print(f"⚠️ Could not register to main node: {e}")
    
    yield
    # Kill all active WebSocket sessions so they reconnect and get re-counted
    await ws_mgr.kill_all()
    mesh_task.cancel()
    presence_task.cancel()

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
    """Combined athletes from MongoDB + local SSIDs + mesh peers."""
    mongo_athletes = get_athletes()
    local_ssids = get_local_contacts_for_mesh()
    mesh_contacts = mesh.get_all_contacts()
    # Merge all, deduplicate by ss_id
    seen = set()
    result = []
    for source in [mongo_athletes, local_ssids, mesh_contacts]:
        for c in source:
            ssid = c.get("ss_id", "")
            if ssid and ssid not in seen:
                seen.add(ssid)
                result.append(c)
    return result

@app.get("/contacts")
def get_contacts():
    """All contacts: local + mongo + mesh (same as /api/athletes)."""
    return api_athletes()

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


@app.post("/register")
async def register_pager(req: RegisterReq, request: Request = None):
    if request is not None and not rate_limiter.allow(client_key(request, "register"), limit=10, window_s=60):
        return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)
    ssid = generate_ssid()
    label = req.display_name or req.label or req.name or ""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO ssids (ssid, label, created_at) VALUES (?, ?, ?)",
        (ssid, label, now),
    )
    conn.commit()
    conn.close()
    return {"ssid": ssid, "ss_id": ssid, "display_name": label, "created_at": now}

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
    return ""


async def notify_nezhri_telegram(from_ss: str, to_ss: str, text: str) -> None:
    """Forward a leaderboard message to the recipient's Telegram via NeZhri.

    Best-effort: any failure is swallowed so the pager's own delivery is never
    affected. NeZhri resolves to_ss → Telegram user and adds the sender name.
    """
    if not (NEZHRI_NOTIFY_URL and NEZHRI_NOTIFY_API_KEY):
        return
    payload = {
        "to_ssid": to_ss,
        "from_ssid": from_ss,
        "from_name": label_for_ssid(from_ss),
        "text": text,
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


@app.post("/message")
async def send_message(req: MessageReq, request: Request = None):
    if request is not None and not rate_limiter.allow(client_key(request, "message"), limit=60, window_s=60):
        return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)
    # Input validation: bound sizes to protect the DB and mesh.
    if not req.text or len(req.text) > MAX_TEXT_LEN:
        return {"ok": False, "error": "text empty or too long"}
    if not req.target or len(req.target) > MAX_SSID_LEN:
        return {"ok": False, "error": "invalid target"}

    now = datetime.now(timezone.utc).isoformat()
    msg = MeshMessage.create(req.from_ss, req.target, req.text)

    # Persist immediately (store-and-forward); delivery status updated below.
    store_message(msg.msg_id, req.from_ss, req.target, req.text, now, delivered=False)

    # Mirror to the recipient's Telegram via NeZhri (fire-and-forget) so a
    # "написать спортсмену" message from the leaderboard always reaches them,
    # online or not. Does not affect pager delivery below.
    asyncio.create_task(notify_nezhri_telegram(req.from_ss, req.target, req.text))

    # Local delivery shortcut.
    if ws_mgr.connections.get(req.target):
        await ws_mgr.send_to(req.target, {
            "text": req.text, "from_ss": req.from_ss, "to_ss": req.target,
            "created_at": now, "msg_id": msg.msg_id,
        })
        mark_delivered(msg.msg_id, req.target)
        return {"ok": True, "route": "local", "msg_id": msg.msg_id}

    # Otherwise hand to the mesh router for multi-hop forwarding.
    result = await route_mesh_message(msg)
    if result["forwarded"]:
        return {"ok": True, "route": "mesh", "msg_id": msg.msg_id, "peers": result["forwarded"]}
    return {"ok": True, "route": "stored", "msg_id": msg.msg_id,
            "note": "Target not reachable yet, message saved for forwarding"}

@app.get("/messages/{ss_id}")
def get_messages(ss_id: str, limit: int = Query(default=100)):
    """Get messages for a SSID (sent or received)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT from_ss, to_ss, text, created_at FROM messages WHERE from_ss = ? OR to_ss = ? ORDER BY id DESC LIMIT ?",
        (ss_id, ss_id, limit)
    ).fetchall()
    conn.close()
    return [{"from_ss": r["from_ss"], "to_ss": r["to_ss"], "text": r["text"], "created_at": r["created_at"]} for r in rows]

@app.websocket("/ws/{ss_id}")
async def ws_endpoint(ws: WebSocket, ss_id: str):
    await ws_mgr.connect(ss_id, ws)
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
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                msg_type = msg.get("type", "")

                if msg_type == "ping":
                    # Client heartbeat
                    ws_mgr.update_ping(ss_id)
                    # Broadcast updated presence to all clients
                    await ws_mgr.broadcast({
                        "type": "presence",
                        "contacts": ws_mgr.get_all_presence()
                    })

                elif msg_type == "get_presence":
                    # Client requesting current presence data
                    await ws.send_json({
                        "type": "presence",
                        "contacts": ws_mgr.get_all_presence()
                    })

                elif msg_type == "call_signal":
                    # WebRTC signaling relay: offer/answer/ICE/end between two
                    # SSIDs connected to this node. Media then flows P2P.
                    target = msg.get("target", "")
                    payload = msg.get("payload", {})
                    if target and ws_mgr.connections.get(target):
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
                    await send_message(MessageReq(text=msg["text"], target=msg["target"], from_ss=ss_id))

            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        ws_mgr.disconnect(ss_id, ws)
        # Broadcast updated presence (this client is now offline/zombie)
        try:
            await ws_mgr.broadcast({
                "type": "presence",
                "contacts": ws_mgr.get_all_presence()
            })
        except:
            pass
    except Exception:
        ws_mgr.disconnect(ss_id, ws)
        try:
            await ws_mgr.broadcast({
                "type": "presence",
                "contacts": ws_mgr.get_all_presence()
            })
        except:
            pass

# --- Mesh Endpoints ---

@app.post("/mesh/hello")
async def mesh_hello(req: MeshHelloReq):
    """Receive ping from a neighbor node + their contact list. Reply with ours.

    The caller advertises its own reachable URL via `self_url`; we store that so
    return-routing works. Previously we stored a bogus `peer-<id>` placeholder,
    which made reverse delivery impossible.
    """
    peer_url = req.self_url if req.self_url.startswith("http") else f"peer-{req.node_id}"
    mesh.update_peer(req.node_id, peer_url, req.contacts)
    return {
        "node_id": NODE_ID,
        "contacts": get_local_contacts_for_mesh(),
        "timestamp": time.time(),
        "self_url": NODE_URL,
    }

@app.post("/mesh/register")
async def mesh_register(req: dict):
    """Register a new peer node (auto-discovery)."""
    peer_url = req.get("url", "")
    node_id = req.get("node_id", "")
    if peer_url and peer_url not in MESH_PEERS:
        MESH_PEERS.append(peer_url)
        return {"ok": True, "peers": MESH_PEERS}
    return {"ok": False, "reason": "already_exists"}

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
    status["peers_list"] = MESH_PEERS
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
    import re
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
