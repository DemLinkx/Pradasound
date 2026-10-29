"""Small, in-memory personal music imports. Credentials never enter lobby events."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
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
MAX_DEVICE_LOGINS = 100
SPOTIFY_DEVICE_ID = "65b708073fc0480ea92a077233ca87bd"
SPOTIFY_API = "https://api.spotify.com/v1"
SC_API = "https://api.soundcloud.com"
VK_API = "https://api.vk.ru/method"
VK_PUBLIC_CLIENT_ID = "2685278"
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
    client_id: str = ""
    proxy: str | None = None
    cooldown_until: float = 0
    quota_reason: str = ""
    public_tracks: list[dict[str, object]] = field(default_factory=list)
    public_notice: str = ""


@dataclass
class PendingDevice:
    provider: str
    device_code: str
    client_id: str
    browser_nonce: str
    expires: float
    interval: int
    next_poll: float
    proxy: str | None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    session_handle: str = ""
    library: dict[str, Any] | None = None


PENDING: dict[str, PendingLogin] = {}
PENDING_DEVICES: dict[str, PendingDevice] = {}
SESSIONS: dict[str, MusicSession] = {}
RESOLVE_CACHE: dict[str, dict[str, str]] = {}
ROUTE_FAILURES: dict[str | None, float] = {}


@dataclass
class AudioTicket:
    source: str
    proxy: str | None
    expires: float
    children: dict[str, str] = field(default_factory=dict)
    provider: str = "soundcloud"


TICKETS: dict[str, AudioTicket] = {}
_extract_limit = asyncio.Semaphore(1)
_spotify_native_limit = asyncio.Semaphore(1)


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


def _vk_headers() -> dict[str, str]:
    agent = str(_value("vk_audio_user_agent", "")).strip()
    if not agent:
        return {}
    if len(agent) > 256 or "\r" in agent or "\n" in agent:
        raise HTTPException(503, "Некорректная настройка VK")
    return {"User-Agent": agent}


def _vk_authorization_url() -> str | None:
    client_id = str(_value("vk_public_client_id", VK_PUBLIC_CLIENT_ID))
    if not re.fullmatch(r"\d{1,16}", client_id):
        return None
    return "https://oauth.vk.com/authorize?" + urlencode(
        {
            "client_id": client_id,
            "scope": "8",
            "redirect_uri": "https://oauth.vk.com/blank.html",
            "display": "page",
            "response_type": "token",
        }
    )


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
    for key, device_pending in list(PENDING_DEVICES.items()):
        if device_pending.expires < now:
            del PENDING_DEVICES[key]
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


def _retry_after_seconds(value: str | None) -> int:
    if value is None:
        return 60
    text = value.strip()
    if text.isascii() and text.isdecimal():
        return 86400 if len(text) > 20 else min(86400, int(text))
    try:
        moment = parsedate_to_datetime(text)
        if moment.tzinfo is None:
            return 60
        seconds = math.ceil((moment - datetime.now(UTC)).total_seconds())
        return max(0, min(86400, seconds))
    except (TypeError, ValueError, OverflowError):
        return 60


def _rate_reason(response: httpx.Response) -> str:
    if len(response.content) > 65536:
        return "unknown"
    try:
        body = response.json()
    except ValueError:
        return "unknown"
    if not isinstance(body, dict):
        return "unknown"
    nested = body.get("error")
    candidates = [body.get("reason")]
    if isinstance(nested, dict):
        candidates.append(nested.get("reason"))
    return "QUOTA_EXCEEDED" if "QUOTA_EXCEEDED" in candidates else "unknown"


def _rate_detail(provider: str, reason: str, seconds: int) -> str:
    if provider == "spotify" and reason == "QUOTA_EXCEEDED":
        return "Spotify ограничил квоту приложения; повторный вход это не исправляет"
    if provider == "spotify":
        return f"Spotify отклонил запрос (429); повторная проверка через {seconds} с"
    return "Лимит музыкального сервиса; попробуйте позже"


def _spotify_cooldown(session: MusicSession) -> None:
    seconds = math.ceil(session.cooldown_until - monotonic())
    if seconds <= 0:
        return
    headers = {"Retry-After": str(seconds)}
    if session.quota_reason == "QUOTA_EXCEEDED":
        headers["X-Music-Rate-Reason"] = "QUOTA_EXCEEDED"
    raise HTTPException(
        429, _rate_detail("spotify", session.quota_reason, seconds), headers=headers
    )


def _rate_failure(response: httpx.Response, url: str) -> HTTPException:
    host = urlparse(url).hostname
    provider = (
        "spotify"
        if host in {"api.spotify.com", "accounts.spotify.com", "open.spotify.com"}
        else "soundcloud"
        if host in {"api.soundcloud.com", "secure.soundcloud.com"}
        else "unknown"
    )
    reason = _rate_reason(response)
    delay = _retry_after_seconds(response.headers.get("Retry-After"))
    if provider != "unknown":
        logging.getLogger("uvicorn.error").warning(
            "music_provider_rate_limit provider=%s reason=%s retry_after=%d",
            provider,
            reason,
            delay,
        )
    headers = {"Retry-After": str(delay)}
    if reason == "QUOTA_EXCEEDED" and provider == "spotify":
        headers["X-Music-Rate-Reason"] = reason
    return HTTPException(429, _rate_detail(provider, reason, delay), headers=headers)


async def _call(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.HTTPError:
        raise HTTPException(502, "Музыкальный сервис временно недоступен") from None
    if response.status_code == 429:
        raise _rate_failure(response, url)
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


def _spotify_playlist_id(url: str) -> str:
    if not _safe_provider_url(url, "spotify"):
        raise HTTPException(400, "Нужна ссылка на публичный плейлист Spotify")
    match = re.fullmatch(r"/(?:intl-[A-Za-z]{2}/)?playlist/([A-Za-z0-9]{22})/?", urlparse(url).path)
    if match is None:
        raise HTTPException(400, "Нужна ссылка на публичный плейлист Spotify")
    return match.group(1)


def _spotify_embed_host(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in {"open.spotify.com", "www.spotify.com"}
            and parsed.port is None
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and len(url) <= 2048
        )
    except ValueError:
        return False


async def _spotify_embed_html(playlist_id: str) -> str:
    url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    selected_proxy = _proxy()
    async with _client(selected_proxy) as client:
        for _ in range(4):
            if not _spotify_embed_host(url):
                raise HTTPException(502, "Spotify перенаправил на недопустимый адрес")
            try:
                request = client.build_request("GET", url, headers={"Accept": "text/html"})
                response = await client.send(request, stream=True)
            except httpx.HTTPError:
                raise HTTPException(502, "Публичный плейлист Spotify временно недоступен") from None
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location", "")
                    next_url = urljoin(url, location)
                    if not _spotify_embed_host(next_url):
                        raise HTTPException(502, "Spotify перенаправил на недопустимый адрес")
                    url = next_url
                    continue
                if response.status_code == 429:
                    content = await _bounded_body(response, 65536)
                    limited = httpx.Response(429, headers=response.headers, content=content)
                    raise _rate_failure(limited, url)
                if response.status_code in (403, 404):
                    raise HTTPException(403, "Публичный плейлист Spotify недоступен")
                if response.status_code != 200:
                    raise HTTPException(502, "Spotify отклонил публичный плейлист")
                content = await _bounded_body(response, 2_000_000)
                try:
                    return content.decode("utf-8")
                except UnicodeDecodeError:
                    raise HTTPException(502, "Некорректный ответ Spotify") from None
            finally:
                await response.aclose()
    raise HTTPException(502, "Слишком много перенаправлений Spotify")


def _spotify_embed_tracks(html: str) -> list[dict[str, object]]:
    match = re.search(
        r"<script\b[^>]*\bid\s*=\s*[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script\s*>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise HTTPException(422, "Публичные треки Spotify в этой ссылке недоступны")
    try:
        page = json.loads(match.group(1))
        entity = page["props"]["pageProps"]["state"]["data"]["entity"]
        raw_tracks = entity["trackList"]
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "Публичные треки Spotify в этой ссылке недоступны") from None
    if not isinstance(raw_tracks, list) or not raw_tracks:
        raise HTTPException(422, "Плейлист Spotify не содержит доступных публичных треков")
    result: list[dict[str, object]] = []
    for raw in raw_tracks[:100]:
        if not isinstance(raw, dict) or raw.get("entityType") != "track":
            continue
        uri = raw.get("uri")
        title = raw.get("title")
        artist = raw.get("subtitle")
        if not isinstance(uri, str) or not isinstance(title, str) or not title.strip():
            continue
        track_match = re.fullmatch(r"spotify:track:([A-Za-z0-9]{22})", uri)
        if track_match is None:
            continue
        track_id = track_match.group(1)
        playable = raw.get("isPlayable") is True
        result.append(
            {
                "id": track_id,
                "title": title[:200],
                "artist": artist[:200] if isinstance(artist, str) else "",
                "canonical_url": f"https://open.spotify.com/track/{track_id}",
                "playable": playable,
                "reason": "Для Spotify воспроизведение через найденное видео YouTube"
                if playable
                else "Трек Spotify недоступен",
            }
        )
    if not result:
        raise HTTPException(422, "Публичные треки Spotify в этой ссылке недоступны")
    return result


def _spotify_public_page(session: MusicSession, handle: str, cursor: str) -> dict[str, Any]:
    try:
        offset = int(cursor or "0")
    except ValueError:
        raise HTTPException(400, "Некорректная страница") from None
    if offset < 0 or offset >= 100 or str(offset) != (cursor or "0"):
        raise HTTPException(400, "Некорректная страница")
    page = session.public_tracks[offset : offset + PAGE_SIZE]
    if not page and offset:
        raise HTTPException(400, "Страница плейлиста не найдена")
    next_offset = offset + PAGE_SIZE
    more = next_offset < len(session.public_tracks)
    return {
        "session": handle,
        "authenticated": False,
        "tracks": page,
        "next_cursor": str(next_offset) if more else None,
        "has_more": more,
        "import_notice": session.public_notice,
    }


async def _spotify_native_page(session: MusicSession, handle: str, cursor: str) -> dict[str, Any]:
    helper = os.environ.get("SPOTIFY_NATIVE_HELPER", "")
    if not helper:
        raise HTTPException(503, "Чтение библиотеки Spotify ещё не установлено на сервере")
    try:
        offset = int(cursor or "0")
    except ValueError:
        raise HTTPException(400, "Некорректная страница") from None
    if offset < 0 or offset >= MAX_TRACKS or str(offset) != (cursor or "0"):
        raise HTTPException(400, "Некорректная страница")
    if cursor and cursor not in session.page_urls:
        raise HTTPException(400, "Страница ещё не загружена")
    await _refresh(session)
    async with session.lock, _spotify_native_limit:
        env = os.environ.copy()
        # Proxy applies only to this short-lived reader, including its TCP session.
        for name in (
            "ALL_PROXY",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "all_proxy",
            "https_proxy",
            "http_proxy",
        ):
            env.pop(name, None)
        env.update(
            ALL_PROXY=session.proxy or "",
            HTTPS_PROXY=session.proxy or "",
            NO_PROXY="",
            GOMEMLIMIT="48MiB",
        )
        payload = json.dumps(
            {"token": session.access_token, "cursor": session.page_urls.get(cursor, "")}
        ).encode()
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                helper,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(payload), 45)
            if process.returncode != 0 or len(stdout) > 2_000_000:
                raise ValueError("reader")
            body = json.loads(stdout)
        except (OSError, TimeoutError, ValueError):
            raise HTTPException(
                502, "Клиентское подключение Spotify не ответило; попробуйте подключиться заново"
            ) from None
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
        if not isinstance(body, dict):
            raise HTTPException(502, "Некорректный ответ клиентской библиотеки Spotify")
        error = body.get("error")
        if error:
            stage = (
                error
                if error
                in {"resolve", "session", "client_token", "login5", "collection", "metadata"}
                else "reader"
            )
            logging.getLogger("uvicorn.error").warning("spotify_native_read_failed stage=%s", stage)
            details = {
                "resolve": "Не удалось связаться с клиентским сервисом Spotify",
                "session": "Spotify не открыл клиентскую сессию; попробуйте новый вход с Premium",
                "client_token": "Spotify не подтвердил клиент приложения",
                "login5": "Spotify не подтвердил доступ клиентской сессии",
                "collection": "Spotify не отдал список любимых треков",
                "metadata": "Spotify не отдал названия и исполнителей любимых треков",
            }
            raise HTTPException(
                502,
                details.get(stage, "Не удалось прочитать библиотеку Spotify"),
            )
        raw_tracks = body.get("tracks")
        next_token = body.get("cursor")
        if (
            not isinstance(raw_tracks, list)
            or len(raw_tracks) > PAGE_SIZE
            or not isinstance(next_token, str)
            or len(next_token) > 8192
        ):
            raise HTTPException(502, "Некорректная страница библиотеки Spotify")
        normalized = []
        for raw in raw_tracks:
            if not isinstance(raw, dict):
                continue
            identifier, title, artist = raw.get("id"), raw.get("title"), raw.get("artist")
            if (
                not isinstance(identifier, str)
                or not re.fullmatch(r"[A-Za-z0-9]{22}", identifier)
                or not isinstance(title, str)
                or not title
            ):
                continue
            item: dict[str, object] = {
                "id": identifier,
                "title": title[:200],
                "artist": artist[:200] if isinstance(artist, str) else "",
                "canonical_url": f"https://open.spotify.com/track/{identifier}",
                "playable": True,
                "reason": "Воспроизведение через найденное видео YouTube",
            }
            session.imported[identifier] = item
            normalized.append(item)
        if raw_tracks and not normalized:
            raise HTTPException(502, "Spotify вернул некорректный список треков")
        next_offset = offset + PAGE_SIZE
        more = bool(next_token) and next_offset < MAX_TRACKS
        if more:
            session.page_urls[str(next_offset)] = next_token
        return {
            "session": handle,
            "authenticated": True,
            "tracks": normalized,
            "next_cursor": str(next_offset) if more else None,
            "has_more": more,
        }


async def _refresh(session: MusicSession) -> None:
    if session.provider == "vk" or session.expires > monotonic() + 60:
        return
    async with session.lock:
        if session.expires > monotonic() + 60:
            return
        configured_id, client_secret = _credentials(session.provider)
        client_id = session.client_id or configured_id
        if (
            not session.refresh_token
            or not client_id
            or (not client_secret and not session.client_id)
        ):
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
        }
        if client_secret and not session.client_id:
            data["client_secret"] = client_secret
        async with _client(session.proxy if session.client_id else _proxy()) as client:
            body = await _call(client, "POST", url, data=data)
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise HTTPException(401, "Подключите музыкальный аккаунт снова")
        session.access_token = token
        if isinstance(body.get("refresh_token"), str):
            session.refresh_token = body["refresh_token"]
        session.expires = monotonic() + int(body.get("expires_in", 3600))


def _store(
    provider: str,
    access: str,
    refresh: str = "",
    expires_in: int = 3600,
    *,
    client_id: str = "",
    proxy: str | None = None,
) -> str:
    _clean()
    if len(SESSIONS) >= MAX_SESSIONS:
        raise HTTPException(503, "Слишком много музыкальных подключений")
    handle = secrets.token_urlsafe(32)
    SESSIONS[handle] = MusicSession(
        provider,
        access,
        refresh,
        monotonic() + expires_in,
        monotonic() + SESSION_TTL,
        client_id=client_id,
        proxy=proxy,
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


def _safe_media_url(url: str, provider: str = "soundcloud") -> bool:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        domains = (
            ("vkcdn.ru", "vkuseraudio.net", "userapi.com", "vk-cdn.net")
            if provider == "vk"
            else ("sndcdn.com", "soundcloud.cloud", "soundcloud.com")
        )
        return (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port is None
            and len(url) <= 4096
            and any(host == name or host.endswith("." + name) for name in domains)
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
        device_enabled = provider == "spotify" and bool(_value("spotify_device_enabled", True))
        enabled = (
            bool(client_id and client_secret)
            if provider != "vk"
            else bool(_value("vk_audio_enabled", False))
        )
        reason = (
            "Готово к подключению"
            if enabled
            else "Подключение Spotify по коду устройства"
            if device_enabled
            else "Подключение Spotify выключено"
            if provider == "spotify"
            else "Нужны ключи приложения SoundCloud"
            if provider == "soundcloud"
            else "VK Audio API доступен только при отдельном разрешении"
        )
        result[provider] = {
            "oauth": enabled and provider != "vk",
            "device_login": device_enabled,
            "native_library": provider == "spotify"
            and bool(os.environ.get("SPOTIFY_NATIVE_HELPER")),
            "public_import": provider in {"spotify", "soundcloud"},
            "experimental_token": enabled and provider == "vk",
            "reason": reason,
        }
        if provider == "vk" and enabled:
            result[provider]["token_authorization_url"] = _vk_authorization_url()
            result[provider]["token_application"] = "Kate Mobile"
            result[provider]["reason"] = (
                "Экспериментальный вход через Kate Mobile; доступ к аудио проверяется после входа"
            )
    return {"providers": result}


def _spotify_verification_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 1024:
        return None
    try:
        parsed = urlparse(value)
        if (
            parsed.scheme == "https"
            and parsed.hostname in {"spotify.com", "www.spotify.com", "accounts.spotify.com"}
            and parsed.port is None
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and parsed.path.rstrip("/") == "/pair"
        ):
            return value
    except ValueError:
        pass
    return None


@router.post("/{provider}/device")
async def start_device(provider: str, request: Request, response: Response) -> dict[str, Any]:
    _check_origin(request)
    if provider != "spotify":
        raise HTTPException(404, "Подключение по коду для этого сервиса недоступно")
    if not _value("spotify_device_enabled", True):
        raise HTTPException(503, "Подключение Spotify по коду выключено")
    _clean()
    if len(PENDING_DEVICES) >= MAX_DEVICE_LOGINS:
        raise HTTPException(503, "Слишком много ожидающих подключений")
    client_id = str(_value("spotify_device_client_id", SPOTIFY_DEVICE_ID))
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", client_id):
        raise HTTPException(503, "Подключение Spotify не настроено")
    selected_proxy = _proxy()
    async with _client(selected_proxy) as client:
        body = await _call(
            client,
            "POST",
            "https://accounts.spotify.com/oauth2/device/authorize",
            data={"client_id": client_id, "scope": "streaming user-library-read"},
        )
    device_code = body.get("device_code")
    user_code = body.get("user_code")
    verification_url = _spotify_verification_url(
        body.get("verification_uri_complete")
    ) or _spotify_verification_url(body.get("verification_uri"))
    try:
        expires_in = int(str(body.get("expires_in", "")))
        interval = max(5, min(60, int(body.get("interval", 5))))
    except (ValueError, TypeError):
        raise HTTPException(502, "Некорректный ответ Spotify") from None
    if (
        not isinstance(device_code, str)
        or not 1 <= len(device_code) <= 2048
        or not isinstance(user_code, str)
        or not 1 <= len(user_code) <= 100
        or not verification_url
        or expires_in <= 0
    ):
        raise HTTPException(502, "Некорректный ответ Spotify")
    expires_in = min(expires_in, LOGIN_TTL)
    login_id = secrets.token_urlsafe(32)
    browser_nonce = secrets.token_urlsafe(32)
    now = monotonic()
    PENDING_DEVICES[login_id] = PendingDevice(
        "spotify",
        device_code,
        client_id,
        browser_nonce,
        now + expires_in,
        interval,
        now + interval,
        selected_proxy,
    )
    response.set_cookie(
        "music_device",
        browser_nonce,
        max_age=expires_in,
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="lax",
        path="/api/music",
    )
    return {
        "login_id": login_id,
        "verification_url": verification_url,
        "user_code": user_code,
        "interval": interval,
        "expires_in": expires_in,
    }


@router.get("/{provider}/device/{login_id}")
async def poll_device(provider: str, login_id: str, request: Request) -> dict[str, Any]:
    _check_origin(request)
    if provider != "spotify" or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", login_id):
        raise HTTPException(404, "Подключение не найдено")
    pending = PENDING_DEVICES.get(login_id)
    if pending is None or pending.provider != provider:
        raise HTTPException(404, "Подключение не найдено")
    browser_nonce = request.cookies.get("music_device", "")
    if not browser_nonce or not hmac.compare_digest(pending.browser_nonce, browser_nonce):
        raise HTTPException(404, "Подключение не найдено")
    if pending.expires <= monotonic():
        PENDING_DEVICES.pop(login_id, None)
        raise HTTPException(410, "Код Spotify истёк; начните подключение снова")
    if pending.library is not None:
        return {"status": "authorized", **pending.library}
    async with pending.lock:
        if pending.library is not None:
            return {"status": "authorized", **pending.library}
        now = monotonic()
        if now < pending.next_poll:
            return {"status": "pending", "retry_after": max(1, math.ceil(pending.next_poll - now))}
        pending.next_poll = now + pending.interval
        try:
            async with _client(pending.proxy) as client:
                response = await client.post(
                    "https://accounts.spotify.com/api/token",
                    data={
                        "client_id": pending.client_id,
                        "device_code": pending.device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                )
        except httpx.HTTPError:
            raise HTTPException(502, "Spotify временно недоступен") from None
        if len(response.content) > 65536:
            raise HTTPException(502, "Некорректный ответ Spotify")
        try:
            body = response.json()
        except ValueError:
            raise HTTPException(502, "Некорректный ответ Spotify") from None
        if not isinstance(body, dict):
            raise HTTPException(502, "Некорректный ответ Spotify")
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", str(pending.interval))
            raise HTTPException(
                429, "Лимит Spotify; попробуйте позже", headers={"Retry-After": retry}
            )
        if response.status_code == 400:
            error = body.get("error")
            if error == "authorization_pending":
                return {"status": "pending", "retry_after": pending.interval}
            if error == "slow_down":
                pending.interval = min(60, pending.interval + 5)
                pending.next_poll = monotonic() + pending.interval
                return {"status": "pending", "retry_after": pending.interval}
            if isinstance(error, str) and error in {"access_denied", "authorization_denied"}:
                PENDING_DEVICES.pop(login_id, None)
                raise HTTPException(403, "Подключение Spotify отклонено")
            if error == "expired_token":
                PENDING_DEVICES.pop(login_id, None)
                raise HTTPException(410, "Код Spotify истёк; начните подключение снова")
            raise HTTPException(502, "Spotify отклонил подключение")
        if response.status_code != 200:
            raise HTTPException(502, "Spotify отклонил подключение")
        access = body.get("access_token")
        refresh = body.get("refresh_token")
        if not isinstance(access, str) or not access:
            raise HTTPException(502, "Spotify не выдал токен доступа")
        if not isinstance(refresh, str):
            refresh = ""
        try:
            token_lifetime = max(60, min(86400, int(body.get("expires_in", 3600))))
        except (TypeError, ValueError):
            token_lifetime = 3600
        handle = _store(
            "spotify",
            access,
            refresh,
            token_lifetime,
            client_id=pending.client_id,
            proxy=pending.proxy,
        )
        pending.session_handle = handle
        try:
            library = await tracks("spotify", request=request, x_music_session=handle)
            library_status = 200
        except HTTPException as exc:
            library_status = exc.status_code
            library = {
                "session": handle,
                "authenticated": False,
                "tracks": [],
                "has_more": False,
                "next_cursor": None,
                "library_error_status": exc.status_code,
                "library_error": (
                    f"Доступ подтверждён, но библиотека Spotify недоступна: {exc.detail}"
                ),
            }
            if exc.status_code == 429:
                library["retry_after"] = _retry_after_seconds(
                    (exc.headers or {}).get("Retry-After")
                )
                if (exc.headers or {}).get("X-Music-Rate-Reason") == "QUOTA_EXCEEDED":
                    library["quota_reason"] = "QUOTA_EXCEEDED"
        logging.getLogger("uvicorn.error").info(
            "music_device_authorized provider=spotify library_access=%s tracks=%d http_status=%d",
            library_status == 200,
            len(library["tracks"]),
            library_status,
        )
        pending.library = library
        return {"status": "authorized", **library}


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
            headers=_vk_headers(),
        )
    _vk_error(data)
    payload = data.get("response")
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise HTTPException(502, "VK не подтвердил доступ к аудиобиблиотеке")
    handle = _store("vk", token, expires_in=SESSION_TTL)
    library = await tracks("vk", request=request, x_music_session=handle)
    logging.getLogger("uvicorn.error").info(
        "music_account_authorized provider=vk tracks=%d", len(library["tracks"])
    )
    return library


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
        logging.getLogger("uvicorn.error").warning(
            "music_provider_error provider=vk code=%s", code if isinstance(code, int) else "unknown"
        )
        if code == 5:
            raise HTTPException(401, "Токен VK истёк или недействителен; подключитесь снова")
        if code in {6, 9}:
            raise HTTPException(429, "Лимит VK; попробуйте позже", headers={"Retry-After": "10"})
        if code == 14:
            raise HTTPException(403, "VK требует подтверждение; получите новый токен в VK")
        if code == 17:
            raise HTTPException(403, "VK требует подтверждение входа на своей странице")
        if code in {15, 200, 201}:
            raise HTTPException(403, "VK ограничил доступ к аудио для этого приложения")
        raise HTTPException(403, "VK не предоставил доступ к музыкальной библиотеке")


@router.get("/{provider}/tracks")
async def tracks(
    provider: str, request: Request, cursor: str = "", x_music_session: str = Header(default="")
) -> dict[str, Any]:
    _provider(provider)
    session = _session(x_music_session, provider)
    if provider == "spotify" and session.public_url:
        return _spotify_public_page(session, x_music_session, cursor)
    if provider == "spotify" and session.client_id and os.environ.get("SPOTIFY_NATIVE_HELPER"):
        return await _spotify_native_page(session, x_music_session, cursor)
    if provider == "soundcloud" and session.public_url:
        return await public_import(
            "soundcloud",
            PublicBody(url=session.public_url, session=x_music_session, cursor=cursor),
            request,
        )
    if provider == "spotify":
        _spotify_cooldown(session)
    await _refresh(session)
    try:
        offset = int(cursor or "0")
    except ValueError:
        raise HTTPException(400, "Некорректная страница") from None
    if offset < 0 or offset >= MAX_TRACKS or str(offset) != (cursor or "0"):
        raise HTTPException(400, "Некорректная страница")
    if provider == "spotify":
        async with session.lock:
            _spotify_cooldown(session)
            try:
                async with _client(session.proxy if session.client_id else _proxy()) as client:
                    body = await _call(
                        client,
                        "GET",
                        f"{SPOTIFY_API}/me/tracks",
                        params={"limit": PAGE_SIZE, "offset": offset},
                        headers={"Authorization": f"Bearer {session.access_token}"},
                    )
            except HTTPException as exc:
                if exc.status_code == 429:
                    retry = _retry_after_seconds((exc.headers or {}).get("Retry-After"))
                    session.cooldown_until = monotonic() + retry
                    session.quota_reason = (exc.headers or {}).get("X-Music-Rate-Reason", "")
                raise
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
                headers=_vk_headers(),
            )
        _vk_error(body)
        payload = body.get("response", {})
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise HTTPException(502, "VK не подтвердил доступ к аудиобиблиотеке")
        raw_items = payload["items"]
        try:
            more = offset + PAGE_SIZE < int(payload.get("count", 0))
        except (TypeError, ValueError):
            raise HTTPException(502, "Некорректный ответ VK") from None
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
    if provider == "spotify":
        if body.session or body.cursor:
            raise HTTPException(400, "Используйте сохранённую сессию для следующей страницы")
        playlist_id = _spotify_playlist_id(body.url)
        html = await _spotify_embed_html(playlist_id)
        imported = _spotify_embed_tracks(html)
        handle = _store("spotify", "", expires_in=SESSION_TTL)
        spotify_session = SESSIONS[handle]
        spotify_session.public_url = f"https://open.spotify.com/playlist/{playlist_id}"
        spotify_session.public_tracks = imported
        spotify_session.public_notice = (
            "Публичный импорт: максимум первые 100 треков плейлиста; личные лайки в него не входят."
        )
        for item in imported:
            spotify_session.imported[str(item["id"])] = item
        return _spotify_public_page(spotify_session, handle, "")
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


def _new_ticket(source: str, proxy: str | None, provider: str = "soundcloud") -> str:
    _clean()
    if len(TICKETS) >= 64:
        TICKETS.pop(next(iter(TICKETS)))
    ticket_id = secrets.token_urlsafe(24)
    TICKETS[ticket_id] = AudioTicket(source, proxy, monotonic() + 7200, provider=provider)
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
                headers=_vk_headers(),
            )
        _vk_error(body)
        records = body.get("response")
        url = (
            records[0].get("url")
            if (isinstance(records, list) and records and isinstance(records[0], dict))
            else None
        )
        if not isinstance(url, str) or not _safe_media_url(url, "vk"):
            raise HTTPException(422, "VK не предоставил поддерживаемый полный аудиопоток")
        if ".m3u8" in urlparse(url).path.lower():
            ticket_id = _new_ticket(url, None, provider="vk")
            return {"url": f"/api/music/hls/{ticket_id}/index.m3u8", "format": "hls"}
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


def _child(ticket_id: str, ticket: AudioTicket, target: str) -> str:
    if len(ticket.children) >= 4096:
        raise HTTPException(502, "Слишком длинный плейлист SoundCloud")
    if not _safe_media_url(target, ticket.provider):
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


async def _open_media(
    client: httpx.AsyncClient, url: str, range_header: str, provider: str = "soundcloud"
) -> httpx.Response:
    for _ in range(4):
        if not _safe_media_url(url, provider):
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
        response = await _open_media(client, source, range, ticket.provider)
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
