# syntax=docker/dockerfile:1
FROM golang:1.25.5-bookworm AS spotify-reader
WORKDIR /build
COPY backend/spotify-native/go.mod backend/spotify-native/go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY backend/spotify-native/*.go ./
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build CGO_ENABLED=0 go test ./... && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /spotify-library .

FROM python:3.12-slim-bookworm AS dependencies
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

FROM python:3.12-slim-bookworm AS runtime
ENV PATH="/app/backend/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DENO_DIR=/tmp/deno DENO_NO_UPDATE_CHECK=1 \
    SPOTIFY_NATIVE_HELPER=/usr/local/bin/prada-spotify-library
COPY --from=spotify-reader /spotify-library /usr/local/bin/prada-spotify-library
COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno
RUN groupadd --gid 10001 prada \
    && useradd --uid 10001 --gid prada --no-create-home --shell /usr/sbin/nologin prada
WORKDIR /app/backend
COPY --from=dependencies --chown=10001:10001 /app/backend/.venv ./.venv
COPY --chown=10001:10001 backend/app ./app
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthcheck', timeout=2).close()"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--ws", "websockets", "--ws-max-size", "16384", "--ws-max-queue", "4", "--ws-per-message-deflate", "false", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log", "--timeout-graceful-shutdown", "15"]

ARG SOURCE_URL=""
ARG REVISION="local"
LABEL org.opencontainers.image.source=$SOURCE_URL \
      org.opencontainers.image.revision=$REVISION
