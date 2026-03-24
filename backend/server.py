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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import pymongo

# --- Config ---
PORT = 9009
DB_PATH = "pager.db"
BASE_DIR = Path(__file__).resolve().parent
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/fitness_bot")

# --- Mesh Config ---
# Neighbor nodes: list of base URLs this node can reach for mesh routing
MESH_PEERS = json.loads(os.getenv("MESH_PEERS", "[]"))  # e.g. ["http://10.0.0.2:9009","http://10.0.0.3:9009"]
NODE_ID = os.getenv("NODE_ID", f"node-{secrets.token_hex(3)}")
MESH_PING_INTERVAL = 30  # seconds

# --- DB ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# --- Mongo Integration ---
def get_athletes():
    """Get athletes from MongoDB who have pager_ssid or are monitored."""
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
                # Keep last_ping for zombie detection
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
        # Update route table
        for c in contacts:
            ssid = c.get("ss_id", "")
            if ssid:
                self.route_table[ssid] = node_id

    def remove_peer(self, node_id: str):
        self.peers.pop(node_id, None)
        self.peer_contacts.pop(node_id, None)
        # Clean route table
        to_remove = [ssid for ssid, nid in self.route_table.items() if nid == node_id]
        for ssid in to_remove:
            del self.route_table[ssid]

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

mesh = MeshNetwork()

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
                        "timestamp": time.time()
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
            from_ss TEXT NOT NULL,
            to_ss TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def generate_ssid() -> str:
    while True:
        suffix = secrets.token_hex(2)
        ssid = f"ss-{suffix}-pager"
        conn = get_db()
        row = conn.execute("SELECT 1 FROM ssids WHERE ssid = ?", (ssid,)).fetchone()
        conn.close()
        if row is None:
            return ssid

# --- App Lifecycle ---
async def start_mesh_tasks(app):
    app.state.mesh_task = asyncio.create_task(mesh_ping_loop())

async def presence_cleanup_loop():
    """Periodically clean stale presence entries and broadcast updates."""
    while True:
        await asyncio.sleep(60)
        # Remove entries older than 10 minutes
        cutoff = time.time() - 600
        to_remove = [sid for sid, ts in ws_mgr.last_ping.items() if ts < cutoff]
        for sid in to_remove:
            if sid not in ws_mgr.connections:
                del ws_mgr.last_ping[sid]

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    mesh_task = asyncio.create_task(mesh_ping_loop())
    presence_task = asyncio.create_task(presence_cleanup_loop())
    yield
    mesh_task.cancel()
    presence_task.cancel()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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

@app.post("/register")
async def register_pager(req: RegisterReq):
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

@app.post("/message")
async def send_message(req: MessageReq):
    now = datetime.now(timezone.utc).isoformat()
    # Save to DB
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (from_ss, to_ss, text, created_at) VALUES (?, ?, ?, ?)",
        (req.from_ss, req.target, req.text, now),
    )
    conn.commit()
    conn.close()
    # Try local delivery first
    if ws_mgr.connections.get(req.target):
        await ws_mgr.send_to(req.target, {
            "text": req.text,
            "from_ss": req.from_ss,
            "to_ss": req.target,
            "created_at": now
        })
        return {"ok": True, "route": "local"}
    # Try mesh routing
    peer_url = mesh.find_route(req.target)
    if peer_url:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(f"{peer_url}/mesh/deliver", json={
                    "from_ss": req.from_ss,
                    "target_ss": req.target,
                    "text": req.text,
                    "origin_node": NODE_ID
                })
                if resp.status_code == 200:
                    return {"ok": True, "route": "mesh", "peer": peer_url}
        except:
            pass
    return {"ok": True, "route": "stored", "note": "Target not connected, message saved"}

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

# --- Mesh Endpoints ---

@app.post("/mesh/hello")
async def mesh_hello(req: MeshHelloReq):
    """Receive ping from a neighbor node + their contact list. Reply with ours."""
    # Auto-detect URL from request
    peer_url = str(req.timestamp)  # placeholder, real URL comes from config
    # Find matching peer URL from config
    for url in MESH_PEERS:
        # Simple heuristic: if node_id matches what we've seen, update
        pass
    # Update mesh state with any URL (we track by node_id)
    mesh.update_peer(req.node_id, f"peer-{req.node_id}", req.contacts)
    return {
        "node_id": NODE_ID,
        "contacts": get_local_contacts_for_mesh(),
        "timestamp": time.time()
    }

@app.post("/mesh/deliver")
async def mesh_deliver(req: MeshMessageReq):
    """Receive a message from a peer node for local delivery."""
    now = datetime.now(timezone.utc).isoformat()
    # Save to DB
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (from_ss, to_ss, text, created_at) VALUES (?, ?, ?, ?)",
        (req.from_ss, req.target_ss, req.text, now),
    )
    conn.commit()
    conn.close()
    # Try local WebSocket delivery
    await ws_mgr.send_to(req.target_ss, {
        "text": req.text,
        "from_ss": req.from_ss,
        "to_ss": req.target_ss,
        "created_at": now
    })
    return {"ok": True, "delivered": True}

@app.get("/mesh/status")
def mesh_status():
    """Current mesh network status."""
    return mesh.get_status()

@app.get("/health")
def health():
    return {"status": "ok", "node_id": NODE_ID, "peers": len(mesh.peers)}

@app.get("/download_node_kit")
def download_node_kit():
    """Generate and stream a .tar.gz archive for deploying a new Pager node."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        # 1. server.py
        with open(BASE_DIR / "server.py", "rb") as f:
            tar.addfile(tarfile.TarInfo(name="server.py"), f)
        # 2. index.html
        with open(BASE_DIR / "index.html", "rb") as f:
            tar.addfile(tarfile.TarInfo(name="index.html"), f)
        # 3. requirements.txt
        req_content = b"fastapi\nuvicorn[standard]\npymongo\npython-multipart\n"
        tar.addfile(tarfile.TarInfo(name="requirements.txt"), io.BytesIO(req_content))
        # 4. deploy.sh
        deploy_content = b"""#!/bin/bash
set -e
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
echo "Starting Pager Node..."
uvicorn server:app --host 0.0.0.0 --port 8000
"""
        tar.addfile(tarfile.TarInfo(name="deploy.sh"), io.BytesIO(deploy_content))
        # 5. README_NODE.txt
        readme_content = b"""# PAGER NODE KIT
## Quick Start
1. chmod +x deploy.sh
2. ./deploy.sh
## Environment Variables
NODE_ID=your-node-id
MESH_PEERS='["http://other-node:8000"]'
## Mesh Network
Nodes ping each other every 30s and exchange SSID lists.
"""
        tar.addfile(tarfile.TarInfo(name="README_NODE.txt"), io.BytesIO(readme_content))
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
