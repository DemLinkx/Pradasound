# Prada Sound backend

Python 3.12+, FastAPI, one Uvicorn worker. Lobbies, queues and Yandex sessions
live in memory and are cleared on restart. No database is required for this MVP.

## Local development

From the repository root:

```sh
uv sync --project backend --locked --python 3.12
uv run --directory backend uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
uv run --directory backend --locked pytest
```

Settings come from environment variables and the repository-root `.env`.
For Vite, set `CORS_ORIGINS=["http://localhost:5173","http://127.0.0.1:5173"]`.
`YOUTUBE_AUDIO_ENABLED=false` disables experimental audio extraction and lets
Main fall back to the visible YouTube iframe. Docker always uses this mode.

## Files

- `app/api/lobby.py`: in-memory rooms, queue, Telegram identity and bot webhook.
- `app/main.py`: YouTube/Yandex endpoints and author-specific Yandex playback.
- `app/settings.py`: environment-backed settings.
- `Dockerfile`: locked runtime dependencies, non-root process, healthcheck.
- `tests/domain/`: API, lobby, provider credential isolation and deployment tests.

Yandex playback links use the adding member's account; audio is played by Main.
Production Docker uses YouTube iframe playback. The local experimental YouTube
mode includes an on-the-fly HTTP audio relay, without saving media on disk.
See [deployment instructions](../infra/README.md) and the root README.
