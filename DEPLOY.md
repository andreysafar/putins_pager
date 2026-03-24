# Deploy notes (telegram.iron-siberia.ru)

## Nginx

- Vhost: `/etc/nginx/sites-available/telegram`
- Upstream: **`127.0.0.1:9009`** (not 8888 unless you change nginx too).

## Backend process

From `backend/`:

```bash
python3 -m uvicorn server:app --host 127.0.0.1 --port 9009
```

- Must serve **`GET /`** → `index.html` (see `server.py`).
- Do not run legacy `pager.py` on 9009 if you want the new Swimming Team UI.

## Android

- Use **Kotlin DSL** in `*.gradle.kts`: `include(":app")`, never `include :app`.
- Enable **`buildConfig = true`** when using `buildConfigField`.

## Gradle: one root build file

- Use **only** `android/build.gradle.kts` at the project root. A legacy **`build.gradle`** (Groovy) with `buildscript` / `allprojects { repositories }` **conflicts** with `dependencyResolutionManagement` + `FAIL_ON_PROJECT_REPOS` in `settings.gradle.kts` and fails sync with *repositories are also declared in the project*.
