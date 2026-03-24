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
- **Статусы**: 🟢 Онлайн / ⚪ Оффлайн / 🟡 Зомби / 🔴 Убит
- **Вкладки чатов** — переключение между контактами
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

Ноды пингуют друг друга каждые 30 сек.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/register` | Создать SS-ID (анонимный) |
| GET | `/contacts` | Список пользователей из MongoDB |
| POST | `/message` | Отправить сообщение |
| GET | `/messages/{ss_id}` | История сообщений |
| WS | `/ws/{ss_id}` | Real-time чат |
| GET | `/mesh/status` | Статус Mesh-сети |
| POST | `/mesh/hello` | Ping от пира |
| POST | `/mesh/deliver` | Маршрутизация через пиры |
| POST | `/upload` | Загрузить файл |
| GET | `/download_node_kit` | Скачать архив ноды |
| GET | `/health` | Проверка здоровья |

## SS-ID Формат

`ss-xxxx-pager` — 4 hex-символа, генерируется при регистрации.

## Лицензия

Private project.