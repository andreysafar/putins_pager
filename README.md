# Safarancho Swimming Team — Pager

Two components:

1. **Backend** (`backend/`) — FastAPI server (port 8888). User registration, SS-ID, messaging, WebSocket.
2. **Android** (`android/`) — Safarancho Pager app (Kotlin). Connects to backend.

## Quick Start (Backend)

```bash
cd backend
cp .env.example .env   # edit if needed
pip install -r requirements.txt
python server.py       # http://localhost:8888
```

## Quick Start (Android)

Open `android/` in Android Studio. Set `BASE_URL` in `build.gradle` or `ApiService.kt`.
Build → APK.

## SS-ID Format

`ss-XXXXX-pager` — 5 random chars, generated on first registration.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | /register | Create user (name, display_name) → SS-ID |
| GET | /contacts | List all registered users |
| POST | /message | Send message (from, to, text) |
| GET | /messages/{ss_id} | Get messages for SS-ID |
| WS | /ws/{ss_id} | Real-time message stream |

## License

Private project.
