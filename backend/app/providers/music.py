"""Small, in-memory personal music imports. Credentials never enter lobby events."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import httpx
import yt_dlp  # type: ignore[import-untyped]
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, SecretStr

from app.settings import Settings

router = APIRouter(prefix="/api/music", tags=["music"])
settings = Settings()
SESSION_TTL = 14 * 3600
LOGIN_TTL = 600
MAX_SESSIONS = 512
MAX_TRACKS = 500
PAGE_SIZE = 50
SPOTIFY_API = "https://api.spotify.com/v1"
SC_API = "https://api.soundcloud.com"
VK_API = "https://api.vk.ru/method"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass
class PendingLogin:
    provider: str
    state: str
    verifier: str
    redirect_uri: str
    browser_nonce: str
    expires: float


@dataclass
class MusicSession:
    provider: str
    access_token: str
    refresh_token: str
    expires: float
    lifetime: float
    imported: dict[str, dict[str, object]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    public_url: str = ""
    last_resolve: float = 0
    secret_tokens: dict[str, str] = field(default_factory=dict)
    page_urls: dict[str, str] = field(default_factory=dict)


PENDING: dict[str, PendingLogin] = {}
SESSIONS: dict[str, MusicSession] = {}
RESOLVE_CACHE: dict[str, dict[str, str]] = {}
ROUTE_FAILURES: dict[str | None, float] = {}


@dataclass
class AudioTicket:
    source: str
    proxy: str | None
    expires: float
    children: dict[str, str] = field(default_factory=dict)


TICKETS: dict[str, AudioTicket] = {}
_extract_limit = asyncio.Semaphore(1)


class QuietMusicLog:
    def debug(self, message: str) -> None:
        pass

    warning = error = debug


class LoginBody(BaseModel):
    redirect_uri: str


class CallbackBody(BaseModel):
    login_id: str
    code: str
    state: str


class TokenBody(BaseModel):
    token: SecretStr


class PublicBody(BaseModel):
    url: str
    session: str = ""
    cursor: str = ""


class ResolveBody(BaseModel):
    session: str
    track_id: str


def _value(name: str, default: object = "") -> object:
    return getattr(settings, name, default)


def _credentials(provider: str) -> tuple[str, str]:
    if provider == "spotify":
        return str(_value("spotify_client_id")), str(_value("spotify_client_secret"))
    if provider == "soundcloud":
        return str(_value("soundcloud_client_id")), str(_value("soundcloud_client_secret"))
    return "", ""


def _proxy() -> str | None:
    proxies = _value("music_proxy_urls", []) or _value("youtube_proxy_urls", [])
    return proxies[0] if isinstance(proxies, list) and proxies else None


def _client(proxy: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        proxy=proxy, trust_env=False, timeout=httpx.Timeout(15, connect=8), follow_redirects=False
    )


def _safe_id(value: object) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise HTTPException(400, "Некорректный ID трека")
    return value


def _provider(provider: str) -> None:
    if provider not in {"spotify", "soundcloud", "vk"}:
        raise HTTPException(404, "Неизвестный провайдер")


def _redirect(provider: str, uri: str) -> None:
    try:
        parsed = urlparse(uri)
    except ValueError:
        raise HTTPException(400, "Недопустимый адрес возврата") from None
    allowed = set(settings.cors_origins)
    allowed.add(str(_value("music_public_origin", "https://prada-sound.space")))
    try:
        valid = (
            f"{parsed.scheme}://{parsed.netloc}" in allowed
            and parsed.path == f"/oauth/{provider}"
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
            and not parsed.username
            and not parsed.password
            and (parsed.port is None or parsed.port > 0)
        )
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(400, "Недопустимый адрес возврата")


def _check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin not in set(settings.cors_origins) | {
        str(_value("music_public_origin", "https://prada-sound.space"))
    }:
        raise HTTPException(403, "Недопустимый Origin")


def _clean() -> None:
    now = monotonic()
    for key, pending in list(PENDING.items()):
        if pending.expires < now:
            del PENDING[key]
    for key, session in list(SESSIONS.items()):
        if session.lifetime < now:
            del SESSIONS[key]
    for key, ticket in list(TICKETS.items()):
        if ticket.expires < now:
            del TICKETS[key]


def _session(handle: str, provider: str | None = None) -> MusicSession:
    _clean()
    session = SESSIONS.get(handle)
    if (
        not session
        or (provider is not None and session.provider != provider)
        or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", handle)
    ):
        raise HTTPException(401, "Подключите музыкальный аккаунт снова")
    return session


async def _call(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.HTTPError:
        raise HTTPException(502, "Музыкальный сервис временно недоступен") from None
    if response.status_code == 429:
        raise HTTPException(
            429,
            "Лимит музыкального сервиса; попробуйте позже",
            headers={"Retry-After": response.headers.get("Retry-After", "10")},
        )
    if response.status_code in (401, 403):
        raise HTTPException(403, "Нет доступа к музыкальной библиотеке")
    if response.status_code >= 400 or len(response.content) > 2_000_000:
        raise HTTPException(502, "Музыкальный сервис отклонил запрос")
    try:
        body = response.json()
    except ValueError:
        raise HTTPException(502, "Некорректный ответ музыкального сервиса") from None
    if not isinstance(body, dict):
        raise HTTPException(502, "Некорректный ответ музыкального сервиса")
    return body


async def _refresh(session: MusicSession) -> None:
    if session.provider == "vk" or session.expires > monotonic() + 60:
        return
    async with session.lock:
        if session.expires > monotonic() + 60:
            return
        client_id, client_secret = _credentials(session.provider)
        if not session.refresh_token or not client_id or not client_secret:
            raise HTTPException(401, "Подключите музыкальный аккаунт снова")
        url = (
            "https://accounts.spotify.com/api/token"
            if session.provider == "spotify"
            else "https://secure.soundcloud.com/oauth/token"
        )
        data = {
            "grant_type": "refresh_token",
            "refresh_token": session.refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        async with _client(_proxy()) as client:
            body = await _call(client, "POST", url, data=data)
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise HTTPException(401, "Подключите музыкальный аккаунт снова")
        session.access_token = token
        if isinstance(body.get("refresh_token"), str):
            session.refresh_token = body["refresh_token"]
        session.expires = monotonic() + int(body.get("expires_in", 3600))


def _store(provider: str, access: str, refresh: str = "", expires_in: int = 3600) -> str:
    _clean()
    if len(SESSIONS) >= MAX_SESSIONS:
        raise HTTPException(503, "Слишком много музыкальных подключений")
    handle = secrets.token_urlsafe(32)
    SESSIONS[handle] = MusicSession(
        provider, access, refresh, monotonic() + expires_in, monotonic() + SESSION_TTL
    )
    return handle


def _track(provider: str, raw: dict[str, Any]) -> dict[str, object] | None:
    if provider == "spotify":
        identifier = raw.get("id")
        artists = raw.get("artists") or []
        artist = ", ".join(a.get("name", "") for a in artists if isinstance(a, dict))
        title = raw.get("name")
        canonical = f"https://open.spotify.com/track/{identifier}"
        playable = not raw.get("is_local", False)
        reason = (
            "Для Spotify воспроизведение через найденное видео YouTube"
            if playable
            else "Локальный трек Spotify недоступен"
        )
    elif provider == "soundcloud":
        urn = str(raw.get("urn", ""))
        identifier = (
            urn.removeprefix("soundcloud:tracks:")
            if urn.startswith("soundcloud:tracks:")
            else str(raw.get("id", ""))
        )
        artist = raw.get("metadata_artist") or (raw.get("user") or {}).get("username", "")
        title = raw.get("title")
        canonical = raw.get("permalink_url", "")
        playable = (
            raw.get("streamable", True) is not False and raw.get("access", "playable") == "playable"
        )
        reason = "" if playable else "Трек SoundCloud недоступен для воспроизведения"
    else:
        identifier = f"{raw.get('owner_id')}_{raw.get('id')}"
        artist = raw.get("artist", "")
        title = raw.get("title")
        canonical = f"https://vk.com/audio{identifier}"
        playable = bool(raw.get("url"))
        reason = "" if playable else "VK не предоставил прямую аудиоссылку"
    if not isinstance(identifier, str):
        return None
    valid_id = (
        re.fullmatch(r"-?\d{1,20}_\d{1,20}", identifier)
        if provider == "vk"
        else ID_RE.fullmatch(identifier)
    )
    if (
        not valid_id
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(artist, str)
        or not isinstance(canonical, str)
    ):
        return None
    if provider == "soundcloud" and not _safe_provider_url(canonical, "soundcloud"):
        return None
    return {
        "id": identifier,
        "title": title[:200],
        "artist": artist[:200],
        "canonical_url": canonical,
        "playable": playable,
        "reason": reason,
    }


def _safe_provider_url(url: str, provider: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    hosts = {
        "spotify": {"open.spotify.com"},
        "soundcloud": {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"},
        "vk": {"vk.com"},
    }
    try:
        return (
            parsed.scheme == "https"
            and parsed.hostname in hosts[provider]
            and not parsed.username
            and not parsed.password
            and parsed.port is None
            and not parsed.fragment
            and len(url) <= 2048
        )
    except ValueError:
        return False


def _safe_media_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port is None
            and len(url) <= 4096
            and any(
                host == name or host.endswith("." + name)
                for name in ("sndcdn.com", "soundcloud.cloud", "soundcloud.com")
            )
        )
    except ValueError:
        return False


def _extract(
    url: str, *, flat: bool = False, offset: int = 0, proxy: str | None = None
) -> dict[str, Any]:
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": not flat,
        "extract_flat": "in_playlist" if flat else False,
        "playliststart": offset + 1,
        "playlistend": offset + PAGE_SIZE,
        "socket_timeout": 10,
        "retries": 0,
        "cachedir": "/tmp/prada-youtube",
        "proxy": proxy or _proxy() or "",
        "logger": QuietMusicLog(),
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        result = ydl.extract_info(url, download=False)
    if not isinstance(result, dict):
        raise ValueError("empty metadata")
    return result


async def _extract_async(url: str, *, flat: bool = False, offset: int = 0) -> dict[str, Any]:
    async with _extract_limit:
        routes: list[str | None] = list(settings.music_proxy_urls or settings.youtube_proxy_urls)
        if not routes:
            routes = [None]
        routes = sorted(routes, key=lambda route: ROUTE_FAILURES.get(route, 0) > monotonic())
        for route in routes:
            try:
                info = await asyncio.wait_for(
                    asyncio.to_thread(_extract, url, flat=flat, offset=offset, proxy=route),
                    timeout=20,
                )
                info["_prada_proxy"] = route
                ROUTE_FAILURES.pop(route, None)
                return info
            except Exception:
                ROUTE_FAILURES[route] = monotonic() + 60
        raise HTTPException(502, "Не удалось получить данные SoundCloud") from None


def validate_track(provider: str, handle: str, track_id: str) -> dict[str, object]:
    """Validate a queue append against server-side imported metadata."""
    _provider(provider)
    session = _session(handle, provider)
    if provider == "vk":
        if not re.fullmatch(r"-?\d{1,20}_\d{1,20}", track_id):
            raise HTTPException(400, "Некорректный ID трека")
    else:
        _safe_id(track_id)
    item = session.imported.get(track_id)
    if item is None or not item["playable"]:
        raise HTTPException(403, "Трек не импортирован или недоступен")
    return item.copy()


@router.get("/providers")
async def providers() -> dict[str, Any]:
    result = {}
    for provider in ("spotify", "soundcloud", "vk"):
        client_id, client_secret = _credentials(provider)
        enabled = (
            bool(client_id and client_secret)
            if provider != "vk"
            else bool(_value("vk_audio_enabled", False))
        )
        reason = (
            "Готово к подключению"
            if enabled
            else "Нужны ключи приложения Spotify"
            if provider == "spotify"
            else "Нужны ключи приложения SoundCloud"
            if provider == "soundcloud"
            else "VK Audio API доступен только при отдельном разрешении"
        )
        result[provider] = {
            "oauth": enabled and provider != "vk",
            "public_import": provider == "soundcloud",
            "experimental_token": enabled and provider == "vk",
            "reason": reason,
        }
    return {"providers": result}


@router.post("/vk/login")
async def vk_login(body: TokenBody, request: Request) -> dict[str, Any]:
    _check_origin(request)
    if not _value("vk_audio_enabled", False):
        raise HTTPException(503, "VK Audio API не включён: обычный VK ID не даёт доступ к аудио")
    token = body.token.get_secret_value()
    if len(token) < 20 or len(token) > 2048:
        raise HTTPException(400, "Некорректный токен VK")
    async with _client() as client:
        data = await _call(
            client,
            "POST",
            f"{VK_API}/audio.get",
            data={"access_token": token, "v": "5.199", "count": 1},
        )
    _vk_error(data)
    handle = _store("vk", token, expires_in=SESSION_TTL)
    return await tracks("vk", request=request, x_music_session=handle)


@router.post("/{provider}/login")
async def login(
    provider: str, body: LoginBody, request: Request, response: Response
) -> dict[str, Any]:
    _check_origin(request)
    _provider(provider)
    if provider == "vk":
        raise HTTPException(400, "VK использует отдельное подключение токеном")
    client_id, client_secret = _credentials(provider)
    if not client_id or not client_secret:
        raise HTTPException(503, f"{provider.title()} пока не подключён: нужны ключи приложения")
    _redirect(provider, body.redirect_uri)
    if len(PENDING) >= 512:
        _clean()
    if len(PENDING) >= 512:
        raise HTTPException(503, "Слишком много ожидающих подключений")
    login_id = secrets.token_urlsafe(24)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    browser_nonce = secrets.token_urlsafe(32)
    PENDING[login_id] = PendingLogin(
        provider, state, verifier, body.redirect_uri, browser_nonce, monotonic() + LOGIN_TTL
    )
    if provider == "spotify":
        url = "https://accounts.spotify.com/authorize"
        query = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": body.redirect_uri,
            "state": state,
            "scope": "user-library-read",
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    else:
        url = "https://secure.soundcloud.com/authorize"
        query = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": body.redirect_uri,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    response.set_cookie(
        "music_login",
        browser_nonce,
        max_age=LOGIN_TTL,
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="lax",
        path="/api/music",
    )
    return {
        "login_id": login_id,
        "authorization_url": f"{url}?{urlencode(query)}",
        "expire_in": LOGIN_TTL,
    }


@router.post("/{provider}/callback")
async def callback(
    provider: str, body: CallbackBody, request: Request, response: Response
) -> dict[str, Any]:
    _check_origin(request)
    _provider(provider)
    pending = PENDING.pop(body.login_id, None)
    cookie = request.cookies.get("music_login", "")
    response.delete_cookie("music_login", path="/api/music")
    if (
        not pending
        or pending.provider != provider
        or pending.expires <= monotonic()
        or not hmac.compare_digest(pending.state, body.state)
        or not hmac.compare_digest(pending.browser_nonce, cookie)
        or not body.code
        or len(body.code) > 2048
    ):
        raise HTTPException(400, "Подключение истекло или не совпадает; начните снова")
    client_id, client_secret = _credentials(provider)
    url = (
        "https://accounts.spotify.com/api/token"
        if provider == "spotify"
        else "https://secure.soundcloud.com/oauth/token"
    )
    data = {
        "grant_type": "authorization_code",
        "code": body.code,
        "redirect_uri": pending.redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": pending.verifier,
    }
    async with _client(_proxy()) as client:
        token = await _call(client, "POST", url, data=data)
    access = token.get("access_token")
    if not isinstance(access, str) or not access:
        raise HTTPException(502, "Музыкальный сервис не выдал доступ")
    handle = _store(
        provider, access, str(token.get("refresh_token") or ""), int(token.get("expires_in", 3600))
    )
    return await tracks(provider, request=request, x_music_session=handle)


def _vk_error(body: dict[str, Any]) -> None:
    if "error" in body:
        code = body["error"].get("error_code") if isinstance(body["error"], dict) else None
        if code in {15, 200, 201}:
            raise HTTPException(403, "VK ограничил доступ к аудио для этого приложения")
        raise HTTPException(403, "VK не предоставил доступ к музыкальной библиотеке")


@router.get("/{provider}/tracks")
async def tracks(
    provider: str, request: Request, cursor: str = "", x_music_session: str = Header(default="")
) -> dict[str, Any]:
    _provider(provider)
    session = _session(x_music_session, provider)
    if provider == "soundcloud" and session.public_url:
        return await public_import(
            "soundcloud",
            PublicBody(url=session.public_url, session=x_music_session, cursor=cursor),
            request,
        )
    await _refresh(session)
    try:
        offset = int(cursor or "0")
    except ValueError:
        raise HTTPException(400, "Некорректная страница") from None
    if offset < 0 or offset >= MAX_TRACKS or str(offset) != (cursor or "0"):
        raise HTTPException(400, "Некорректная страница")
    if provider == "spotify":
        async with _client(_proxy()) as client:
            body = await _call(
                client,
                "GET",
                f"{SPOTIFY_API}/me/tracks",
                params={"limit": PAGE_SIZE, "offset": offset},
                headers={"Authorization": f"Bearer {session.access_token}"},
            )
        raw_items = [item.get("track") for item in body.get("items", []) if isinstance(item, dict)]
        more = bool(body.get("next"))
    elif provider == "soundcloud":
        endpoint = session.page_urls.get(cursor) if cursor else f"{SC_API}/me/likes/tracks"
        if not endpoint:
            raise HTTPException(400, "Страница ещё не загружена")
        async with _client(_proxy()) as client:
            body = await _call(
                client,
                "GET",
                endpoint,
                params={"limit": PAGE_SIZE, "linked_partitioning": "true"} if not cursor else None,
                headers={
                    "Authorization": f"OAuth {session.access_token}",
                    "Accept": "application/json; charset=utf-8",
                },
            )
        raw_items = body.get("collection", [])
        next_url = body.get("next_href")
        more = False
        if isinstance(next_url, str):
            parsed_next = urlparse(next_url)
            if (
                parsed_next.scheme == "https"
                and parsed_next.netloc == "api.soundcloud.com"
                and parsed_next.path == "/me/likes/tracks"
            ):
                session.page_urls[str(offset + PAGE_SIZE)] = next_url
                more = True
    else:
        async with _client() as client:
            body = await _call(
                client,
                "POST",
                f"{VK_API}/audio.get",
                data={
                    "access_token": session.access_token,
                    "v": "5.199",
                    "count": PAGE_SIZE,
                    "offset": offset,
                },
            )
        _vk_error(body)
        payload = body.get("response", {})
        raw_items = payload.get("items", []) if isinstance(payload, dict) else []
        more = offset + PAGE_SIZE < int(payload.get("count", 0))
    if not isinstance(raw_items, list):
        raise HTTPException(502, "Некорректный ответ музыкального сервиса")
    normalized = []
    for raw in raw_items[:PAGE_SIZE]:
        if isinstance(raw, dict):
            if provider == "soundcloud" and isinstance(raw.get("track"), dict):
                raw = raw["track"]
            item = _track(provider, raw)
            if item is not None:
                session.imported[str(item["id"])] = item
                if provider == "soundcloud" and isinstance(raw.get("secret_token"), str):
                    session.secret_tokens[str(item["id"])] = raw["secret_token"][:256]
                normalized.append(item)
    next_offset = offset + PAGE_SIZE
    return {
        "session": x_music_session,
        "authenticated": True,
        "tracks": normalized,
        "next_cursor": str(next_offset) if more and next_offset < MAX_TRACKS else None,
        "has_more": bool(more and next_offset < MAX_TRACKS),
    }


@router.post("/{provider}/disconnect")
async def disconnect(
    provider: str, request: Request, x_music_session: str = Header(default="")
) -> dict[str, Any]:
    _check_origin(request)
    _provider(provider)
    session = _session(x_music_session, provider)
    if provider == "soundcloud" and session.access_token:
        await _refresh(session)
        try:
            async with _client(_proxy()) as client:
                response = await client.post(
                    f"{SC_API}/disconnect",
                    headers={"Authorization": f"OAuth {session.access_token}"},
                )
        except httpx.HTTPError:
            raise HTTPException(502, "Не удалось отозвать доступ SoundCloud") from None
        if response.status_code != 204:
            if response.status_code == 429:
                raise HTTPException(429, "Лимит SoundCloud; попробуйте позже")
            raise HTTPException(502, "SoundCloud не подтвердил отзыв доступа")
    SESSIONS.pop(x_music_session, None)
    return {"disconnected": True}


@router.post("/{provider}/public")
async def public_import(provider: str, body: PublicBody, request: Request) -> dict[str, Any]:
    _check_origin(request)
    _provider(provider)
    if not _safe_provider_url(body.url, provider):
        raise HTTPException(400, "Недопустимая ссылка на музыкальный сервис")
    if provider != "soundcloud":
        raise HTTPException(501, "Публичный импорт этого сервиса пока не подтверждён")
    parsed = urlparse(body.url)
    if not re.fullmatch(r"/[A-Za-z0-9_-]+/likes/?", parsed.path) or parsed.query:
        raise HTTPException(400, "Нужна ссылка на публичные лайки SoundCloud")
    try:
        offset = int(body.cursor or "0")
    except ValueError:
        raise HTTPException(400, "Некорректная страница") from None
    if offset < 0 or offset >= MAX_TRACKS or str(offset) != (body.cursor or "0"):
        raise HTTPException(400, "Некорректная страница")
    session = _session(body.session, "soundcloud") if body.session else None
    if session is not None and session.access_token:
        raise HTTPException(400, "Используйте отдельный публичный импорт")
    if session is not None and session.public_url != body.url:
        raise HTTPException(400, "Публичная ссылка не соответствует подключению")
    info = await _extract_async(body.url, flat=True, offset=offset)
    raw_entries = info.get("entries") or []
    if not isinstance(raw_entries, list):
        raise HTTPException(502, "Некорректный ответ SoundCloud")
    if session is None:
        handle = _store("soundcloud", "", expires_in=SESSION_TTL)
        session = SESSIONS[handle]
    else:
        handle = body.session
    session.public_url = body.url
    imported = []
    for raw in raw_entries[:PAGE_SIZE]:
        if not isinstance(raw, dict):
            continue
        identifier = str(raw.get("id", ""))
        if not re.fullmatch(r"\d{1,20}", identifier):
            continue
        canonical = raw.get("webpage_url") or raw.get("url") or ""
        if not isinstance(canonical, str) or not _safe_provider_url(canonical, "soundcloud"):
            continue
        url_artist = urlparse(canonical).path.strip("/").split("/")[0]
        item = {
            "id": identifier,
            "title": str(raw.get("title") or "SoundCloud track")[:200],
            "artist": str(raw.get("uploader") or raw.get("artist") or url_artist)[:200],
            "canonical_url": canonical,
            "playable": True,
            "reason": "",
        }
        session.imported[identifier] = item
        imported.append(item)
    next_offset = offset + PAGE_SIZE
    more = len(raw_entries) >= PAGE_SIZE and next_offset < MAX_TRACKS
    return {
        "session": handle,
        "authenticated": False,
        "tracks": imported,
        "next_cursor": str(next_offset) if more else None,
        "has_more": more,
    }


@router.post("/spotify/resolve")
async def spotify_resolve(body: ResolveBody, request: Request) -> dict[str, Any]:
    _check_origin(request)
    item = validate_track("spotify", body.session, body.track_id)
    session = _session(body.session, "spotify")
    key = str(item["id"])
    cached = RESOLVE_CACHE.get(key)
    if cached is not None:
        return cached.copy()
    async with session.lock:
        now = monotonic()
        if now - session.last_resolve < 6:
            raise HTTPException(429, "Подождите перед следующим поиском")
        session.last_resolve = now
    query = f"ytsearch1:{item['artist']} {item['title']} audio"
    async with _extract_limit:
        try:
            info = await asyncio.wait_for(asyncio.to_thread(_extract, query, flat=True), timeout=20)
        except (TimeoutError, Exception):
            raise HTTPException(502, "Не удалось найти трек на YouTube") from None
    entries = info.get("entries") or []
    first = entries[0] if isinstance(entries, list) and entries else None
    track_id = first.get("id") if isinstance(first, dict) else None
    if not isinstance(track_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", track_id):
        raise HTTPException(404, "Подходящая запись на YouTube не найдена")
    assert isinstance(first, dict)
    result = {
        "track_id": track_id,
        "title": f"{item['title']} — {item['artist']}",
        "matched_title": str(first.get("title") or "")[:200],
        "canonical_url": f"https://www.youtube.com/watch?v={track_id}",
    }
    if len(RESOLVE_CACHE) >= 128:
        RESOLVE_CACHE.pop(next(iter(RESOLVE_CACHE)))
    RESOLVE_CACHE[key] = result
    return result.copy()


def _queue_item(lobby_id: str, item_id: str, host_token: str) -> tuple[str, str, str]:
    from app.api.lobby import LOBBIES  # Local import avoids a router cycle.

    lobby = LOBBIES.get(lobby_id)
    if lobby is None:
        raise HTTPException(404, "Лобби не найдено")
    if not host_token or not hmac.compare_digest(host_token, lobby.host_token):
        raise HTTPException(403, "Только Main может получить аудио")
    item = next((i for i in lobby.queue_items if i.id == item_id), None)
    if item is None or not item.track_id.startswith(("sc_", "vk_")):
        raise HTTPException(404, "Трек не найден в очереди")
    handle = getattr(item, "provider_session", None)
    if not isinstance(handle, str):
        raise HTTPException(403, "Автору трека нужно подключиться снова")
    provider = "soundcloud" if item.track_id.startswith("sc_") else "vk"
    raw_id = item.track_id[3:]
    return provider, handle, raw_id


def _new_ticket(source: str, proxy: str | None) -> str:
    _clean()
    if len(TICKETS) >= 64:
        TICKETS.pop(next(iter(TICKETS)))
    ticket_id = secrets.token_urlsafe(24)
    TICKETS[ticket_id] = AudioTicket(source, proxy, monotonic() + 7200)
    return ticket_id


@router.get("/queue/{lobby_id}/{item_id}/stream")
async def queue_stream(
    lobby_id: str, item_id: str, x_host_token: str = Header(default="")
) -> dict[str, Any]:
    provider, handle, raw_id = _queue_item(lobby_id, item_id, x_host_token)
    session = _session(handle, provider)
    metadata = validate_track(provider, handle, raw_id)
    if provider == "vk":
        await _refresh(session)
        async with _client() as client:
            body = await _call(
                client,
                "POST",
                f"{VK_API}/audio.getById",
                data={"access_token": session.access_token, "v": "5.199", "audios": raw_id},
            )
        _vk_error(body)
        records = body.get("response")
        url = records[0].get("url") if isinstance(records, list) and records else None
        if not isinstance(url, str) or not _safe_vk_audio_url(url):
            raise HTTPException(422, "VK не предоставил поддерживаемый полный аудиопоток")
        return {"url": url, "format": "audio"}
    if session.access_token:
        await _refresh(session)
        auth = {"Authorization": f"OAuth {session.access_token}"}
        params = (
            {"secret_token": session.secret_tokens[raw_id]}
            if raw_id in session.secret_tokens
            else {}
        )
        async with _client(_proxy()) as client:
            resource = f"{SC_API}/tracks/soundcloud:tracks:{raw_id}"
            track = await _call(client, "GET", resource, headers=auth, params=params)
            if track.get("access") != "playable":
                raise HTTPException(422, "SoundCloud разрешает только preview или блокирует трек")
            streams = await _call(client, "GET", f"{resource}/streams", headers=auth, params=params)
        url = streams.get("hls_aac_160_url") or streams.get("hls_aac_96_url")
        if not isinstance(url, str) or not _safe_media_url(url):
            raise HTTPException(422, "SoundCloud не предоставил полный HLS-поток")
        ticket_id = _new_ticket(url, _proxy())
        return {"url": f"/api/music/hls/{ticket_id}/index.m3u8", "format": "hls"}
    info = await _extract_async(str(metadata["canonical_url"]))
    formats = info.get("formats") or []
    if not isinstance(formats, list):
        raise HTTPException(422, "SoundCloud не предоставил аудиопоток")
    candidates = [
        f
        for f in formats
        if isinstance(f, dict)
        and isinstance(f.get("url"), str)
        and f.get("acodec") not in (None, "none")
        and _safe_media_url(f["url"])
        and "preview" not in str(f.get("format_id", "")).lower()
        and "/preview/" not in urlparse(f["url"]).path.lower()
        and f.get("preference") != -10
    ]
    hls = [
        f for f in candidates if f.get("protocol") in ("m3u8", "m3u8_native") or ".m3u8" in f["url"]
    ]
    if hls:
        best = max(hls, key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0))
        ticket_id = _new_ticket(best["url"], info.get("_prada_proxy", _proxy()))
        return {"url": f"/api/music/hls/{ticket_id}/index.m3u8", "format": "hls"}
    progressive = [
        f for f in candidates if f.get("protocol") in ("http", "https") and ".m3u8" not in f["url"]
    ]
    if progressive:
        best = max(progressive, key=lambda f: f.get("abr") or 0)
        ticket_id = _new_ticket(best["url"], info.get("_prada_proxy", _proxy()))
        return {"url": f"/api/music/hls/{ticket_id}/audio.bin", "format": "audio"}
    raise HTTPException(422, "SoundCloud не предоставил полный поддерживаемый аудиопоток")


def _safe_vk_audio_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port is None
            and len(url) <= 4096
            and ".m3u8" not in parsed.path
            and any(
                host == name or host.endswith("." + name)
                for name in ("vkcdn.ru", "vkuseraudio.net", "userapi.com", "vk-cdn.net")
            )
        )
    except ValueError:
        return False


def _child(ticket_id: str, ticket: AudioTicket, target: str) -> str:
    if len(ticket.children) >= 4096:
        raise HTTPException(502, "Слишком длинный плейлист SoundCloud")
    if not _safe_media_url(target):
        raise HTTPException(502, "Недопустимый адрес аудиофайла")
    child_id = secrets.token_urlsafe(12)
    suffix = "m3u8" if ".m3u8" in urlparse(target).path else "bin"
    ticket.children[f"{child_id}.{suffix}"] = target
    return f"/api/music/hls/{ticket_id}/{child_id}.{suffix}"


def _rewrite_manifest(ticket_id: str, ticket: AudioTicket, source: str, content: str) -> str:
    if not content.startswith("#EXTM3U") or len(content) > 1_000_000:
        raise HTTPException(502, "Некорректный плейлист SoundCloud")
    if "SAMPLE-AES" in content or "KEYFORMAT=" in content:
        raise HTTPException(422, "Защищённый поток SoundCloud не поддерживается")
    output = []
    for line in content.splitlines():
        if line.startswith("#"):

            def replace_uri(match: re.Match[str]) -> str:
                target = urljoin(source, match.group(1))
                return f'URI="{_child(ticket_id, ticket, target)}"'

            output.append(re.sub(r'URI="([^"]+)"', replace_uri, line))
        elif line.strip():
            output.append(_child(ticket_id, ticket, urljoin(source, line.strip())))
    return "\n".join(output) + "\n"


async def _open_media(client: httpx.AsyncClient, url: str, range_header: str) -> httpx.Response:
    for _ in range(4):
        if not _safe_media_url(url):
            raise HTTPException(502, "Недопустимый адрес аудиофайла")
        try:
            request = client.build_request(
                "GET", url, headers={"Range": range_header} if range_header else None
            )
            response = await client.send(request, stream=True)
        except httpx.HTTPError:
            raise HTTPException(502, "Аудиопоток SoundCloud недоступен") from None
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            next_url = urljoin(url, location)
            await response.aclose()
            url = next_url
            continue
        if response.status_code not in (200, 206):
            await response.aclose()
            raise HTTPException(502, "Аудиопоток SoundCloud недоступен")
        return response
    raise HTTPException(502, "Слишком много перенаправлений аудиопотока")


async def _bounded_body(response: httpx.Response, limit: int) -> bytes:
    parts: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise HTTPException(502, "Слишком большой фрагмент SoundCloud")
        parts.append(chunk)
    return b"".join(parts)


@router.get("/hls/{ticket_id}/{resource}")
async def hls_resource(ticket_id: str, resource: str, range: str = Header(default="")) -> Response:
    _clean()
    ticket = TICKETS.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "Аудиоссылка истекла")
    source: str | None
    if resource == "index.m3u8" or resource == "audio.bin":
        source = ticket.source
    else:
        source = ticket.children.get(resource)
        if source is None:
            raise HTTPException(404, "Аудиофайл не найден")
    if range and not re.fullmatch(r"bytes=\d{0,12}-\d{0,12}", range):
        raise HTTPException(400, "Некорректный Range")
    client = _client(ticket.proxy)
    try:
        response = await _open_media(client, source, range)
    except BaseException:
        await client.aclose()
        raise
    if not resource.endswith(".m3u8"):
        length = response.headers.get("content-length", "")
        if resource != "audio.bin" and length.isdecimal() and int(length) > 16_000_000:
            await response.aclose()
            await client.aclose()
            raise HTTPException(502, "Слишком большой сегмент SoundCloud")
        headers = {"Cache-Control": "private, max-age=30"}
        for name in ("content-range", "accept-ranges", "content-length"):
            if name in response.headers:
                headers[name] = response.headers[name]

        async def body_stream() -> AsyncIterator[bytes]:
            received = 0
            try:
                async for chunk in response.aiter_bytes(16384):
                    received += len(chunk)
                    if resource != "audio.bin" and received > 16_000_000:
                        raise RuntimeError("SoundCloud segment exceeded limit")
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return StreamingResponse(
            body_stream(),
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "audio/mp4"),
            headers=headers,
        )
    try:
        content_bytes = await _bounded_body(response, 1_000_000)
    finally:
        await response.aclose()
        await client.aclose()
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(502, "Некорректный плейлист SoundCloud") from None
    rewritten = _rewrite_manifest(ticket_id, ticket, str(response.url), content)
    return Response(
        content=rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )
