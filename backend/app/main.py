import asyncio
import hashlib
from collections import OrderedDict
import logging
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
from app.api.lobby import LOBBIES, listener_lobby, telegram_polling_loop, load_rooms, room_maintenance, save_rooms
from app.api.lobby import router as lobby_router
from app.providers.music import router as music_router
from app.settings import MAX_TRACK_SECONDS, Settings

settings = Settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_rooms()
    maintenance = asyncio.create_task(room_maintenance())
    task = (asyncio.create_task(telegram_polling_loop())
            if settings.telegram_polling_enabled and settings.telegram_bot_token else None)
    try:
        yield
    finally:
        maintenance.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance
        await save_rooms()
        unfinished = [entry[1] for entry in LISTENER_STREAMS.values() if not entry[1].done()] + list(MEDIA_PENDING.values())
        for pending in unfinished:
            pending.cancel()
        await asyncio.gather(*unfinished, return_exceptions=True)
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
app.include_router(music_router)

@dataclass(frozen=True)
class YouTubeAudio:
    url: str
    expires_at: float
    proxy: str | None
    headers: dict[str, str] = field(default_factory=dict)
    duration: float = 0


YOUTUBE_AUDIO_CACHE: dict[str, YouTubeAudio] = {}
YOUTUBE_METADATA_CACHE: dict[str, tuple[str, str]] = {}
YOUTUBE_DURATION_CACHE: dict[str, float] = {}
YOUTUBE_AUDIO_TICKETS: dict[str, tuple[str, float]] = {}
YOUTUBE_EXTRACT_LIMIT = asyncio.Semaphore(2)
YOUTUBE_AUDIO_FAILURES: dict[str, tuple[float, str]] = {}
YOUTUBE_AUDIO_LOCKS: dict[str, asyncio.Lock] = {}
YOUTUBE_ROUTE_HEALTH: dict[str | None, tuple[float, float]] = {}
LISTENER_STREAMS: dict[tuple[str, str, int], tuple[float, asyncio.Task[dict[str, Any]]]] = {}


MEDIA_PARTS: OrderedDict[tuple[str, str], tuple[float, tuple[int, dict[str, str], bytes]]] = OrderedDict()
MEDIA_PENDING: dict[tuple[str, str], asyncio.Task] = {}
MEDIA_BYTES = 0
MEDIA_BUDGET = 64 * 1024 * 1024
MEDIA_BLOCK = 256 * 1024
MEDIA_LOAD_LIMIT = asyncio.Semaphore(4)


async def bounded_media_body(response: httpx.Response, limit: int) -> bytes:
    parts, size = [], 0
    async for part in response.aiter_bytes(16384):
        size += len(part)
        if size > limit:
            raise HTTPException(502, "Слишком большой фрагмент аудио")
        parts.append(part)
    return b"".join(parts)


async def media_part(key: tuple[str, str], load):
    global MEDIA_BYTES
    now = monotonic()
    for old_key, (expiry, entry) in list(MEDIA_PARTS.items()):
        if expiry <= now:
            MEDIA_BYTES -= len(entry[2])
            MEDIA_PARTS.pop(old_key, None)
    cached = MEDIA_PARTS.get(key)
    if cached:
        MEDIA_PARTS.move_to_end(key)
        return cached[1]
    pending = MEDIA_PENDING.get(key)
    if pending is None:
        if len(MEDIA_PENDING) >= 32:
            raise HTTPException(503, "Аудиобуфер занят. Повторите запрос.")

        async def fill():
            global MEDIA_BYTES
            try:
                async with MEDIA_LOAD_LIMIT:
                    value = await load()
                size = len(value[2])
                while MEDIA_PARTS and (MEDIA_BYTES + size > MEDIA_BUDGET or len(MEDIA_PARTS) >= 1024):
                    _, (_, discarded) = MEDIA_PARTS.popitem(last=False)
                    MEDIA_BYTES -= len(discarded[2])
                if size <= MEDIA_BUDGET:
                    MEDIA_PARTS[key] = (monotonic() + 600, value)
                    MEDIA_BYTES += size
                return value
            finally:
                MEDIA_PENDING.pop(key, None)

        pending = asyncio.create_task(fill())
        MEDIA_PENDING[key] = pending
        pending.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
    return await asyncio.shield(pending)


async def shared_audio_response(source_key: str, byte_range: str | None, fetch_part) -> StreamingResponse:
    async def metadata():
        status, headers, _ = await fetch_part("bytes=0-0", MEDIA_BLOCK)
        match = re.fullmatch(r"bytes \d+-\d+/(\d+)", headers.get("content-range", ""))
        if status != 206 or not match or not 0 < int(match[1]) <= 64 * 1024 * 1024:
            raise HTTPException(502, "Источник не поддерживает безопасную буферизацию аудио")
        return status, headers, b""

    _, info, _ = await media_part((source_key, "metadata"), metadata)
    total = int(info["content-range"].rsplit("/", 1)[1])
    start, end = 0, total - 1
    if byte_range:
        match = re.fullmatch(r"bytes=(\d{0,12})-(\d{0,12})", byte_range)
        if not match or not any(match.groups()):
            raise HTTPException(416, "Некорректный диапазон", headers={"Content-Range": f"bytes */{total}"})
        if match[1]:
            start, end = int(match[1]), min(int(match[2]), total - 1) if match[2] else total - 1
        else:
            start = max(0, total - int(match[2]))
        if start > end or start >= total:
            raise HTTPException(416, "Диапазон за пределами трека", headers={"Content-Range": f"bytes */{total}"})

    async def chunks():
        position = start
        while position <= end:
            block = position // MEDIA_BLOCK * MEDIA_BLOCK
            last = min(total - 1, block + MEDIA_BLOCK - 1)

            async def load(block=block, last=last):
                status, headers, body = await fetch_part(f"bytes={block}-{last}", MEDIA_BLOCK)
                expected = f"bytes {block}-{last}/{total}"
                if status != 206 or headers.get("content-range") != expected or len(body) != last - block + 1:
                    raise HTTPException(502, "Источник вернул неполный диапазон аудио")
                return status, headers, body

            _, _, body = await media_part((source_key, str(block)), load)
            stop = min(end + 1, last + 1)
            yield body[position - block:stop - block]
            position = stop

    headers = {"Content-Length": str(end - start + 1), "Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    if byte_range:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    return StreamingResponse(chunks(), status_code=206 if byte_range else 200,
                             media_type=info.get("content-type", "audio/mp4"), headers=headers)


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
    duration = info.get("duration") if isinstance(info, dict) else None
    if isinstance(info, dict) and (info.get("is_live") or info.get("live_status") == "is_live"):
        raise HTTPException(422, "Прямые эфиры нельзя добавлять в музыкальную очередь")
    if type(duration) in (int, float) and duration > MAX_TRACK_SECONDS:
        if len(YOUTUBE_DURATION_CACHE) >= 512:
            YOUTUBE_DURATION_CACHE.pop(next(iter(YOUTUBE_DURATION_CACHE)))
        YOUTUBE_DURATION_CACHE[video_id] = float(duration)
        raise HTTPException(422, "Трек длиннее 12 минут — выберите более короткую запись")
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
    duration = info.get("duration")
    if type(duration) in (int, float) and 0 < duration <= 86400:
        if len(YOUTUBE_DURATION_CACHE) >= 512:
            YOUTUBE_DURATION_CACHE.pop(next(iter(YOUTUBE_DURATION_CACHE)))
        YOUTUBE_DURATION_CACHE[video_id] = float(duration)
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
                except HTTPException as exc:
                    if exc.status_code == 422:
                        raise
                    YOUTUBE_ROUTE_HEALTH[proxy] = (monotonic() + 60, 30)
                    continue
                except (DownloadError, TimeoutError, ValueError, httpx.HTTPError):
                    YOUTUBE_ROUTE_HEALTH[proxy] = (monotonic() + 60, 30)
                    continue
                YOUTUBE_ROUTE_HEALTH[proxy] = (0, monotonic() - started)
                source = YouTubeAudio(url, expiry, proxy, headers,
                                      YOUTUBE_DURATION_CACHE.get(video_id, 0))
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
) -> dict[str, Any]:
    lobby = LOBBIES.get(x_lobby_id)
    if not lobby or not secrets.compare_digest(x_host_token, lobby.host_token):
        raise HTTPException(status_code=403, detail="Доступно только хосту комнаты")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(status_code=400, detail="Некорректный Video ID")
    source = await youtube_audio_url(video_id)
    if source.duration > MAX_TRACK_SECONDS:
        raise HTTPException(422, "Трек длиннее 12 минут")
    now = monotonic()
    for key, (_, expiry) in list(YOUTUBE_AUDIO_TICKETS.items()):
        if expiry <= now:
            YOUTUBE_AUDIO_TICKETS.pop(key, None)
    if len(YOUTUBE_AUDIO_TICKETS) >= 2048:
        YOUTUBE_AUDIO_TICKETS.pop(next(iter(YOUTUBE_AUDIO_TICKETS)))
    ticket = secrets.token_urlsafe(24)
    YOUTUBE_AUDIO_TICKETS[ticket] = (video_id, now + 7200)
    return {"url": f"/api/youtube/audio/file/{ticket}",
            "duration": source.duration or YOUTUBE_DURATION_CACHE.get(video_id),
            "max_duration": MAX_TRACK_SECONDS}


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
    if source.duration > MAX_TRACK_SECONDS:
        raise HTTPException(422, "Трек длиннее 12 минут")
    if settings.server_playback_enabled:
        async def fetch_part(part_range: str, limit: int):
            async with httpx.AsyncClient(proxy=source.proxy, headers=source.headers, trust_env=False,
                                         timeout=httpx.Timeout(15, read=30), follow_redirects=False) as shared_client:
                response = await open_youtube_stream(shared_client, source.url, part_range)
                try:
                    if response.status_code not in (200, 206):
                        YOUTUBE_AUDIO_CACHE.pop(video_id, None)
                        raise HTTPException(502, "Аудиосервер YouTube временно недоступен")
                    return response.status_code, dict(response.headers), await bounded_media_body(response, limit)
                finally:
                    await response.aclose()
        key = "youtube:" + hashlib.sha256(source.url.encode()).hexdigest()
        return await shared_audio_response(key, byte_range, fetch_part)
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
        try:
            client = await run_in_threadpool(lambda: Client(token.access_token).init(timeout=10))
        except UnauthorizedError:
            raise HTTPException(status_code=401, detail="Яндекс не подтвердил доступ к аккаунту") from None
        except YandexMusicError:
            raise HTTPException(status_code=502, detail="Не удалось открыть аккаунт Яндекса. Повторите вход") from None
        handle = secrets.token_urlsafe(32)
        lifetime = min(token.expires_in or 14400, 14400)
        YANDEX_SESSIONS[handle] = YandexSession(client, monotonic() + lifetime)
        library_error = ""
        try:
            tracks = await run_in_threadpool(liked_page, client, 0)
        except (YandexMusicError, ValueError, TypeError, AttributeError):
            tracks = YandexTracksResponse(tracks=[])
            library_error = "Аккаунт подключён, но список лайков не загрузился. Нажмите «Обновить лайки»"
            logging.getLogger("uvicorn.error").warning("Yandex authorization succeeded; likes unavailable")
        return {
            "status": "authorized",
            "authenticated": tracks.authenticated,
            "tracks": tracks.tracks,
            "has_more": tracks.has_more,
            "next_offset": tracks.next_offset,
            "session": handle,
            "library_error": library_error,
            "unsupported_count": tracks.unsupported_count,
        }


class YandexToken(BaseModel):
    token: SecretStr = Field(min_length=1, max_length=8192)


class YandexTrackResponse(BaseModel):
    id: str
    title: str
    artist: str
    playable: bool = True
    reason: str = ""
    duration_ms: int = 0


class YandexTracksResponse(BaseModel):
    authenticated: bool = True
    tracks: list[YandexTrackResponse]
    has_more: bool = False
    next_offset: int = 0
    unsupported_count: int = 0


def liked_page(client: Any, offset: int) -> YandexTracksResponse:
    cached = getattr(client, "_prada_liked_ids", None)
    if offset and cached and cached[0] > monotonic():
        liked = cached[1]
    else:
        likes = client.users_likes_tracks(timeout=10)
        liked = [track.track_id for track in likes.tracks] if likes and likes.tracks else []
        client._prada_liked_ids = (monotonic() + 120, liked)
    selected = liked[offset : offset + 50]
    tracks = client.tracks(selected, timeout=10) if selected else []
    normalized = []
    missing = 0
    for track in tracks or []:
        if not track:
            missing += 1
            continue
        raw_id = str(getattr(track, "id", "") or "")
        if not raw_id or len(raw_id) > 128:
            missing += 1
            continue
        catalog_id = re.fullmatch(r"([0-9]{1,20})(?::[0-9]{1,20})?", raw_id)
        playable = catalog_id is not None
        duration_ms = getattr(track, "duration_ms", 0) or 0
        too_long = type(duration_ms) is int and duration_ms > MAX_TRACK_SECONDS * 1000
        playable = playable and not too_long
        metadata = getattr(track, "meta_data", None)
        title = getattr(track, "title", None) or getattr(metadata, "title", None)
        artists = [getattr(artist, "name", None) for artist in getattr(track, "artists", None) or []]
        artist = ", ".join(name.strip() for name in artists if isinstance(name, str) and name.strip())
        fallback_artist = getattr(metadata, "artist", None)
        normalized.append(YandexTrackResponse(
            id=catalog_id[1] if catalog_id else raw_id,
            title=title.strip() if isinstance(title, str) and title.strip() else "Без названия",
            artist=artist or (fallback_artist.strip() if isinstance(fallback_artist, str) and fallback_artist.strip() else "Неизвестный исполнитель"),
            playable=playable,
            reason="Трек длиннее 12 минут" if too_long else "" if playable else
            "Этот тип записи Яндекса пока не поддерживается плеером",
            duration_ms=duration_ms if type(duration_ms) is int else 0,
        ))
    unsupported = sum(not track.playable for track in normalized) + missing
    if unsupported:
        logging.getLogger("uvicorn.error").warning(
            "Yandex likes normalized: loaded=%d unsupported=%d missing=%d",
            len(normalized), unsupported - missing, missing,
        )
    return YandexTracksResponse(
        tracks=normalized,
        has_more=len(liked) > offset + 50,
        next_offset=offset + len(selected),
        unsupported_count=unsupported,
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
    if offset < 0 or offset > 100000:
        raise HTTPException(status_code=400, detail="Некорректная страница")
    client = yandex_session(x_yandex_session)
    try:
        return liked_page(client, offset)
    except UnauthorizedError:
        raise HTTPException(status_code=401, detail="Подключите Яндекс Музыку заново") from None
    except YandexMusicError:
        raise HTTPException(status_code=502, detail="Не удалось загрузить лайки") from None


@app.get("/api/yandex/stream/{track_id}")
def yandex_stream(track_id: str, x_yandex_session: str = Header(default="")) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9]{1,20}", track_id):
        raise HTTPException(status_code=400, detail="Некорректный ID трека")
    client = yandex_session(x_yandex_session)
    try:
        tracks = client.tracks([track_id], timeout=10)
        if not tracks:
            raise HTTPException(status_code=404, detail="Трек не найден")
        duration = getattr(tracks[0], "duration_ms", 0)
        if type(duration) is int and duration > MAX_TRACK_SECONDS * 1000:
            raise HTTPException(422, "Трек длиннее 12 минут")
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
        return {"url": selected.get_direct_link(timeout=10),
                **({"duration": duration / 1000} if type(duration) is int and duration > 0 else {})}
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
) -> dict[str, Any]:
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


async def room_stream_descriptor(lobby_id: str, item_id: str) -> dict[str, Any]:
    lobby = LOBBIES.get(lobby_id)
    if lobby is None:
        raise HTTPException(404, "Комната не найдена")
    item = next((entry for entry in lobby.queue_items[:2] if entry.id == item_id), None)
    if item is None:
        raise HTTPException(404, "Доступны только текущий и следующий треки комнаты")
    key = (lobby_id, item.id, item.access_revision)
    now = monotonic()
    for cached_key, (expiry, task) in list(LISTENER_STREAMS.items()):
        owner = LOBBIES.get(cached_key[0])
        needed = owner is not None and any(entry.id == cached_key[1] for entry in owner.queue_items[:2])
        if task.done() and (expiry < now or not needed):
            del LISTENER_STREAMS[cached_key]
    cached = LISTENER_STREAMS.get(key)
    if cached is None:
        if len(LISTENER_STREAMS) >= 256:
            raise HTTPException(503, "Подготовка музыки занята; повторите через несколько секунд")

        async def resolve() -> dict[str, Any]:
            if item.track_id.startswith("yandex_"):
                return await run_in_threadpool(
                    yandex_queue_stream, lobby_id, item_id, lobby.host_token
                )
            if item.track_id.startswith(("sc_", "vk_")):
                from app.providers.music import queue_stream
                data = await queue_stream(lobby_id, item_id, lobby.host_token)
                return {**data, "duration": data.get("duration") or item.duration_ms / 1000}
            if item.track_id.startswith(("spotify_", "apple_")):
                raise HTTPException(422, "Оригинал воспроизводится в личном браузерном плеере сервиса")
            return await prepare_youtube_audio(item.track_id, lobby_id, lobby.host_token)

        cached = (now + 600, asyncio.create_task(resolve()))
        LISTENER_STREAMS[key] = cached
    try:
        result = await asyncio.shield(cached[1])
    except Exception:
        if LISTENER_STREAMS.get(key) is cached:
            LISTENER_STREAMS.pop(key, None)
        raise
    if not any(entry.id == item_id for entry in lobby.queue_items[:2]):
        raise HTTPException(409, "Очередь уже переключилась; обновляем плеер")
    return result


@app.get("/api/room/{lobby_id}/{item_id}/stream")
async def room_host_stream(lobby_id: str, item_id: str, x_host_token: str = Header(default="")) -> dict[str, Any]:
    lobby = LOBBIES.get(lobby_id)
    if not lobby or not secrets.compare_digest(x_host_token, lobby.host_token):
        raise HTTPException(403, "Доступно только хосту комнаты")
    result = await room_stream_descriptor(lobby_id, item_id)
    if LOBBIES.get(lobby_id) is not lobby or not secrets.compare_digest(x_host_token, lobby.host_token):
        raise HTTPException(403, "Права хоста изменились")
    return result


@app.get("/api/listen/{lobby_id}/{item_id}/stream")
async def listener_stream(lobby_id: str, item_id: str, x_listener_token: str = Header(default="")) -> dict[str, Any]:
    listener_lobby(lobby_id, x_listener_token)
    result = await room_stream_descriptor(lobby_id, item_id)
    listener_lobby(lobby_id, x_listener_token)
    return result
