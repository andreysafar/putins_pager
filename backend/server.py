#!/usr/bin/env python3
"""Safarancho Swimming Team — Pager Backend (FastAPI)"""

import os
import json
import sqlite3
import secrets
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

# --- Config ---
PORT = int(os.getenv("PORT", "8888"))
DB_PATH = os.getenv("DB_PATH", "pager.db")
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")
BASE_DIR = Path(__file__).resolve().parent

# --- DB ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            ss_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_ss TEXT NOT NULL,
            to_ss TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            read INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_msg_to ON messages(to_ss, created_at);
        CREATE INDEX IF NOT EXISTS idx_msg_from ON messages(from_ss);
    """)
    conn.commit()
    conn.close()

def gen_ss_id():
    return f"ss-{secrets.token_hex(3)[:5]}-pager"

# --- Models ---
class RegisterReq(BaseModel):
    name: str
    display_name: str

class MessageReq(BaseModel):
    from_ss: str
    to_ss: str
    text: str

# --- WebSocket manager ---
class WSManager:
    def __init__(self):
        self.connections: dict[str, set[WebSocket]] = {}

    async def connect(self, ss_id: str, ws: WebSocket):
        await ws.accept()
        self.connections.setdefault(ss_id, set()).add(ws)

    def disconnect(self, ss_id: str, ws: WebSocket):
        conns = self.connections.get(ss_id)
        if conns:
            conns.discard(ws)

    async def send_to(self, ss_id: str, data: dict):
        for ws in list(self.connections.get(ss_id, [])):
            try:
                await ws.send_json(data)
            except Exception:
                self.disconnect(ss_id, ws)

ws_mgr = WSManager()

# --- App ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Safarancho Pager", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS,
                   allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def serve_index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/register")
def register(req: RegisterReq):
    ss_id = gen_ss_id()
    conn = get_db()
    try:
        conn.execute("INSERT INTO users (ss_id, name, display_name, created_at) VALUES (?,?,?,?)",
                      (ss_id, req.name, req.display_name, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()
    return {"ss_id": ss_id, "display_name": req.display_name}

@app.get("/contacts")
def contacts():
    conn = get_db()
    rows = conn.execute("SELECT ss_id, display_name FROM users ORDER BY created_at").fetchall()
    conn.close()
    return [{"ss_id": r["ss_id"], "display_name": r["display_name"]} for r in rows]

@app.post("/message")
async def send_message(req: MessageReq):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute("INSERT INTO messages (from_ss, to_ss, text, created_at) VALUES (?,?,?,?)",
                  (req.from_ss, req.to_ss, req.text, now))
    conn.commit()
    conn.close()
    msg = {"from_ss": req.from_ss, "to_ss": req.to_ss, "text": req.text, "created_at": now}
    await ws_mgr.send_to(req.to_ss, msg)
    return {"ok": True, "created_at": now}

@app.get("/messages/{ss_id}")
def get_messages(ss_id: str, limit: int = 50, after: str | None = None):
    conn = get_db()
    if after:
        rows = conn.execute(
            "SELECT * FROM messages WHERE (to_ss=? OR from_ss=?) AND created_at > ? ORDER BY created_at DESC LIMIT ?",
            (ss_id, ss_id, after, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM messages WHERE (to_ss=? OR from_ss=?) ORDER BY created_at DESC LIMIT ?",
            (ss_id, ss_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.websocket("/ws/{ss_id}")
async def ws_endpoint(ws: WebSocket, ss_id: str):
    await ws_mgr.connect(ss_id, ws)
    try:
        while True:
            data = await ws.receive_json()
            # Client can send messages via WS too
            if data.get("action") == "send":
                req = MessageReq(from_ss=ss_id, to_ss=data["to_ss"], text=data["text"])
                await send_message(req)
    except WebSocketDisconnect:
        ws_mgr.disconnect(ss_id, ws)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
