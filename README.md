# Safarancho Pager — Mesh Network Messenger v7.0

Децентрализованный мессенджер с Mesh-сетью. Стиль: Пейджер 90-х + Matrix + Fallout.

## Компоненты

| Папка | Описание |
|-------|----------|
| `backend/` | FastAPI сервер (порт 9009). Регистрация, WebSocket, Mesh, файлы |
| `android/` | Android-приложение (Kotlin). Подключается к backend |

## Быстрый старт (Backend)

```bash
cd backend
cp .env.example .env   # настройка NODE_ID, MESH_PEERS
pip install -r requirements.txt
python -m uvicorn server:app --host 0.0.0.0 --port 9009
```

Или скачай готовый Node Kit:
```bash
# В браузере: http://localhost:9009 → кнопка "Download Node Kit"
# Распаковать и: chmod +x deploy.sh && ./deploy.sh
```

## Web-интерфейс

Открой `http://localhost:9009` в браузере.

### Особенности:
- **Matrix Rain** — фоновый эффект
- **Телефонная книга** — ★ Избранное / 📡 Ближайшие ноды (эта нода + прямые
  соседи) / 🛰 Удалённые контакты (соседи соседей, 2+ хопа)
- **Статусы**: 🟢 Онлайн / 🧟 Зомби (сессия висит, но не отвечает) / ⚪ Оффлайн
- **Вкладки чатов** — переключение между контактами, история подгружается с ноды
- **Ссылки**: `/?to=<ss>` открывает чат (кнопка «Написать» на iron-siberia.ru),
  `/?login=<код>&to=<ss>` — предложение начать новую сессию из уведомления в Telegram
- **Загрузка файлов** — кнопка 📎, превью картинок

## Android

Открой `android/` в Android Studio. Настрой `BASE_URL` в `ApiService.kt` → Собери APK.

### Работает:
- Регистрация / Logout
- Чат (текст + картинки)
- Уведомления (только при открытом приложении)

## Mesh-сеть (децентрализация)

Ноды обмениваются сообщениями через пиры:
```bash
# В .env:
MESH_PEERS='["http://другая-нода:9009"]'
```

Ноды пингуют друг друга каждые 30 сек. Мастер-нода (pager.iron-siber.ru, без
`MAIN_NODE_URL`) хранит справочник атлетов Iron Siber (SSID ↔ атлет NeZhri) и
раздаёт его по mesh. Подробнее — в [MESH.md](MESH.md#телефонная-книга-статусы-и-сессии).

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/register` | Создать SS-ID (анонимный) |
| GET | `/contacts` | Плоский список контактов (локальные + mesh) — для Android |
| GET | `/phonebook?ss_id=` | Иерархическая книга: избранное / ближайшие ноды / удалённые |
| GET/POST | `/favorites/{ss}` | Избранное (DELETE `/favorites/{ss}/{contact}`) |
| POST | `/session/claim` | Код из Telegram-ссылки → SSID и собеседник |
| GET | `/mesh/route/{ss}` | Где контакт и его статус (online/zombie/offline, хопы) |
| POST | `/message` | Отправить сообщение |
| GET | `/messages/{ss_id}` | История сообщений |
| WS | `/ws/{ss_id}` | Real-time чат |
| GET | `/mesh/status` | Статус Mesh-сети |
| POST | `/mesh/hello` | Ping от пира: контакты с хопами/статусами + список его соседей |
| POST | `/mesh/register` | Нода сообщает о себе мастеру (сохраняется в БД) |
| POST | `/mesh/ingest` | Приём конверта от любого транспорта (multi-hop) |
| POST | `/mesh/deliver` | Legacy single-hop (заворачивается в ingest) |
| POST | `/keys` / GET `/keys/{ss_id}` | Реестр публичных ключей (E2E) |
| WS | `call_signal` | Сигналинг видеозвонков (offer/answer/ICE/end) |
| POST | `/upload` | Загрузить файл |
| GET | `/download_node_kit` | Скачать архив ноды |
| GET | `/health` | Проверка здоровья |

## Видеозвонки и шифрование

- **Видеозвонки**: WebRTC P2P, сигналинг по WebSocket. Кнопка 📹 в
  веб-клиенте и в чате Android; работают веб ↔ веб и веб ↔ Android.
- **E2E**: ECDH P-256 + AES-256-GCM, прозрачно с фолбэком на открытый
  текст. Подробности и ограничения — в [MESH.md](MESH.md).

## Mesh-архитектура

Полное описание маршрутизации, транспортов (интернет / Wi-Fi / BLE),
store-and-forward и дорожной карты — в [MESH.md](MESH.md).

Кратко: сообщения ходят в едином конверте (`msg_id`, `ttl`, `path`),
маршрутизатор (`backend/mesh_router.py`) дедупит и пересылает по хопам,
Android-узел (`android/.../mesh/`) умеет реле по интернету, Wi-Fi (NSD)
и BLE.

### Тесты бэкенда

```bash
cd backend && python -m pytest -q   # router, телефонная книга/статусы, интеграция API
```

## SS-ID Формат

`ss-xxxxxxxx-pager` — 8 hex-символов (32 бита энтропии), генерируется
при регистрации.

## Лицензия

Private project.