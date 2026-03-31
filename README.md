# TriPager — Mesh Pager for Triathlon

Off-grid mesh pager for triathlon races where cellular coverage is unreliable.
Peer-to-peer messaging via mesh network nodes — no operator dependency.

## Components

1. **Backend** (`backend/`) — FastAPI server. Registration, SS-ID, messaging, WebSocket, mesh routing.
2. **Android** (`android/`) — Native Kotlin app. Connects to backend, push notifications, offline mesh.

## Quick Start (Backend)

```bash
cd backend
cp .env.example .env   # edit if needed
pip install -r requirements.txt
python server.py       # http://localhost:9009
```

## Quick Start (Android)

Open `android/` in Android Studio. Set `BASE_URL` in `app/build.gradle.kts`.
Build -> APK.

## SS-ID Format

`ss-XXXX-pager` — 4 random hex chars, generated on registration.

## API

| Method | Path | Description |
|--------|------|-------------|
| POST | /register | Acquire anonymous SS-ID |
| GET | /contacts | List all known contacts (local + mesh) |
| POST | /message | Send message (from, to, text) |
| GET | /messages/{ss_id} | Get message history |
| WS | /ws/{ss_id} | Real-time message stream |
| GET | /mesh/status | Mesh network status |
| POST | /mesh/hello | Mesh peer handshake |
| POST | /mesh/deliver | Mesh message routing |

## Mesh Network

Nodes auto-discover peers and exchange contact lists. Messages route through
available peers when the target is not locally connected. Configure peers
via `MESH_PEERS` env variable.

## License

Private project.
