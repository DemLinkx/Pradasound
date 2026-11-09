import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import math
import re
import secrets
from dataclasses import asdict, dataclass, field
from time import monotonic, time
from urllib.parse import parse_qsl, urlencode, urlparse
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet

from fastapi import APIRouter, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.settings import MAX_TRACK_SECONDS, Settings

router = APIRouter(prefix="/api/lobby", tags=["lobby"])
origins = Settings().cors_origins
telegram_bot_token = Settings().telegram_bot_token
telegram_webhook_secret = Settings().telegram_webhook_secret
telegram_mini_app_url = Settings().telegram_mini_app_url
telegram_proxy_url = Settings().telegram_proxy_url
room_settings = Settings()
ROOM_DIRTY = 0
ROOM_SAVED = 0
ROOM_SAVE_ERROR = False

TELEGRAM_STATUS: dict[str, object] = {"connected": False, "polls": 0}


@router.get("/config")
async def public_config() -> dict[str, object]:
    username = Settings().telegram_bot_username.lstrip("@")
    return {"telegram_bot_username": username if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username) else "",
            "max_track_seconds": MAX_TRACK_SECONDS}


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request, x_telegram_bot_api_secret_token: str = Header(default="")
) -> dict[str, object]:
    if not telegram_webhook_secret or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, telegram_webhook_secret
    ):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not telegram_mini_app_url.startswith("https://"):
        raise HTTPException(status_code=503, detail="Mini App is not configured")
    if len(await request.body()) > 16384:
        raise HTTPException(status_code=413, detail="Update too large")
    try:
        update = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid update") from None
    return telegram_reply(update)


def telegram_reply(update: object) -> dict[str, object]:
    if not isinstance(update, dict):
        return {}
    message = update.get("message")
    if not isinstance(message, dict):
        return {}
    chat = message.get("chat")
    text = message.get("text")
    if (not isinstance(chat, dict) or chat.get("type") != "private"
            or type(chat.get("id")) is not int or not isinstance(text, str)
            or text.partition(" ")[0] not in ("/start", "/help")):
        return {}
    app_url = telegram_mini_app_url
    payload = text.partition(" ")[2].strip()
    if re.fullmatch(r"l_[a-f0-9]{32}", payload):
        parsed = urlparse(app_url)
        query = dict(parse_qsl(parsed.query))
        query["lobby"] = payload[2:]
        app_url = parsed._replace(query=urlencode(query)).geturl()
    return {
        "method": "sendMessage",
        "chat_id": chat["id"],
        "text": (
            "Prada Sound — общая очередь музыки для вашей вечеринки. "
            "Откройте приложение, создайте комнату и пригласите друзей."
        ),
        "reply_markup": {"inline_keyboard": [[{
            "text": "Открыть Prada Sound",
            "web_app": {"url": app_url},
        }]]},
    }


async def telegram_polling_loop() -> None:
    offset = 0
    backoff = 2
    initialized = False
    async with httpx.AsyncClient(proxy=telegram_proxy_url or None, trust_env=False,
                                 timeout=httpx.Timeout(15, read=45)) as client:
        async def call(method: str, payload: dict) -> object:
            response = await client.post(f"https://api.telegram.org/bot{telegram_bot_token}/{method}",
                                         json=payload)
            response.raise_for_status()
            body = response.json()
            if not body.get("ok"):
                raise ValueError("Telegram API rejected the request")
            return body["result"]

        while True:
            try:
                if not initialized:
                    await call("deleteWebhook", {"drop_pending_updates": False})
                    initialized = True
                updates = await call("getUpdates", {"offset": offset, "timeout": 35,
                                                    "allowed_updates": ["message"]})
                if not isinstance(updates, list):
                    raise ValueError("Invalid Telegram updates")
                if not TELEGRAM_STATUS["connected"]:
                    logging.getLogger("uvicorn.error").info("Telegram polling connected")
                TELEGRAM_STATUS.update(connected=True, polls=int(TELEGRAM_STATUS["polls"]) + 1)
                for update in updates:
                    reply = telegram_reply(update)
                    if reply:
                        method = str(reply.pop("method"))
                        await call(method, reply)
                    offset = int(update["update_id"]) + 1
                backoff = 2
            except (httpx.HTTPError, ValueError, KeyError):
                TELEGRAM_STATUS["connected"] = False
                logging.getLogger("uvicorn.error").warning("Telegram connection unavailable; retrying")
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)


class TelegramAuth(BaseModel):
    init_data: str = ""


@dataclass
class TelegramUser:
    id: int
    name: str
    username: str | None
    photo_url: str | None

    def public(self) -> dict[str, str | None]:
        return {"name": self.name, "username": self.username, "photo_url": self.photo_url}


USERS: dict[int, TelegramUser] = {}


def verify_telegram(init_data: str) -> TelegramUser:
    if (
        not telegram_bot_token
        or not isinstance(init_data, str)
        or not init_data
        or len(init_data.encode()) > 8192
    ):
        raise ValueError("telegram_not_configured_or_missing")
    fields = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    data = dict(fields)
    if len(fields) != len(data) or not re.fullmatch(r"[a-f0-9]{64}", data.get("hash", "")):
        raise ValueError("invalid_telegram_data")
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()) if key != "hash")
    secret = hmac.new(b"WebAppData", telegram_bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, data["hash"]):
        raise ValueError("invalid_telegram_signature")
    try:
        auth_date = int(data["auth_date"])
        raw_user = json.loads(data["user"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_telegram_user") from exc
    if auth_date > time() + 60 or time() - auth_date > 86400:
        raise ValueError("expired_telegram_data")
    if not isinstance(raw_user, dict) or type(raw_user.get("id")) is not int or raw_user["id"] <= 0:
        raise ValueError("invalid_telegram_user")
    first = raw_user.get("first_name")
    last = raw_user.get("last_name")
    username = raw_user.get("username")
    photo = raw_user.get("photo_url")
    if not isinstance(first, str) or not first.strip() or len(first) > 100:
        raise ValueError("invalid_telegram_user")
    if last is not None and (not isinstance(last, str) or len(last) > 100):
        raise ValueError("invalid_telegram_user")
    if username is not None and (
        not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
    ):
        raise ValueError("invalid_telegram_user")
    if photo is not None and (
        not isinstance(photo, str) or len(photo) > 2048 or urlparse(photo).scheme != "https"
    ):
        photo = None
    name = " ".join(part for part in (first.strip(), (last or "").strip()) if part)
    user = TelegramUser(raw_user["id"], name, username, photo)
    USERS[user.id] = user
    return user


@dataclass
class QueueItem:
    id: str
    track_id: str
    owner_id: str
    yandex_session: str | None = None
    access_revision: int = 0
    provider_session: str | None = None
    canonical_url: str | None = None
    duration_ms: int = 0
    added_order: int = 0
    votes: dict[str, int] = field(default_factory=dict)

    @property
    def rating(self) -> int:
        return sum(self.votes.values())


@dataclass
class Lobby:
    host_token: str
    host_telegram_id: int | None = None
    main_id: str | None = None
    queue_items: list[QueueItem] = field(default_factory=list)
    track_titles: dict[str, str] = field(default_factory=dict)
    playback_revision: int = 0
    queue_revision: int = 0
    owner_ids: dict[str, str] = field(default_factory=dict)
    profiles: dict[str, dict[str, str | None]] = field(default_factory=dict)
    member_owners: dict[str, str] = field(default_factory=dict)
    member_telegram: dict[str, int | None] = field(default_factory=dict)
    connections: dict[str, WebSocket] = field(default_factory=dict)
    last_empty: float = field(default_factory=monotonic)
    ever_shared: bool = False
    ever_connected: bool = False
    vacated: bool = False
    pending: int = 0
    claim_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    broadcast_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    listener_tokens: dict[str, str] = field(default_factory=dict)
    playback: dict[str, object] | None = None
    sync_serial: int = 0
    last_sync: float = 0
    clock_started: float = 0
    server_offset: float = 0
    preparing_id: str = ""
    playback_error: str = ""
    failed_at: float = 0
    relay_source: str | None = None
    relay_generation: int = 0
    relay_active: bool = False
    relay_requests: set[str] = field(default_factory=set)
    relay_listeners: set[str] = field(default_factory=set)


def rank_queue(lobby: Lobby) -> None:
    lobby.queue_items[1:] = sorted(lobby.queue_items[1:], key=lambda item: (-item.rating, item.added_order))


LOBBIES: dict[str, Lobby] = {}


def mark_rooms_dirty() -> None:
    global ROOM_DIRTY
    ROOM_DIRTY += 1


def room_idle_ttl(lobby: Lobby) -> int:
    return 900 if lobby.ever_connected or lobby.ever_shared else 30


def room_state_cipher() -> Fernet | None:
    if room_settings.room_state_file is None:
        return None
    key = room_settings.room_state_key.get_secret_value()
    if not key:
        raise RuntimeError("ROOM_STATE_KEY is required for encrypted room recovery")
    return Fernet(key.encode())


def room_records() -> bytes:
    now = time()
    records = {}
    for lid, room in LOBBIES.items():
        if is_expired(room):
            continue
        records[lid] = {
            "host_token": room.host_token, "host_telegram_id": room.host_telegram_id,
            "queue_items": [asdict(item) for item in room.queue_items],
            "track_titles": room.track_titles, "owner_ids": room.owner_ids, "profiles": room.profiles,
            "playback_revision": room.playback_revision, "queue_revision": room.queue_revision,
            "ever_shared": room.ever_shared, "ever_connected": room.ever_connected,
            "empty_since": None if room.connections else now - (monotonic() - room.last_empty),
            "playback": playback_snapshot(room), "sync_serial": room.sync_serial,
        }
    return json.dumps({"format": 1, "rooms": records}, allow_nan=False, separators=(",", ":")).encode()


def write_room_records(payload: bytes) -> None:
    cipher = room_state_cipher()
    path = room_settings.room_state_file
    if cipher is None or path is None:
        return
    if len(payload) > 32 * 1024 * 1024:
        raise RuntimeError("Room recovery data exceeded 32 MiB")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(cipher.encrypt(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


async def save_rooms() -> None:
    global ROOM_SAVED, ROOM_SAVE_ERROR
    if room_settings.room_state_file is None or ROOM_SAVED == ROOM_DIRTY:
        return
    revision, payload = ROOM_DIRTY, room_records()
    writer = asyncio.create_task(asyncio.to_thread(write_room_records, payload))
    try:
        await asyncio.shield(writer)
    except asyncio.CancelledError:
        await writer
        raise
    except Exception:
        if not ROOM_SAVE_ERROR:
            logging.getLogger("uvicorn.error").error("Encrypted room recovery save failed")
        ROOM_SAVE_ERROR = True
    else:
        ROOM_SAVED, ROOM_SAVE_ERROR = revision, False


def load_rooms() -> None:
    cipher = room_state_cipher()
    path = room_settings.room_state_file
    if cipher is None or path is None or not path.exists():
        return
    if path.stat().st_size > 48 * 1024 * 1024:
        raise RuntimeError("Room recovery file exceeded limit")
    try:
        data = json.loads(cipher.decrypt(path.read_bytes()))
        if data.get("format") != 1 or not isinstance(data.get("rooms"), dict) or len(data["rooms"]) > 100:
            raise ValueError("Invalid room recovery format")
        restored = {}
        for lid, row in data["rooms"].items():
            if not re.fullmatch(r"[a-f0-9]{32}", lid) or not isinstance(row.get("host_token"), str):
                raise ValueError("Invalid recovered room")
            if not isinstance(row.get("queue_items"), list) or len(row["queue_items"]) > 500:
                raise ValueError("Invalid recovered queue")
            room = Lobby(host_token=row["host_token"], host_telegram_id=row.get("host_telegram_id"),
                         ever_shared=bool(row.get("ever_shared")), ever_connected=bool(row.get("ever_connected")),
                         vacated=True, playback_revision=int(row.get("playback_revision", 0)),
                         queue_revision=int(row.get("queue_revision", 0)), sync_serial=int(row.get("sync_serial", 0)))
            room.queue_items = [QueueItem(**item) for item in row["queue_items"]]
            room.track_titles, room.owner_ids, room.profiles = row["track_titles"], row["owner_ids"], row["profiles"]
            empty = row.get("empty_since")
            if empty is not None and (type(empty) not in (int, float) or not math.isfinite(empty)):
                raise ValueError("Invalid recovered expiry")
            room.last_empty = monotonic() - max(0, time() - empty) if empty is not None else monotonic()
            state = row.get("playback")
            if state and room.queue_items and state.get("item_id") == room.queue_items[0].id:
                position = float(state["position"])
                if not state["paused"]:
                    position += max(0, time() - float(state["server_time"]) / 1000)
                room.playback = {**state, "position": min(float(state["duration"]), position), "paused": True, "server_time": time() * 1000}
                room.clock_started = monotonic()
            if not is_expired(room):
                restored[lid] = room
        LOBBIES.update(restored)
    except Exception:
        raise RuntimeError("Encrypted room recovery failed; check the file and key") from None


SERVER_PREPARES: dict[str, asyncio.Task] = {}


def server_head(lobby: Lobby) -> bool:
    return bool(room_settings.server_playback_enabled and lobby.queue_items and
                not lobby.queue_items[0].track_id.startswith(("yandex_", "spotify_", "apple_")))


def set_room_clock(lobby: Lobby, position: float, duration: float, paused: bool, *, transport: bool = True) -> None:
    lobby.sync_serial += 1
    previous = lobby.playback or {}
    lobby.playback = {"item_id": lobby.queue_items[0].id, "revision": lobby.playback_revision,
                      "position": max(0, min(position, duration)), "duration": duration, "paused": paused,
                      "server_time": time() * 1000, "serial": lobby.sync_serial, "authority": "server",
                      "transport_serial": lobby.sync_serial if transport else previous.get("transport_serial", lobby.sync_serial)}
    lobby.clock_started = monotonic()
    mark_rooms_dirty()


def advance_room(lobby: Lobby, *, overlap: float = 0) -> None:
    removed = lobby.queue_items.pop(0)
    if not any(item.track_id == removed.track_id for item in lobby.queue_items):
        lobby.track_titles.pop(removed.track_id, None)
    lobby.playback_revision += 1
    lobby.queue_revision += 1
    lobby.server_offset = overlap
    lobby.preparing_id = ""
    lobby.failed_at = 0
    lobby.playback_error = ""
    mark_rooms_dirty()


async def prepare_room_clock(lid: str, room: Lobby, item: QueueItem, revision: int) -> None:
    from app.main import room_stream_descriptor
    room.preparing_id = item.id
    try:
        data = await asyncio.wait_for(room_stream_descriptor(lid, item.id), timeout=45)
        if LOBBIES.get(lid) is not room or not room.queue_items or room.queue_items[0].id != item.id or room.playback_revision != revision:
            return
        duration = data.get("duration") or item.duration_ms / 1000
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            raise HTTPException(422, "Источник не сообщил длительность трека")
        if duration > MAX_TRACK_SECONDS:
            raise HTTPException(422, "Трек длиннее 12 минут")
        item.duration_ms = int(duration * 1000)
        room.playback_error = ""
        set_room_clock(room, room.server_offset, duration, False)
        room.server_offset = 0
        await manager.broadcast(lid)
    except Exception:
        if LOBBIES.get(lid) is room and room.queue_items and room.queue_items[0].id == item.id and room.playback_revision == revision:
            room.failed_at = monotonic()
            room.playback_error = "Аудио недоступно. Сервер попробует следующий трек через 5 секунд."
            await manager.broadcast(lid)
    finally:
        if room.preparing_id == item.id:
            room.preparing_id = ""


async def server_clock_tick() -> None:
    for lid, task in list(SERVER_PREPARES.items()):
        if task.done():
            if not task.cancelled() and task.exception() is not None:
                logging.getLogger("uvicorn.error").error("Room audio preparation failed")
            SERVER_PREPARES.pop(lid, None)
        elif lid not in LOBBIES or not LOBBIES[lid].queue_items or task.get_name() != f"{LOBBIES[lid].queue_items[0].id}:{LOBBIES[lid].playback_revision}":
            task.cancel()
            SERVER_PREPARES.pop(lid, None)
    for lid, room in list(LOBBIES.items()):
        if not room.connections or not server_head(room):
            continue
        if room.failed_at:
            if monotonic() - room.failed_at >= 5:
                advance_room(room)
                await manager.broadcast(lid)
            continue
        state = playback_snapshot(room)
        if state and state.get("authority") == "server":
            if not state["paused"] and state["position"] >= state["duration"]:
                overlap = 3 if len(room.queue_items) > 1 and not room.queue_items[1].track_id.startswith(("spotify_", "apple_", "yandex_")) else 0
                advance_room(room, overlap=overlap)
                await manager.broadcast(lid)
            else:
                continue
        if room.queue_items and server_head(room) and lid not in SERVER_PREPARES:
            SERVER_PREPARES[lid] = asyncio.create_task(prepare_room_clock(lid, room, room.queue_items[0], room.playback_revision), name=f"{room.queue_items[0].id}:{room.playback_revision}")


async def room_maintenance() -> None:
    last_save = monotonic()
    try:
        while True:
            await asyncio.sleep(.25)
            await server_clock_tick()
            for lid, room in list(LOBBIES.items()):
                if not room.pending and is_expired(room):
                    LOBBIES.pop(lid, None)
                    mark_rooms_dirty()
            if monotonic() - last_save >= 1:
                await save_rooms()
                last_save = monotonic()
    finally:
        tasks = list(SERVER_PREPARES.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        SERVER_PREPARES.clear()


def reset_relay(lobby: Lobby) -> None:
    lobby.relay_source = None
    lobby.relay_generation += 1
    lobby.relay_active = False
    lobby.relay_listeners.clear()


def relay_ice(lobby_id: str, member_id: str) -> list[dict[str, object]]:
    settings = Settings()
    servers: list[dict[str, object]] = [{"urls": "stun:stun.cloudflare.com:3478"}]
    if settings.relay_turn_secret and settings.relay_turn_urls:
        username = f"{int(time()) + 3600}:{lobby_id}:{member_id}"
        credential = base64.b64encode(hmac.new(settings.relay_turn_secret.encode(),
                                               username.encode(), hashlib.sha1).digest()).decode()
        servers.append({"urls": settings.relay_turn_urls, "username": username,
                        "credential": credential})
    return servers


async def expire_empty(lobby_id: str, lobby: Lobby, stamp: float) -> None:
    await asyncio.sleep(room_idle_ttl(lobby))
    while LOBBIES.get(lobby_id) is lobby and not lobby.connections and lobby.last_empty == stamp:
        if not lobby.pending:
            LOBBIES.pop(lobby_id, None)
            mark_rooms_dirty()
            return
        await asyncio.sleep(1)


def is_expired(lobby: Lobby) -> bool:
    return (not lobby.connections and
            monotonic() - lobby.last_empty >= (room_idle_ttl(lobby)))


def playback_snapshot(lobby: Lobby) -> dict[str, object] | None:
    state = lobby.playback
    if (not state or not lobby.queue_items or
            state.get("item_id") != lobby.queue_items[0].id or
            state.get("revision") != lobby.playback_revision):
        return None
    if state.get("authority") == "server":
        position = float(state["position"]) + (0 if state["paused"] else max(0, monotonic() - lobby.clock_started))
        return {**state, "position": min(float(state["duration"]), position), "server_time": time() * 1000}
    return state


def listener_lobby(lobby_id: str, token: str) -> Lobby:
    lobby = LOBBIES.get(lobby_id)
    member_id = lobby.listener_tokens.get(token) if lobby and token else None
    if not lobby or not member_id or member_id not in lobby.connections:
        raise HTTPException(403, "Включите «Слушать вместе» в подключённой комнате")
    if lobby.main_id is None and not server_head(lobby):
        raise HTTPException(409, "Ждём подключения хоста")
    return lobby


class ConnectionManager:
    async def connect(self, lobby_id: str, ws: WebSocket, main: bool, owner_id: str) -> str | None:
        lobby = LOBBIES[lobby_id]
        if main and lobby.main_id is not None:
            previous_id = lobby.main_id
            previous = lobby.connections.pop(previous_id, None)
            lobby.member_owners.pop(previous_id, None)
            lobby.member_telegram.pop(previous_id, None)
            lobby.main_id = None
            reset_relay(lobby)
            if previous is not None:
                try:
                    await previous.close(code=1012, reason="main_reconnected")
                except (OSError, RuntimeError, WebSocketDisconnect):
                    pass
        if len(lobby.connections) + (lobby.main_id is None and not main) >= 100:
            await ws.close(code=1008, reason="lobby_full")
            return None
        member_id = uuid4().hex
        lobby.connections[member_id] = ws
        lobby.member_owners[member_id] = owner_id
        if len(set(lobby.member_owners.values())) >= 2:
            lobby.ever_shared = True
        lobby.vacated = False
        lobby.ever_connected = True
        mark_rooms_dirty()
        if main:
            lobby.main_id = member_id
        await self.send(ws, {"type": "identity", "owner_id": owner_id, "member_id": member_id})
        await self.broadcast(lobby_id)
        return member_id

    def disconnect(self, lobby_id: str, member_id: str) -> None:
        lobby = LOBBIES.get(lobby_id)
        if lobby is None or member_id not in lobby.connections:
            return
        mark_rooms_dirty()
        lobby.connections.pop(member_id, None)
        lobby.member_owners.pop(member_id, None)
        lobby.member_telegram.pop(member_id, None)
        lobby.relay_requests.discard(member_id)
        lobby.relay_listeners.discard(member_id)
        if member_id in (lobby.relay_source, lobby.main_id):
            reset_relay(lobby)
        for token, member in list(lobby.listener_tokens.items()):
            if member == member_id:
                del lobby.listener_tokens[token]
        if lobby.main_id == member_id:
            lobby.main_id = None
            state = playback_snapshot(lobby)
            if state and state.get("authority") != "server":
                now = time() * 1000
                position = float(state["position"])
                if not state["paused"]:
                    position += max(0, now - float(state["server_time"])) / 1000
                lobby.sync_serial += 1
                lobby.playback = {**state, "paused": True, "position": position,
                                  "server_time": now, "serial": lobby.sync_serial}
        host_owner = lobby.owner_ids.get(lobby.host_token)
        for owner_id in list(lobby.profiles):
            if (
                owner_id != host_owner
                and owner_id not in lobby.member_owners.values()
                and not any(item.owner_id == owner_id for item in lobby.queue_items)
            ):
                lobby.profiles.pop(owner_id, None)
        if not lobby.connections:
            state = playback_snapshot(lobby)
            if state and state.get("authority") == "server":
                set_room_clock(lobby, state["position"], state["duration"], True)
            lobby.last_empty = monotonic()
            lobby.vacated = True
            asyncio.create_task(expire_empty(lobby_id, lobby, lobby.last_empty))

    async def send(self, ws: WebSocket, message: dict[str, object]) -> bool:
        try:
            await asyncio.wait_for(ws.send_json(message), timeout=2)
            return True
        except (TimeoutError, OSError, RuntimeError, WebSocketDisconnect):
            try:
                await asyncio.wait_for(ws.close(code=1013), timeout=0.5)
            except (TimeoutError, OSError, RuntimeError, WebSocketDisconnect):
                pass
            return False

    async def broadcast(self, lobby_id: str) -> None:
        lobby = LOBBIES.get(lobby_id)
        if lobby is None:
            return
        async with lobby.broadcast_lock:
            if LOBBIES.get(lobby_id) is lobby:
                await self._broadcast(lobby_id)

    async def sync_listeners(self, lobby_id: str) -> None:
        lobby = LOBBIES[lobby_id]
        state = playback_snapshot(lobby)
        if state is None:
            return
        targets = [(mid, lobby.connections[mid]) for mid in set(lobby.listener_tokens.values())
                   if mid in lobby.connections]
        if lobby.relay_source and lobby.relay_source != lobby.main_id and lobby.main_id:
            targets.append((lobby.main_id, lobby.connections[lobby.main_id]))
        sent = await asyncio.gather(*(self.send(ws, {"type": "playback_sync", **state})
                                      for _, ws in targets))
        for (mid, _), success in zip(targets, sent):
            if not success:
                self.disconnect(lobby_id, mid)
        if not all(sent):
            await self.broadcast(lobby_id)

    async def _broadcast(self, lobby_id: str) -> None:
        lobby = LOBBIES[lobby_id]
        active = bool(lobby.relay_source and lobby.queue_items and
                      lobby.queue_items[0].track_id.startswith("spotify_"))
        if active != lobby.relay_active:
            lobby.relay_active = active
            lobby.relay_generation += 1
            lobby.relay_listeners.clear()
        while lobby.connections:
            clients = list(lobby.connections.items())
            state: dict[str, object] = {
                "type": "lobby_state",
                "lobby_id": lobby_id,
                "tracks_queue": [item.track_id for item in lobby.queue_items],
                "queue_items": [
                    {"id": item.id, "track_id": item.track_id, "owner_id": item.owner_id,
                     "access_revision": item.access_revision, "rating": item.rating,
                     "likes": sum(v == 1 for v in item.votes.values()),
                     "dislikes": sum(v == -1 for v in item.votes.values()),
                     **({"duration_ms": item.duration_ms} if item.duration_ms else {}),
                     **({"canonical_url": item.canonical_url} if item.canonical_url else {})}
                    for item in lobby.queue_items
                ],
                "track_titles": dict(lobby.track_titles),
                "playback_revision": lobby.playback_revision,
                "playback": playback_snapshot(lobby),
                "host_online": lobby.main_id is not None,
                "server_playback": server_head(lobby),
                "playback_error": lobby.playback_error,
                "queue_revision": lobby.queue_revision,
                "profiles": dict(lobby.profiles),
                "main_profile": lobby.profiles.get(lobby.owner_ids.get(lobby.host_token, "")),
                "relay": {"source_id": lobby.relay_source, "generation": lobby.relay_generation,
                          "requests": sorted(lobby.relay_requests),
                          "listeners": sorted(lobby.relay_listeners)},
                "members": [
                    {
                        "member_id": member_id,
                        "role": "Main" if member_id == lobby.main_id else "Member",
                        "owner_id": lobby.member_owners.get(member_id),
                    }
                    for member_id, _ in clients
                ],
            }
            sent = await asyncio.gather(*(self.send(ws, {**state, "my_votes": {
                item.id: item.votes[owner] for item in lobby.queue_items if owner in item.votes
            }}) for mid, ws in clients for owner in [lobby.member_owners.get(mid, "")]))
            failed = [member_id for (member_id, _), success in zip(clients, sent) if not success]
            if not failed:
                return
            for member_id in failed:
                self.disconnect(lobby_id, member_id)


manager = ConnectionManager()


@router.post("/telegram/me")
async def telegram_me(body: TelegramAuth) -> dict[str, object]:
    try:
        user = verify_telegram(body.init_data)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"authenticated": True, "profile": user.public()}


@router.post("/create", status_code=201)
async def create_lobby(body: TelegramAuth | None = None) -> dict[str, str]:
    user = None
    if body and body.init_data:
        try:
            user = verify_telegram(body.init_data)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    for expired_id, lobby in list(LOBBIES.items()):
        if not lobby.pending and is_expired(lobby):
            del LOBBIES[expired_id]
    if len(LOBBIES) >= 100:
        raise HTTPException(status_code=503, detail="lobby_limit_reached")
    lobby_id = uuid4().hex
    host_token = secrets.token_urlsafe(32)
    lobby = Lobby(host_token=host_token, host_telegram_id=user.id if user else None)
    if user:
        owner_id = uuid4().hex
        lobby.owner_ids[host_token] = owner_id
        lobby.profiles[owner_id] = user.public()
    LOBBIES[lobby_id] = lobby
    mark_rooms_dirty()
    asyncio.create_task(expire_empty(lobby_id, lobby, lobby.last_empty))
    return {"lobby_id": lobby_id, "host_token": host_token, "role": "Main"}


@router.websocket("/{lobby_id}/ws")
async def lobby_ws(ws: WebSocket, lobby_id: str) -> None:
    lobby = LOBBIES.get(lobby_id)
    origin = ws.headers.get("origin")
    if origin is not None and origin not in origins:
        await ws.close(code=1008, reason="origin_not_allowed")
        return
    if lobby is None or is_expired(lobby):
        await ws.accept()
        await ws.close(code=1008, reason="party_ended")
        return
    lobby.pending += 1
    try:
        await ws.accept()
        raw_auth = await asyncio.wait_for(ws.receive_text(), timeout=5)
        auth = json.loads(raw_auth) if len(raw_auth.encode("utf-8")) <= 16384 else None
    except WebSocketDisconnect:
        return
    except (TimeoutError, json.JSONDecodeError, KeyError):
        await ws.close(code=1008, reason="invalid_auth")
        return
    finally:
        lobby.pending -= 1
    if not isinstance(auth, dict):
        await ws.close(code=1008, reason="invalid_auth")
        return
    host_token = auth.get("host_token")
    if host_token is not None and (
        not isinstance(host_token, str)
        or not secrets.compare_digest(host_token.encode(), lobby.host_token.encode())
    ):
        await ws.close(code=1008, reason="invalid_host_token")
        return
    member_token = auth.get("member_token")
    if host_token is None and (
        not isinstance(member_token, str) or not re.fullmatch(r"[a-f0-9]{64}", member_token)
    ):
        await ws.close(code=1008, reason="invalid_member_token")
        return
    telegram_user = None
    if auth.get("telegram_init_data") is not None:
        try:
            telegram_user = verify_telegram(auth["telegram_init_data"])
        except (ValueError, TypeError):
            await ws.close(code=1008, reason="invalid_telegram_auth")
            return
    if host_token is not None and lobby.host_telegram_id is not None and (
        telegram_user is None or telegram_user.id != lobby.host_telegram_id
    ):
        await ws.close(code=1008, reason="host_telegram_mismatch")
        return
    owner_key = host_token if host_token is not None else (
        f"telegram:{telegram_user.id}" if telegram_user else member_token
    )
    assert isinstance(owner_key, str)
    owner_id = lobby.owner_ids.setdefault(owner_key, uuid4().hex)
    if telegram_user:
        lobby.profiles[owner_id] = telegram_user.public()
    elif host_token is not None:
        lobby.profiles.setdefault(owner_id, {"name": "Хост", "username": None, "photo_url": None})
    else:
        lobby.profiles.setdefault(owner_id, {"name": "Гость", "username": None, "photo_url": None})
    async with lobby.claim_lock:
        if LOBBIES.get(lobby_id) is not lobby or is_expired(lobby):
            await ws.close(code=1008, reason="party_ended")
            return
        if host_token is not None and not secrets.compare_digest(host_token, lobby.host_token):
            await ws.close(code=1008, reason="invalid_host_token")
            return
        promote = (host_token is None and lobby.vacated and lobby.ever_shared
                   and not lobby.connections)
        if promote:
            lobby.owner_ids.pop(lobby.host_token, None)
            lobby.host_token = secrets.token_urlsafe(32)
            lobby.owner_ids[lobby.host_token] = owner_id
            lobby.host_telegram_id = telegram_user.id if telegram_user else None
        if (host_token is not None and isinstance(member_token, str)
                and re.fullmatch(r"[a-f0-9]{64}", member_token)):
            lobby.owner_ids[member_token] = owner_id
        member_id = await manager.connect(lobby_id, ws, main=host_token is not None or promote,
                                          owner_id=owner_id)
        if member_id is not None:
            lobby.member_telegram[member_id] = telegram_user.id if telegram_user else None
        if promote and member_id is not None:
            await manager.send(ws, {"type": "host_granted", "host_token": lobby.host_token})
    if member_id is None:
        return
    relay_window, relay_messages = monotonic(), 0
    try:
        while True:
            incoming = await ws.receive()
            if incoming["type"] == "websocket.disconnect":
                break
            if member_id not in lobby.connections:
                break
            raw = incoming.get("text")
            if raw is None or len(raw.encode("utf-8")) > 16384:
                await ws.close(code=1009 if raw is not None else 1003)
                break
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await manager.send(ws, {"type": "error", "error": "invalid_json"})
                continue
            if not isinstance(message, dict):
                await manager.send(ws, {"type": "error", "error": "invalid_message"})
                continue
            action = message.get("action")
            if isinstance(action, str) and action.startswith("relay_"):
                now = monotonic()
                if now - relay_window >= 10:
                    relay_window, relay_messages = now, 0
                relay_messages += 1
                if relay_messages > 240:
                    await manager.send(ws, {"type": "error", "error": "Слишком много команд трансляции"})
                    continue
                if action == "relay_request":
                    lobby.relay_requests.add(member_id)
                    if member_id == lobby.main_id:
                        reset_relay(lobby)
                        lobby.relay_source = member_id
                        lobby.relay_requests.discard(member_id)
                elif action == "relay_grant":
                    target = message.get("member_id")
                    if (member_id != lobby.main_id or not isinstance(target, str) or
                            target not in lobby.relay_requests or target not in lobby.connections):
                        await manager.send(ws, {"type": "error", "error": "Транслятора назначает хост"})
                        continue
                    reset_relay(lobby)
                    lobby.relay_source = target
                    lobby.relay_requests.discard(target)
                    lobby.playback = None
                elif action == "relay_stop":
                    lobby.relay_requests.discard(member_id)
                    if member_id in (lobby.main_id, lobby.relay_source):
                        reset_relay(lobby)
                elif action == "relay_join":
                    allowed = member_id == lobby.main_id or member_id in lobby.listener_tokens.values()
                    if (not allowed or not lobby.relay_source or member_id == lobby.relay_source or
                            not lobby.queue_items or not lobby.queue_items[0].track_id.startswith("spotify_")):
                        await manager.send(ws, {"type": "error", "error": "Включите «Слушать вместе» для трансляции"})
                        continue
                    if member_id not in lobby.relay_listeners and len(lobby.relay_listeners) >= 20:
                        await manager.send(ws, {"type": "error", "error": "В трансляции уже 20 слушателей"})
                        continue
                    lobby.relay_listeners.add(member_id)
                    await manager.send(ws, {"type": "relay_access", "generation": lobby.relay_generation,
                                            "ice_servers": relay_ice(lobby_id, member_id)})
                    await manager.send(lobby.connections[lobby.relay_source], {
                        "type": "relay_access", "generation": lobby.relay_generation,
                        "ice_servers": relay_ice(lobby_id, lobby.relay_source)})
                elif action == "relay_leave":
                    lobby.relay_listeners.discard(member_id)
                elif action == "relay_signal":
                    target = message.get("target")
                    kind, data = message.get("kind"), message.get("data")
                    source = lobby.relay_source
                    sending = member_id == source and target in lobby.relay_listeners if isinstance(target, str) else False
                    receiving = target == source and member_id in lobby.relay_listeners
                    if (type(message.get("generation")) is not int or
                            message["generation"] != lobby.relay_generation or not source or
                            not isinstance(target, str) or target not in lobby.connections or
                            not (sending or receiving) or kind not in ("offer", "answer", "ice") or
                            kind == "offer" and not sending or kind == "answer" and not receiving):
                        await manager.send(ws, {"type": "error", "error": "Недействительное соединение трансляции"})
                        continue
                    if kind in ("offer", "answer"):
                        if (not isinstance(data, str) or len(data) > 12000 or
                                "m=audio " not in data or re.search(r"(?:^|\n)m=(?!audio )", data)):
                            await manager.send(ws, {"type": "error", "error": "Разрешена только аудиотрансляция"})
                            continue
                    elif (not isinstance(data, dict) or not isinstance(data.get("candidate"), str) or
                          len(data["candidate"]) > 2048 or data.get("sdpMLineIndex") not in (0, None) or
                          data.get("sdpMid") is not None and
                          (not isinstance(data["sdpMid"], str) or len(data["sdpMid"]) > 32)):
                        await manager.send(ws, {"type": "error", "error": "Некорректный ICE-кандидат"})
                        continue
                    await manager.send(lobby.connections[target], {"type": "relay_signal", "from": member_id,
                        "generation": lobby.relay_generation, "kind": kind, "data": data})
                    continue
                elif action == "relay_control":
                    source = lobby.relay_source
                    if (member_id != lobby.main_id or not source or source == member_id or
                            message.get("expected_revision") != lobby.playback_revision or
                            not lobby.queue_items or message.get("item_id") != lobby.queue_items[0].id or
                            not lobby.queue_items[0].track_id.startswith("spotify_") or
                            message.get("command") not in ("play", "pause", "seek")):
                        await manager.send(ws, {"type": "error", "error": "Управление доступно только хосту"})
                        continue
                    position = message.get("position", 0)
                    if type(position) not in (int, float) or not 0 <= position <= MAX_TRACK_SECONDS:
                        continue
                    await manager.send(lobby.connections[source], {"type": "relay_control",
                        "item_id": message["item_id"], "revision": lobby.playback_revision,
                        "command": message["command"], "position": position})
                    continue
                else:
                    await manager.send(ws, {"type": "error", "error": "Неизвестная команда трансляции"})
                    continue
                await manager.broadcast(lobby_id)
                continue
            if message.get("action") == "transfer_host":
                target_id = message.get("member_id")
                target = lobby.connections.get(target_id) if isinstance(target_id, str) else None
                if member_id != lobby.main_id or not target or target_id == member_id:
                    await manager.send(ws, {"type": "error", "error": "invalid_host_transfer"})
                    continue
                old_token, old_telegram = lobby.host_token, lobby.host_telegram_id
                target_owner = lobby.member_owners[target_id]
                new_token = secrets.token_urlsafe(32)
                lobby.host_token = new_token
                lobby.host_telegram_id = lobby.member_telegram.get(target_id)
                lobby.owner_ids[new_token] = target_owner
                lobby.main_id = target_id
                if not await manager.send(target, {"type": "host_granted", "host_token": new_token}):
                    lobby.host_token, lobby.host_telegram_id = old_token, old_telegram
                    lobby.main_id = member_id
                    lobby.owner_ids.pop(new_token, None)
                    await manager.send(ws, {"type": "error", "error": "host_transfer_failed"})
                    continue
                lobby.owner_ids.pop(old_token, None)
                reset_relay(lobby)
                await manager.send(ws, {"type": "host_revoked"})
                await manager.broadcast(lobby_id)
                continue
            elif message.get("action") == "listen_together":
                enabled = message.get("enabled")
                if type(enabled) is not bool or member_id == lobby.main_id:
                    await manager.send(ws, {"type": "error", "error": "invalid_listen_mode"})
                    continue
                for token, mid in list(lobby.listener_tokens.items()):
                    if mid == member_id:
                        del lobby.listener_tokens[token]
                token = secrets.token_urlsafe(32) if enabled else ""
                if enabled:
                    lobby.listener_tokens[token] = member_id
                else:
                    lobby.relay_listeners.discard(member_id)
                await manager.send(ws, {"type": "listener_access", "enabled": enabled,
                                       "token": token, "server_time": time() * 1000,
                                       "host_online": lobby.main_id is not None,
                                       "playback": playback_snapshot(lobby)})
                if lobby.relay_source:
                    await manager.broadcast(lobby_id)
                continue
            elif message.get("action") == "playback_ping":
                stamp = message.get("client_time")
                if type(stamp) in (int, float) and 0 <= stamp <= 1e15:
                    await manager.send(ws, {"type": "playback_clock", "client_time": stamp,
                                           "server_time": time() * 1000})
                continue
            elif message.get("action") == "playback_control":
                state = playback_snapshot(lobby)
                command = message.get("command")
                if member_id != lobby.main_id:
                    await manager.send(ws, {"type": "error", "error": "main_only"})
                    continue
                if (not server_head(lobby) or not state or state.get("authority") != "server" or
                        message.get("item_id") != state["item_id"] or type(message.get("expected_revision")) is not int or
                        message["expected_revision"] != lobby.playback_revision or command not in ("play", "pause", "seek")):
                    await manager.send(ws, {"type": "error", "error": "stale_playback_control"})
                    continue
                position = message.get("position") if command == "seek" else state["position"]
                if type(position) not in (int, float) or not math.isfinite(position) or not 0 <= position <= MAX_TRACK_SECONDS:
                    await manager.send(ws, {"type": "error", "error": "invalid_position"})
                    continue
                set_room_clock(lobby, position, state["duration"], command == "pause" if command != "seek" else state["paused"])
                await manager.broadcast(lobby_id)
                continue
            elif message.get("action") == "playback_sync":
                if server_head(lobby):
                    continue
                spotify_relay = bool(lobby.relay_source and lobby.queue_items and
                                     lobby.queue_items[0].track_id.startswith("spotify_"))
                leader = lobby.relay_source if spotify_relay else lobby.main_id
                if member_id != leader:
                    if member_id == lobby.main_id and spotify_relay:
                        continue
                    await manager.send(ws, {"type": "error", "error": "main_only"})
                    continue
                position, duration = message.get("position"), message.get("duration", 0)
                if (not lobby.queue_items or message.get("item_id") != lobby.queue_items[0].id or
                        type(message.get("expected_revision")) is not int or
                        message["expected_revision"] != lobby.playback_revision or
                        type(message.get("paused")) is not bool or
                        type(position) not in (int, float) or
                        not 0 <= position <= 86400 or type(duration) not in (int, float) or
                        not 0 <= duration <= 86400):
                    await manager.send(ws, {"type": "error", "error": "stale_playback_sync"})
                    continue
                previous = playback_snapshot(lobby)
                control = (previous is None or previous["paused"] != message["paused"] or
                           message.get("seek") is True)
                if not control and monotonic() - lobby.last_sync < .05:
                    continue
                lobby.last_sync = monotonic()
                lobby.sync_serial += 1
                lobby.playback = {"item_id": message["item_id"],
                                  "revision": lobby.playback_revision,
                                  "position": position, "duration": duration,
                                  "paused": message["paused"], "server_time": time() * 1000,
                                  "serial": lobby.sync_serial,
                                  "transport_serial": lobby.sync_serial if control else
                                  previous.get("transport_serial", previous["serial"])}
                if control:
                    mark_rooms_dirty()
                await manager.sync_listeners(lobby_id)
                continue
            elif message.get("action") == "add_track":
                track_id = message.get("track_id")
                if not isinstance(track_id, str) or not re.fullmatch(
                    r"(?:yandex_[0-9]{1,20}|sc_[0-9]{1,20}|spotify_[A-Za-z0-9]{22}|apple_[0-9]{1,20}|"
                    r"vk_-?[0-9]{1,20}_[0-9]{1,20}|[A-Za-z0-9_-]{11})", track_id
                ):
                    await manager.send(ws, {"type": "error", "error": "invalid_track_id"})
                    continue
                if len(lobby.queue_items) >= 500:
                    await manager.send(ws, {"type": "error", "error": "queue_full"})
                    continue
                if re.fullmatch(r"[A-Za-z0-9_-]{11}", track_id):
                    from app.main import YOUTUBE_DURATION_CACHE

                    if YOUTUBE_DURATION_CACHE.get(track_id, 0) > MAX_TRACK_SECONDS:
                        await manager.send(ws, {"type": "error", "error": "Трек длиннее 12 минут"})
                        continue
                yandex_handle = (
                    message.get("yandex_session") if track_id.startswith("yandex_") else None
                )
                if track_id.startswith("yandex_") and (
                    not isinstance(yandex_handle, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", yandex_handle)
                ):
                    await manager.send(ws, {"type": "error", "error": "yandex_session_required"})
                    continue
                provider_handle = None
                provider_title = None
                canonical_url = None
                duration_ms = 0
                if track_id.startswith(("sc_", "vk_", "spotify_", "apple_")):
                    from app.providers.music import validate_track

                    provider_handle = message.get("provider_session")
                    if not isinstance(provider_handle, str) or not re.fullmatch(
                        r"[A-Za-z0-9_-]{32,128}", provider_handle
                    ):
                        await manager.send(ws, {"type": "error", "error": "Подключите музыкальный сервис заново"})
                        continue
                    provider = ("apple" if track_id.startswith("apple_") else
                                "spotify" if track_id.startswith("spotify_") else
                                "soundcloud" if track_id.startswith("sc_") else "vk")
                    provider_id = track_id.split("_", 1)[1]
                    try:
                        metadata = validate_track(provider, provider_handle, provider_id)
                    except HTTPException as exc:
                        await manager.send(ws, {"type": "error", "error": str(exc.detail)})
                        continue
                    provider_title = f"{metadata['title']} — {metadata['artist']}"[:200]
                    canonical_url = str(metadata["canonical_url"])
                    value = metadata.get("duration_ms")
                    if type(value) is int and 0 < value <= MAX_TRACK_SECONDS * 1000:
                        duration_ms = value
                lobby.queue_items.append(QueueItem(uuid4().hex, track_id, owner_id, yandex_handle,
                                                   provider_session=provider_handle,
                                                   canonical_url=canonical_url, duration_ms=duration_ms,
                                                   added_order=lobby.queue_revision))
                rank_queue(lobby)
                lobby.queue_revision += 1
                title = provider_title or message.get("title")
                if (
                    isinstance(title, str)
                    and 0 < len(title) <= 200
                ):
                    lobby.track_titles[track_id] = title
            elif message.get("action") == "vote_track":
                value = message.get("vote")
                item = next((item for item in lobby.queue_items if item.id == message.get("item_id")), None)
                if item is None or type(value) is not int or value not in (-1, 0, 1):
                    await manager.send(ws, {"type": "error", "error": "invalid_track_vote"})
                    continue
                if value:
                    item.votes[owner_id] = value
                else:
                    item.votes.pop(owner_id, None)
                rank_queue(lobby)
                lobby.queue_revision += 1
            elif message.get("action") == "set_yandex_session":
                yandex_handle = message.get("yandex_session")
                if not isinstance(yandex_handle, str) or not re.fullmatch(
                    r"[A-Za-z0-9_-]{32,128}", yandex_handle
                ):
                    await manager.send(ws, {"type": "error", "error": "invalid_yandex_session"})
                    continue
                changed = False
                for item in lobby.queue_items:
                    if (item.owner_id == owner_id and item.track_id.startswith("yandex_")
                            and item.yandex_session != yandex_handle):
                        item.yandex_session = yandex_handle
                        item.access_revision += 1
                        changed = True
                if not changed:
                    continue
            elif message.get("action") == "set_music_session":
                from app.providers.music import validate_track

                provider_name = message.get("provider")
                provider_handle = message.get("provider_session")
                if provider_name not in ("soundcloud", "vk") or not isinstance(provider_handle, str):
                    await manager.send(ws, {"type": "error", "error": "invalid_music_session"})
                    continue
                prefix = "sc_" if provider_name == "soundcloud" else "vk_"
                selected = [item for item in lobby.queue_items
                            if item.owner_id == owner_id and item.track_id.startswith(prefix)]
                try:
                    for item in selected:
                        validate_track(str(provider_name), provider_handle, item.track_id[3:])
                except HTTPException as exc:
                    await manager.send(ws, {"type": "error", "error": str(exc.detail)})
                    continue
                changed = False
                for item in selected:
                    if item.provider_session != provider_handle:
                        item.provider_session = provider_handle
                        item.access_revision += 1
                        changed = True
                if not changed:
                    continue
            elif message.get("action") == "skip_track":
                if server_head(lobby) and message.get("ended") is True:
                    continue
                source_ended = (member_id == lobby.relay_source and lobby.queue_items and
                                lobby.queue_items[0].track_id.startswith("spotify_") and
                                message.get("ended") is True and
                                message.get("item_id") == lobby.queue_items[0].id)
                if member_id != lobby.main_id and not source_ended:
                    await manager.send(ws, {"type": "error", "error": "main_only"})
                    continue
                if type(message.get("expected_revision")) is not int or (
                    message["expected_revision"] != lobby.playback_revision
                ):
                    await manager.send(ws, {"type": "error", "error": "stale_playback"})
                    await manager.broadcast(lobby_id)
                    continue
                if not lobby.queue_items:
                    await manager.send(ws, {"type": "error", "error": "queue_empty"})
                    continue
                advance_room(lobby)
            elif message.get("action") in ("remove_track", "move_track"):
                if type(message.get("expected_queue_revision")) is not int or (
                    message["expected_queue_revision"] != lobby.queue_revision
                ):
                    await manager.send(ws, {"type": "error", "error": "stale_queue"})
                    await manager.broadcast(lobby_id)
                    continue
                item_id = message.get("item_id")
                index = next((i for i, item in enumerate(lobby.queue_items)
                              if item.id == item_id), -1)
                if index < 0:
                    await manager.send(ws, {"type": "error", "error": "track_not_queued"})
                    continue
                if message["action"] == "remove_track":
                    if member_id != lobby.main_id and lobby.queue_items[index].owner_id != owner_id:
                        await manager.send(ws, {"type": "error", "error": "not_track_owner"})
                        continue
                    removed = lobby.queue_items.pop(index)
                    if not any(item.track_id == removed.track_id for item in lobby.queue_items):
                        lobby.track_titles.pop(removed.track_id, None)
                    if index == 0:
                        lobby.playback_revision += 1
                        lobby.failed_at = 0
                        lobby.playback_error = ""
                        lobby.server_offset = 0
                else:
                    if member_id != lobby.main_id:
                        await manager.send(ws, {"type": "error", "error": "main_only"})
                        continue
                    destination = message.get("to_index")
                    if (
                        index == 0
                        or
                        type(destination) is not int
                        or not 1 <= destination < len(lobby.queue_items)
                    ):
                        await manager.send(ws, {"type": "error", "error": "invalid_position"})
                        continue
                    if destination == index:
                        await manager.broadcast(lobby_id)
                        continue
                    lobby.queue_items.insert(destination, lobby.queue_items.pop(index))
                lobby.queue_revision += 1
            else:
                await manager.send(ws, {"type": "error", "error": "invalid_message"})
                continue
            mark_rooms_dirty()
            await manager.broadcast(lobby_id)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        manager.disconnect(lobby_id, member_id)
        await manager.broadcast(lobby_id)
