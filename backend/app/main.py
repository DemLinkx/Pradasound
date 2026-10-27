import asyncio
import re
import secrets
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, SecretStr
from yandex_music import Client, ClientAsync
from yandex_music.exceptions import (
    DeviceAuthError,
    TimedOutError,
    UnauthorizedError,
    YandexMusicError,
)
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from app.api.healthcheck import router as healthcheck_router
from app.api.lobby import LOBBIES
from app.api.lobby import router as lobby_router
from app.api.lobby import telegram_polling_loop
from app.settings import Settings

settings = Settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = (asyncio.create_task(telegram_polling_loop())
            if settings.telegram_polling_enabled and settings.telegram_bot_token else None)
    try:
        yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Prada Sound API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(healthcheck_router)
app.include_router(lobby_router)

@dataclass(frozen=True)
class YouTubeAudio:
    url: str
    expires_at: float
    proxy: str | None
    headers: dict[str, str] = field(default_factory=dict)


YOUTUBE_AUDIO_CACHE: dict[str, YouTubeAudio] = {}
YOUTUBE_METADATA_CACHE: dict[str, tuple[str, str]] = {}
YOUTUBE_AUDIO_TICKETS: dict[str, tuple[str, float]] = {}
YOUTUBE_EXTRACT_LIMIT = asyncio.Semaphore(2)
YOUTUBE_AUDIO_FAILURES: dict[str, tuple[float, str]] = {}
YOUTUBE_AUDIO_LOCKS: dict[str, asyncio.Lock] = {}
YOUTUBE_ROUTE_HEALTH: dict[str | None, tuple[float, float]] = {}


class QuietYoutubeLog:
    def debug(self, message: str) -> None:
        pass

    warning = error = debug


def extract_youtube_audio(video_id: str, proxy: str | None = None) -> tuple[str, float, str, str, dict[str, str]]:
    options: dict[str, Any] = {"format": "bestaudio[ext=m4a]/bestaudio", "quiet": True,
                               "no_warnings": True, "noplaylist": True, "skip_download": True,
                               "proxy": proxy or "", "socket_timeout": 8, "retries": 0,
                               "extractor_retries": 0, "logger": QuietYoutubeLog(),
                               "cachedir": "/tmp/prada-youtube"}
    if settings.youtube_cookies_file:
        if not settings.youtube_cookies_file.is_file():
            raise ValueError("Файл cookies YouTube не найден")
        options["cookiefile"] = str(settings.youtube_cookies_file)
    with YoutubeDL(options) as youtube:
        info = youtube.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    url = info.get("url") if isinstance(info, dict) else None
    parsed = urlparse(url) if isinstance(url, str) else None
    if (
        not parsed or parsed.scheme != "https" or not parsed.hostname
        or not parsed.hostname.endswith(".googlevideo.com")
    ):
        raise ValueError("YouTube не вернул аудиопоток")
    expiry = float(parse_qs(parsed.query).get("expire", [0])[0])
    title = str(info.get("title") or video_id)[:150]
    artist = str(info.get("uploader") or info.get("channel") or "YouTube")[:100]
    headers = {str(key): str(value) for key, value in (info.get("http_headers") or {}).items()
               if key.lower() in ("user-agent", "referer", "origin", "accept-language")}
    return url, min(expiry - 60, time.time() + 600), title, artist, headers


async def open_youtube_stream(
    client: httpx.AsyncClient, source: str, byte_range: str | None = None
) -> httpx.Response:
    for _ in range(4):
        parsed = urlparse(source)
        if (parsed.scheme != "https" or not parsed.hostname
                or not parsed.hostname.endswith(".googlevideo.com")):
            raise HTTPException(status_code=502, detail="Некорректный аудиосервер YouTube")
        response = await client.send(client.build_request(
            "GET", source, headers={"Range": byte_range} if byte_range else {}
        ), stream=True)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("location", "")
        await response.aclose()
        if not location:
            raise HTTPException(status_code=502, detail="Некорректный редирект YouTube")
        source = urljoin(source, location)
    raise HTTPException(status_code=502, detail="Слишком много редиректов YouTube")


async def youtube_audio_url(video_id: str) -> YouTubeAudio:
    if not settings.youtube_audio_enabled:
        raise HTTPException(status_code=503, detail="Используйте встроенный плеер YouTube")
    cached = YOUTUBE_AUDIO_CACHE.get(video_id)
    if cached and cached.expires_at > time.time():
        return cached
    if len(YOUTUBE_AUDIO_LOCKS) >= 512 and video_id not in YOUTUBE_AUDIO_LOCKS:
        for key, candidate in list(YOUTUBE_AUDIO_LOCKS.items()):
            if not candidate.locked():
                YOUTUBE_AUDIO_LOCKS.pop(key, None)
                break
    lock = YOUTUBE_AUDIO_LOCKS.setdefault(video_id, asyncio.Lock())
    async with lock:
        cached = YOUTUBE_AUDIO_CACHE.get(video_id)
        if cached and cached.expires_at > time.time():
            return cached
        failed = YOUTUBE_AUDIO_FAILURES.get(video_id)
        if failed and failed[0] > monotonic():
            raise HTTPException(status_code=503, detail=failed[1])
        async with YOUTUBE_EXTRACT_LIMIT:
            routes = settings.youtube_proxy_urls or [None]
            now = monotonic()
            routes = sorted(routes, key=lambda route: (
                YOUTUBE_ROUTE_HEALTH.get(route, (0, 30))[0] > now,
                YOUTUBE_ROUTE_HEALTH.get(route, (0, 30))[1],
            ))
            for proxy in routes:
                started = monotonic()
                try:
                    url, expiry, title, artist, headers = await asyncio.wait_for(
                        run_in_threadpool(extract_youtube_audio, video_id, proxy), timeout=15
                    )
                    if expiry <= time.time():
                        raise ValueError("Expired audio URL")
                    async with httpx.AsyncClient(proxy=proxy, headers=headers, trust_env=False,
                                                 timeout=6) as client:
                        probe = await open_youtube_stream(client, url, "bytes=0-0")
                        try:
                            if probe.status_code not in (200, 206):
                                raise ValueError("Audio unavailable on this route")
                        finally:
                            await probe.aclose()
                except (DownloadError, TimeoutError, ValueError, httpx.HTTPError, HTTPException):
                    YOUTUBE_ROUTE_HEALTH[proxy] = (monotonic() + 60, 30)
                    continue
                YOUTUBE_ROUTE_HEALTH[proxy] = (0, monotonic() - started)
                source = YouTubeAudio(url, expiry, proxy, headers)
                if len(YOUTUBE_AUDIO_CACHE) >= 512:
                    YOUTUBE_AUDIO_CACHE.pop(next(iter(YOUTUBE_AUDIO_CACHE)))
                YOUTUBE_AUDIO_CACHE[video_id] = source
                if len(YOUTUBE_METADATA_CACHE) >= 512:
                    YOUTUBE_METADATA_CACHE.pop(next(iter(YOUTUBE_METADATA_CACHE)))
                YOUTUBE_METADATA_CACHE[video_id] = (title, artist)
                YOUTUBE_AUDIO_FAILURES.pop(video_id, None)
                return source
        detail = "Аудио YouTube недоступно на доступных маршрутах; пробуем встроенный плеер."
        YOUTUBE_AUDIO_FAILURES[video_id] = (monotonic() + 20, detail)
        if len(YOUTUBE_AUDIO_FAILURES) >= 512:
            YOUTUBE_AUDIO_FAILURES.pop(next(iter(YOUTUBE_AUDIO_FAILURES)))
        raise HTTPException(status_code=503, detail=detail)


@app.post("/api/youtube/audio/{video_id}")
async def prepare_youtube_audio(
    video_id: str, x_lobby_id: str = Header(""), x_host_token: str = Header("")
) -> dict[str, str]:
    lobby = LOBBIES.get(x_lobby_id)
    if not lobby or not secrets.compare_digest(x_host_token, lobby.host_token):
        raise HTTPException(status_code=403, detail="Доступно только хосту комнаты")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(status_code=400, detail="Некорректный Video ID")
    await youtube_audio_url(video_id)
    now = monotonic()
    for key, (_, expiry) in list(YOUTUBE_AUDIO_TICKETS.items()):
        if expiry <= now:
            YOUTUBE_AUDIO_TICKETS.pop(key, None)
    if len(YOUTUBE_AUDIO_TICKETS) >= 2048:
        YOUTUBE_AUDIO_TICKETS.pop(next(iter(YOUTUBE_AUDIO_TICKETS)))
    ticket = secrets.token_urlsafe(24)
    YOUTUBE_AUDIO_TICKETS[ticket] = (video_id, now + 7200)
    return {"url": f"/api/youtube/audio/file/{ticket}"}


@app.get("/api/youtube/audio/file/{ticket}")
async def stream_youtube_audio(ticket: str, request: Request) -> StreamingResponse:
    saved = YOUTUBE_AUDIO_TICKETS.get(ticket)
    if not saved or saved[1] <= monotonic():
        raise HTTPException(status_code=404, detail="Аудиоссылка истекла")
    video_id = saved[0]
    byte_range = request.headers.get("range")
    if byte_range and not re.fullmatch(r"bytes=\d*-\d*", byte_range):
        raise HTTPException(status_code=416, detail="Некорректный диапазон")
    source = await youtube_audio_url(video_id)
    client = httpx.AsyncClient(proxy=source.proxy, headers=source.headers, trust_env=False,
                               timeout=httpx.Timeout(15, read=60), follow_redirects=False)

    try:
        upstream = await open_youtube_stream(client, source.url, byte_range)
        if upstream.status_code in (403, 410, 429, 500, 502, 503, 504):
            await upstream.aclose()
            YOUTUBE_AUDIO_CACHE.pop(video_id, None)
            YOUTUBE_ROUTE_HEALTH[source.proxy] = (monotonic() + 60, 30)
            await client.aclose()
            source = await youtube_audio_url(video_id)
            client = httpx.AsyncClient(proxy=source.proxy, headers=source.headers, trust_env=False,
                                       timeout=httpx.Timeout(15, read=60), follow_redirects=False)
            upstream = await open_youtube_stream(client, source.url, byte_range)
        if upstream.status_code not in (200, 206):
            await upstream.aclose()
            raise HTTPException(status_code=502, detail="YouTube не отдал аудиопоток")
    except httpx.HTTPError:
        YOUTUBE_AUDIO_CACHE.pop(video_id, None)
        YOUTUBE_ROUTE_HEALTH[source.proxy] = (monotonic() + 60, 30)
        await client.aclose()
        raise HTTPException(
            status_code=502, detail="Не удалось подключиться к аудиосерверу YouTube"
        ) from None
    except HTTPException:
        await client.aclose()
        raise

    async def chunks():
        try:
            async for chunk in upstream.aiter_raw(16384):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {key: value for key in ("content-length", "content-range", "accept-ranges")
               if (value := upstream.headers.get(key))}
    headers["Cache-Control"] = "no-store"
    return StreamingResponse(
        chunks(), status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "audio/mp4"), headers=headers,
    )


@app.get("/api/youtube/metadata/{video_id}")
async def youtube_metadata(video_id: str) -> dict[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(status_code=400, detail="Некорректный Video ID")
    cached = YOUTUBE_METADATA_CACHE.get(video_id)
    if cached:
        return {"title": cached[0], "artist": cached[1]}
    try:
        data = None
        for proxy in settings.youtube_proxy_urls or [None]:
            try:
                async with httpx.AsyncClient(proxy=proxy, trust_env=False, timeout=5,
                                             follow_redirects=False) as client:
                    response = await client.get(
                        "https://www.youtube.com/oembed",
                        params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
                    )
                    response.raise_for_status()
                    data = response.json()
                break
            except (httpx.HTTPError, ValueError):
                continue
        if not isinstance(data, dict) or not data:
            raise ValueError("missing metadata")
        if data:
            title, artist = data.get("title"), data.get("author_name")
            if not isinstance(title, str) or not isinstance(artist, str):
                raise ValueError("missing metadata")
            if len(YOUTUBE_METADATA_CACHE) >= 512:
                YOUTUBE_METADATA_CACHE.pop(next(iter(YOUTUBE_METADATA_CACHE)))
            YOUTUBE_METADATA_CACHE[video_id] = (title[:150], artist[:100])
            return {"title": title[:150], "artist": artist[:100]}
    except (httpx.HTTPError, ValueError):
        if settings.youtube_api_key:
            try:
                async with httpx.AsyncClient(proxy=next(iter(settings.youtube_proxy_urls), None),
                                             trust_env=False, timeout=5) as client:
                    response = await client.get("https://www.googleapis.com/youtube/v3/videos",
                                                params={"part": "snippet", "id": video_id,
                                                        "key": settings.youtube_api_key})
                    response.raise_for_status()
                    items = response.json().get("items", [])
                    if items:
                        snippet = items[0]["snippet"]
                        return {"title": str(snippet["title"])[:150],
                                "artist": str(snippet["channelTitle"])[:100]}
            except (httpx.HTTPError, KeyError, ValueError):
                pass
        try:
            await youtube_audio_url(video_id)
            title, artist = YOUTUBE_METADATA_CACHE[video_id]
            return {"title": title, "artist": artist}
        except HTTPException:
            return {"title": f"YouTube · {video_id}", "artist": "Название пока недоступно"}


@dataclass
class DeviceLogin:
    code: str
    expires_at: float
    interval: int
    next_poll: float = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class YandexSession:
    client: Any
    expires_at: float


DEVICE_LOGINS: dict[str, DeviceLogin] = {}
YANDEX_SESSIONS: dict[str, YandexSession] = {}


def yandex_session(handle: str) -> Any:
    saved = YANDEX_SESSIONS.get(handle)
    if not saved or saved.expires_at <= monotonic():
        YANDEX_SESSIONS.pop(handle, None)
        raise HTTPException(status_code=401, detail="Подключите Яндекс Музыку заново")
    return saved.client


@app.post("/api/yandex/login")
async def start_yandex_login(request: Request) -> dict[str, str | int]:
    if await request.body():
        raise HTTPException(
            status_code=400, detail="Логин и пароль не требуются. Подтвердите вход кодом в Яндексе"
        )
    now = monotonic()
    for key, pending in list(DEVICE_LOGINS.items()):
        if pending.expires_at <= now:
            DEVICE_LOGINS.pop(key, None)
    if len(DEVICE_LOGINS) >= 100:
        raise HTTPException(status_code=429, detail="Слишком много попыток. Повторите позже")
    try:
        code = await asyncio.wait_for(ClientAsync().request_device_code(), timeout=10)
    except (YandexMusicError, TimeoutError):
        raise HTTPException(status_code=502, detail="Яндекс не выдал код входа") from None
    login_id = secrets.token_urlsafe(24)
    interval = max(1, code.interval)
    DEVICE_LOGINS[login_id] = DeviceLogin(code.device_code, monotonic() + code.expires_in, interval)
    return {
        "login_id": login_id,
        "verification_url": code.verification_url,
        "user_code": code.user_code,
        "expires_in": code.expires_in,
        "interval": interval,
    }


@app.get("/api/yandex/login/{login_id}")
async def check_yandex_login(login_id: str) -> dict[str, object]:
    pending = DEVICE_LOGINS.get(login_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Попытка входа не найдена")
    async with pending.lock:
        now = monotonic()
        if now >= pending.expires_at:
            DEVICE_LOGINS.pop(login_id, None)
            raise HTTPException(status_code=410, detail="Время подтверждения истекло")
        if now < pending.next_poll:
            return {"status": "pending", "retry_after": max(1, int(pending.next_poll - now) + 1)}
        pending.next_poll = now + pending.interval
        try:
            token = await asyncio.wait_for(
                ClientAsync().poll_device_token(pending.code), timeout=10
            )
        except DeviceAuthError:
            DEVICE_LOGINS.pop(login_id, None)
            raise HTTPException(
                status_code=400, detail="Яндекс отклонил вход или запросил новое подтверждение"
            ) from None
        except (YandexMusicError, TimeoutError):
            raise HTTPException(
                status_code=502, detail="Не удалось проверить подтверждение Яндекса"
            ) from None
        if token is None:
            return {"status": "pending", "retry_after": pending.interval}
        DEVICE_LOGINS.pop(login_id, None)
        client = await run_in_threadpool(lambda: Client(token.access_token).init(timeout=10))
        tracks = await run_in_threadpool(liked_page, client, 0)
        handle = secrets.token_urlsafe(32)
        lifetime = min(token.expires_in or 14400, 14400)
        YANDEX_SESSIONS[handle] = YandexSession(client, monotonic() + lifetime)
        return {
            "status": "authorized",
            "authenticated": tracks.authenticated,
            "tracks": tracks.tracks,
            "has_more": tracks.has_more,
            "next_offset": tracks.next_offset,
            "session": handle,
        }


class YandexToken(BaseModel):
    token: SecretStr = Field(min_length=1, max_length=8192)


class YandexTracksResponse(BaseModel):
    authenticated: bool = True
    tracks: list[dict[str, str]]
    has_more: bool = False
    next_offset: int = 0


def liked_page(client: Any, offset: int) -> YandexTracksResponse:
    likes = client.users_likes_tracks(timeout=10)
    liked = likes.tracks if likes and likes.tracks else []
    selected = liked[offset : offset + 50]
    tracks = client.tracks([track.track_id for track in selected], timeout=10) if selected else []
    return YandexTracksResponse(
        tracks=[
            {
                "id": str(track.id),
                "title": track.title or "Без названия",
                "artist": ", ".join(artist.name for artist in track.artists or []),
            }
            for track in tracks or []
            if track
        ],
        has_more=len(liked) > offset + 50,
        next_offset=offset + len(selected),
    )


@app.post("/api/yandex/tracks")
def yandex_tracks(body: YandexToken) -> YandexTracksResponse:
    token = body.token.get_secret_value().strip()
    if not token:
        raise HTTPException(status_code=400, detail="Укажите токен Яндекс Музыки")
    try:
        return liked_page(Client(token).init(timeout=10), 0)
    except UnauthorizedError:
        raise HTTPException(status_code=401, detail="Невалидный токен Яндекс Музыки") from None
    except TimedOutError:
        raise HTTPException(status_code=504, detail="Яндекс Музыка не ответила вовремя") from None
    except YandexMusicError:
        raise HTTPException(
            status_code=502, detail="Не удалось загрузить лайки Яндекс Музыки"
        ) from None
    except Exception:
        raise HTTPException(status_code=502, detail="Ошибка ответа Яндекс Музыки") from None


@app.get("/api/yandex/tracks")
def more_yandex_tracks(
    offset: int = 0, x_yandex_session: str = Header(default="")
) -> YandexTracksResponse:
    if offset < 0 or offset > 10000:
        raise HTTPException(status_code=400, detail="Некорректная страница")
    client = yandex_session(x_yandex_session)
    try:
        return liked_page(client, offset)
    except UnauthorizedError:
        raise HTTPException(status_code=401, detail="Подключите Яндекс Музыку заново") from None
    except YandexMusicError:
        raise HTTPException(status_code=502, detail="Не удалось загрузить лайки") from None


@app.get("/api/yandex/stream/{track_id}")
def yandex_stream(track_id: str, x_yandex_session: str = Header(default="")) -> dict[str, str]:
    if not re.fullmatch(r"[0-9]{1,20}", track_id):
        raise HTTPException(status_code=400, detail="Некорректный ID трека")
    client = yandex_session(x_yandex_session)
    try:
        tracks = client.tracks([track_id], timeout=10)
        if not tracks:
            raise HTTPException(status_code=404, detail="Трек не найден")
        variants = tracks[0].get_download_info(timeout=10)
        mp3 = [
            variant for variant in variants or [] if variant.codec == "mp3" and not variant.preview
        ]
        if not mp3:
            raise HTTPException(
                status_code=403,
                detail="Полный MP3 недоступен для этого аккаунта Яндекс Музыки",
            )
        selected = max(mp3, key=lambda variant: variant.bitrate_in_kbps)
        return {"url": selected.get_direct_link(timeout=10)}
    except HTTPException:
        raise
    except UnauthorizedError:
        raise HTTPException(status_code=401, detail="Подключите Яндекс Музыку заново") from None
    except YandexMusicError:
        raise HTTPException(status_code=502, detail="Не удалось получить аудиоссылку") from None
    except Exception:
        raise HTTPException(status_code=502, detail="Ошибка аудиоссылки Яндекса") from None


@app.get("/api/yandex/queue/{lobby_id}/{queue_item_id}/stream")
def yandex_queue_stream(
    lobby_id: str, queue_item_id: str, x_host_token: str = Header(default="")
) -> dict[str, str]:
    lobby = LOBBIES.get(lobby_id)
    if lobby is None:
        raise HTTPException(status_code=404, detail="Комната не найдена")
    if not secrets.compare_digest(x_host_token, lobby.host_token):
        raise HTTPException(status_code=403, detail="Доступно только хосту")
    item = next((entry for entry in lobby.queue_items if entry.id == queue_item_id), None)
    if item is None or not item.track_id.startswith("yandex_"):
        raise HTTPException(status_code=404, detail="Яндекс-трек не найден в очереди")
    if not item.yandex_session:
        raise HTTPException(status_code=409, detail="Автору трека нужно подключить Яндекс Музыку")
    try:
        return yandex_stream(item.track_id.removeprefix("yandex_"), item.yandex_session)
    except HTTPException as exc:
        if exc.status_code == 401:
            raise HTTPException(
                status_code=401, detail="Автору трека нужно заново подключить Яндекс Музыку"
            ) from None
        raise
