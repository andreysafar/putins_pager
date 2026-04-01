#!/usr/bin/env python3
import os
import sqlite3
import secrets
import asyncio
import hmac
import hashlib
import json
import time
import base64
import httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import pymongo
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.responses import JSONResponse

# --- Config ---
PORT = int(os.getenv("PORT", "9009"))
DB_PATH = os.getenv("DB_PATH", "pager.db")
BASE_DIR = Path(__file__).resolve().parent
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/mesh_pager")
APP_SECRET = os.getenv("APP_SECRET", secrets.token_hex(32))
MESH_SECRET = os.getenv("MESH_SECRET", "")
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")
MESSAGE_TTL_DEFAULT = int(os.getenv("MESSAGE_TTL_DEFAULT", "86400"))  # 24h
CLEANUP_INTERVAL = 300  # 5 min

# --- Mesh Config ---
MESH_PEERS = json.loads(os.getenv("MESH_PEERS", "[]"))
NODE_ID = os.getenv("NODE_ID", f"node-{secrets.token_hex(3)}")
MESH_PING_INTERVAL = 30

# --- Rate Limiter ---
limiter = Limiter(key_func=get_remote_address)

# --- Auth ---
def generate_token(ss_id: str) -> str:
    return hmac.new(APP_SECRET.encode(), ss_id.encode(), hashlib.sha256).hexdigest()

def verify_token(ss_id: str, token: str) -> bool:
    expected = generate_token(ss_id)
    return hmac.compare_digest(expected, token)

def require_auth(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    parts = auth[7:].split(":", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=401, detail="Invalid token format. Expected ss_id:token")
    ss_id, token = parts
    if not verify_token(ss_id, token):
        raise HTTPException(status_code=401, detail="Invalid token")
    return ss_id

# --- DB ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# --- Mongo Integration ---
def get_athletes():
    try:
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        db = client.get_database()
        users = list(db.users.find(
            {"pager_ssid": {"$exists": True}},
            {"_id": 0, "username": 1, "first_name": 1, "pager_ssid": 1, "display_name": 1, "public_key": 1}
        ).limit(50))
        if not users:
            users = list(db.monitored_athletes.find(
                {},
                {"_id": 0, "name": 1, "pager_ssid": 1, "public_key": 1}
            ).limit(50))
        result = []
        for u in users:
            result.append({
                "ss_id": u.get("pager_ssid", u.get("ssid", "")),
                "display_name": u.get("display_name") or u.get("first_name") or u.get("username") or u.get("name", "ATHLETE"),
                "public_key": u.get("public_key", ""),
            })
        return [r for r in result if r["ss_id"]]
    except Exception as e:
        print(f"Mongo error: {e}")
        return []

# --- WebSocket manager ---
class WSManager:
    def __init__(self):
        self.connections: dict[str, set[WebSocket]] = {}

    async def connect(self, ss_id: str, ws: WebSocket):
        await ws.accept()
        self.connections.setdefault(ss_id, set()).add(ws)
        # Store-and-forward: flush undelivered messages
        await self._flush_pending(ss_id, ws)

    async def _flush_pending(self, ss_id: str, ws: WebSocket):
        conn = get_db()
        rows = conn.execute(
            "SELECT id, from_ss, to_ss, text, created_at FROM messages "
            "WHERE to_ss = ? AND delivered = 0 ORDER BY id ASC",
            (ss_id,)
        ).fetchall()
        delivered_ids = []
        for r in rows:
            try:
                await ws.send_json({
                    "text": r["text"],
                    "from_ss": r["from_ss"],
                    "to_ss": r["to_ss"],
                    "created_at": r["created_at"],
                })
                delivered_ids.append(r["id"])
            except:
                break
        if delivered_ids:
            placeholders = ",".join("?" * len(delivered_ids))
            conn.execute(f"UPDATE messages SET delivered = 1 WHERE id IN ({placeholders})", delivered_ids)
            conn.commit()
        conn.close()

    def disconnect(self, ss_id: str, ws: WebSocket):
        if ss_id in self.connections:
            self.connections[ss_id].discard(ws)

    async def send_to(self, ss_id: str, data: dict):
        for ws in list(self.connections.get(ss_id, [])):
            try:
                await ws.send_json(data)
            except:
                self.disconnect(ss_id, ws)

ws_mgr = WSManager()

# --- Mesh Network State ---
class MeshNetwork:
    def __init__(self):
        self.peers: dict[str, dict] = {}
        self.peer_contacts: dict[str, list] = {}
        self.route_table: dict[str, str] = {}

    def update_peer(self, node_id: str, url: str, contacts: list):
        self.peers[node_id] = {
            "url": url,
            "last_seen": time.time(),
            "status": "online"
        }
        self.peer_contacts[node_id] = contacts
        for c in contacts:
            ssid = c.get("ss_id", "")
            if ssid:
                self.route_table[ssid] = node_id

    def remove_peer(self, node_id: str):
        self.peers.pop(node_id, None)
        self.peer_contacts.pop(node_id, None)
        to_remove = [ssid for ssid, nid in self.route_table.items() if nid == node_id]
        for ssid in to_remove:
            del self.route_table[ssid]

    def get_all_contacts(self) -> list:
        seen = set()
        result = []
        conn = get_db()
        rows = conn.execute("SELECT ssid, label, public_key FROM ssids").fetchall()
        conn.close()
        for r in rows:
            if r["ssid"] not in seen:
                seen.add(r["ssid"])
                result.append({
                    "ss_id": r["ssid"],
                    "display_name": r["label"] or r["ssid"],
                    "public_key": r["public_key"] or "",
                })
        for node_id, contacts in self.peer_contacts.items():
            for c in contacts:
                ssid = c.get("ss_id", "")
                if ssid and ssid not in seen:
                    seen.add(ssid)
                    result.append(c)
        return result

    def find_route(self, target_ss: str) -> Optional[str]:
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

mesh = MeshNetwork()

# --- Mesh Background Tasks ---
async def mesh_ping_loop():
    while True:
        my_contacts = get_local_contacts_for_mesh()
        headers = {}
        if MESH_SECRET:
            headers["X-Mesh-Secret"] = MESH_SECRET
        for peer_url in MESH_PEERS:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.post(f"{peer_url}/mesh/hello", json={
                        "node_id": NODE_ID,
                        "contacts": my_contacts,
                        "timestamp": time.time()
                    }, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        mesh.update_peer(
                            data["node_id"],
                            peer_url,
                            data.get("contacts", [])
                        )
            except Exception:
                pass
        for nid in list(mesh.peers.keys()):
            if time.time() - mesh.peers[nid]["last_seen"] > MESH_PING_INTERVAL * 5:
                mesh.remove_peer(nid)
        await asyncio.sleep(MESH_PING_INTERVAL)

async def cleanup_expired_messages():
    while True:
        try:
            conn = get_db()
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("DELETE FROM messages WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
            conn.commit()
            conn.close()
        except Exception:
            pass
        await asyncio.sleep(CLEANUP_INTERVAL)

def get_local_contacts_for_mesh() -> list:
    conn = get_db()
    rows = conn.execute("SELECT ssid, label, public_key FROM ssids").fetchall()
    conn.close()
    return [{"ss_id": r["ssid"], "display_name": r["label"] or r["ssid"], "public_key": r["public_key"] or ""} for r in rows]

# --- Pydantic Models ---
class MessageReq(BaseModel):
    text: str
    target: str
    from_ss: str = ""
    ttl_seconds: Optional[int] = None

class RegisterReq(BaseModel):
    label: str = ""
    name: str = ""
    display_name: str = ""
    public_key: str = ""

class MeshHelloReq(BaseModel):
    node_id: str
    contacts: list
    timestamp: float

class MeshMessageReq(BaseModel):
    from_ss: str
    target_ss: str
    text: str
    origin_node: str = ""

# --- Mesh Auth ---
def verify_mesh_secret(request: Request):
    if MESH_SECRET:
        provided = request.headers.get("X-Mesh-Secret", "")
        if not hmac.compare_digest(provided, MESH_SECRET):
            raise HTTPException(status_code=403, detail="Invalid mesh secret")

# --- DB Init ---
def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ssids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ssid TEXT UNIQUE NOT NULL,
            label TEXT DEFAULT '',
            public_key TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_ss TEXT NOT NULL,
            to_ss TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT DEFAULT NULL,
            delivered INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    # Migrate: add columns if missing (for existing DBs)
    try:
        conn.execute("ALTER TABLE ssids ADD COLUMN public_key TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN expires_at TEXT DEFAULT NULL")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN delivered INTEGER DEFAULT 0")
    except Exception:
        pass
    conn.commit()
    conn.close()

def generate_ssid() -> str:
    while True:
        suffix = secrets.token_hex(8)
        ssid = f"ss-{suffix}-pager"
        conn = get_db()
        row = conn.execute("SELECT 1 FROM ssids WHERE ssid = ?", (ssid,)).fetchone()
        conn.close()
        if row is None:
            return ssid

# --- App Lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    mesh_task = asyncio.create_task(mesh_ping_loop())
    cleanup_task = asyncio.create_task(cleanup_expired_messages())
    yield
    mesh_task.cancel()
    cleanup_task.cancel()

app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded. Slow down."})

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Routes ---

@app.get("/")
def serve_index():
    return FileResponse(BASE_DIR / "index.html")

@app.get("/api/athletes")
def api_athletes():
    mongo_athletes = get_athletes()
    local_ssids = get_local_contacts_for_mesh()
    mesh_contacts = mesh.get_all_contacts()
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
@limiter.limit("10/minute")
def get_contacts(request: Request):
    return api_athletes()

@app.get("/contacts/{ss_id}/key")
def get_contact_key(ss_id: str):
    conn = get_db()
    row = conn.execute("SELECT public_key FROM ssids WHERE ssid = ?", (ss_id,)).fetchone()
    conn.close()
    if row and row["public_key"]:
        return {"ss_id": ss_id, "public_key": row["public_key"]}
    raise HTTPException(status_code=404, detail="Public key not found")

@app.post("/register")
@limiter.limit("5/minute")
async def register_pager(req: RegisterReq, request: Request):
    ssid = generate_ssid()
    label = req.display_name or req.label or req.name or ""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO ssids (ssid, label, public_key, created_at) VALUES (?, ?, ?, ?)",
        (ssid, label, req.public_key, now),
    )
    conn.commit()
    conn.close()
    token = generate_token(ssid)
    return {
        "ssid": ssid,
        "ss_id": ssid,
        "display_name": label,
        "token": token,
        "created_at": now,
    }

@app.post("/message")
@limiter.limit("30/minute")
async def send_message(req: MessageReq, request: Request, caller_ss: str = Depends(require_auth)):
    now = datetime.now(timezone.utc).isoformat()
    expires_at = None
    ttl = req.ttl_seconds if req.ttl_seconds is not None else MESSAGE_TTL_DEFAULT
    if ttl > 0:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()

    from_ss = caller_ss

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO messages (from_ss, to_ss, text, created_at, expires_at, delivered) VALUES (?, ?, ?, ?, ?, 0)",
        (from_ss, req.target, req.text, now, expires_at),
    )
    msg_id = cur.lastrowid
    conn.commit()
    conn.close()

    msg_data = {
        "text": req.text,
        "from_ss": from_ss,
        "to_ss": req.target,
        "created_at": now
    }

    # Try local delivery
    if ws_mgr.connections.get(req.target):
        await ws_mgr.send_to(req.target, msg_data)
        conn = get_db()
        conn.execute("UPDATE messages SET delivered = 1 WHERE id = ?", (msg_id,))
        conn.commit()
        conn.close()
        return {"ok": True, "route": "local"}

    # Try mesh routing
    peer_url = mesh.find_route(req.target)
    if peer_url:
        try:
            headers = {}
            if MESH_SECRET:
                headers["X-Mesh-Secret"] = MESH_SECRET
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(f"{peer_url}/mesh/deliver", json={
                    "from_ss": from_ss,
                    "target_ss": req.target,
                    "text": req.text,
                    "origin_node": NODE_ID
                }, headers=headers)
                if resp.status_code == 200:
                    conn = get_db()
                    conn.execute("UPDATE messages SET delivered = 1 WHERE id = ?", (msg_id,))
                    conn.commit()
                    conn.close()
                    return {"ok": True, "route": "mesh", "peer": peer_url}
        except Exception:
            pass

    return {"ok": True, "route": "stored", "note": "Target offline — message queued for delivery"}

@app.get("/messages/{ss_id}")
def get_messages(ss_id: str, limit: int = Query(default=100)):
    conn = get_db()
    rows = conn.execute(
        "SELECT from_ss, to_ss, text, created_at FROM messages WHERE from_ss = ? OR to_ss = ? ORDER BY id DESC LIMIT ?",
        (ss_id, ss_id, limit)
    ).fetchall()
    conn.close()
    return [{"from_ss": r["from_ss"], "to_ss": r["to_ss"], "text": r["text"], "created_at": r["created_at"]} for r in rows]

@app.websocket("/ws/{ss_id}")
async def ws_endpoint(ws: WebSocket, ss_id: str, token: str = Query(default="")):
    # Validate token for WebSocket auth
    if not verify_token(ss_id, token):
        await ws.close(code=4001, reason="Invalid token")
        return
    await ws_mgr.connect(ss_id, ws)
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if "target" in msg and "text" in msg:
                    # Internal WS send — trusted since token was validated
                    now = datetime.now(timezone.utc).isoformat()
                    expires_at = None
                    ttl = msg.get("ttl_seconds", MESSAGE_TTL_DEFAULT)
                    if ttl and ttl > 0:
                        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
                    conn = get_db()
                    cur = conn.execute(
                        "INSERT INTO messages (from_ss, to_ss, text, created_at, expires_at, delivered) VALUES (?, ?, ?, ?, ?, 0)",
                        (ss_id, msg["target"], msg["text"], now, expires_at),
                    )
                    msg_id = cur.lastrowid
                    conn.commit()
                    conn.close()
                    if ws_mgr.connections.get(msg["target"]):
                        await ws_mgr.send_to(msg["target"], {
                            "text": msg["text"], "from_ss": ss_id,
                            "to_ss": msg["target"], "created_at": now
                        })
                        conn = get_db()
                        conn.execute("UPDATE messages SET delivered = 1 WHERE id = ?", (msg_id,))
                        conn.commit()
                        conn.close()
            except Exception:
                pass
    except WebSocketDisconnect:
        ws_mgr.disconnect(ss_id, ws)

# --- Mesh Endpoints ---

@app.post("/mesh/hello")
async def mesh_hello(req: MeshHelloReq, request: Request):
    verify_mesh_secret(request)
    mesh.update_peer(req.node_id, f"peer-{req.node_id}", req.contacts)
    return {
        "node_id": NODE_ID,
        "contacts": get_local_contacts_for_mesh(),
        "timestamp": time.time()
    }

@app.post("/mesh/deliver")
async def mesh_deliver(req: MeshMessageReq, request: Request):
    verify_mesh_secret(request)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO messages (from_ss, to_ss, text, created_at, delivered) VALUES (?, ?, ?, ?, 0)",
        (req.from_ss, req.target_ss, req.text, now),
    )
    msg_id = cur.lastrowid
    conn.commit()
    conn.close()
    if ws_mgr.connections.get(req.target_ss):
        await ws_mgr.send_to(req.target_ss, {
            "text": req.text,
            "from_ss": req.from_ss,
            "to_ss": req.target_ss,
            "created_at": now
        })
        conn = get_db()
        conn.execute("UPDATE messages SET delivered = 1 WHERE id = ?", (msg_id,))
        conn.commit()
        conn.close()
    return {"ok": True, "delivered": True}

@app.get("/mesh/status")
def mesh_status():
    return mesh.get_status()

@app.get("/health")
def health():
    return {"status": "ok", "node_id": NODE_ID, "peers": len(mesh.peers)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
