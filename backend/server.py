#!/usr/bin/env python3
import os
import sqlite3
import secrets
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import pymongo

# --- Config ---
PORT = 9009
DB_PATH = "pager.db"
BASE_DIR = Path(__file__).resolve().parent
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/fitness_bot")

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
        # Ищем в коллекциях users или monitored_athletes (проверяем обе)
        users = list(db.users.find({"pager_ssid": {"$exists": True}}, {"_id":0, "username":1, "first_name":1, "pager_ssid":1}).limit(20))
        if not users:
            users = list(db.monitored_athletes.find({}, {"_id":0, "name":1, "pager_ssid":1}).limit(20))
        return users
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
    def disconnect(self, ss_id: str, ws: WebSocket):
        if ss_id in self.connections:
            self.connections[ss_id].discard(ws)
    async def send_to(self, ss_id: str, data: dict):
        for ws in list(self.connections.get(ss_id, [])):
            try: await ws.send_json(data)
            except: self.disconnect(ss_id, ws)

ws_mgr = WSManager()

class MessageReq(BaseModel):
    text: str
    target: str

app = FastAPI(lifespan=lambda a: (yield))
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def serve_index(): return FileResponse(BASE_DIR / "index.html")

@app.get("/api/athletes")
def api_athletes():
    return get_athletes()

@app.post("/message")
async def send_message(req: MessageReq):
    now = datetime.now(timezone.utc).isoformat()
    # Эмуляция отправки (в будущем - в базу)
    await ws_mgr.send_to(req.target, {"text": req.text, "created_at": now})
    return {"ok": True}

@app.websocket("/ws/{ss_id}")
async def ws_endpoint(ws: WebSocket, ss_id: str):
    await ws_mgr.connect(ss_id, ws)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect:
        ws_mgr.disconnect(ss_id, ws)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
