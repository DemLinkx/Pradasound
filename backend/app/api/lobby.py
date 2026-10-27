import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from time import monotonic, time
from urllib.parse import parse_qsl, urlparse
from uuid import uuid4

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.settings import Settings

router = APIRouter(prefix="/api/lobby", tags=["lobby"])
origins = Settings().cors_origins
telegram_bot_token = Settings().telegram_bot_token
telegram_webhook_secret = Settings().telegram_webhook_secret
telegram_mini_app_url = Settings().telegram_mini_app_url
telegram_proxy_url = Settings().telegram_proxy_url
TELEGRAM_STATUS: dict[str, object] = {"connected": False, "polls": 0}


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
    return {
        "method": "sendMessage",
        "chat_id": chat["id"],
        "text": (
            "Prada Sound — общая очередь музыки для вашей вечеринки. "
            "Откройте приложение, создайте комнату и пригласите друзей."
        ),
        "reply_markup": {"inline_keyboard": [[{
            "text": "Открыть Prada Sound",
            "web_app": {"url": telegram_mini_app_url},
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
    connections: dict[str, WebSocket] = field(default_factory=dict)
    last_empty: float = field(default_factory=monotonic)
    pending: int = 0
    broadcast_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


LOBBIES: dict[str, Lobby] = {}


class ConnectionManager:
    async def connect(self, lobby_id: str, ws: WebSocket, main: bool, owner_id: str) -> str | None:
        lobby = LOBBIES[lobby_id]
        if main and lobby.main_id is not None:
            previous_id = lobby.main_id
            previous = lobby.connections.pop(previous_id, None)
            lobby.member_owners.pop(previous_id, None)
            lobby.main_id = None
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
        if main:
            lobby.main_id = member_id
        await self.send(ws, {"type": "identity", "owner_id": owner_id})
        await self.broadcast(lobby_id)
        return member_id

    def disconnect(self, lobby_id: str, member_id: str) -> None:
        lobby = LOBBIES[lobby_id]
        lobby.connections.pop(member_id, None)
        lobby.member_owners.pop(member_id, None)
        if lobby.main_id == member_id:
            lobby.main_id = None
        host_owner = lobby.owner_ids.get(lobby.host_token)
        for owner_id in list(lobby.profiles):
            if (
                owner_id != host_owner
                and owner_id not in lobby.member_owners.values()
                and not any(item.owner_id == owner_id for item in lobby.queue_items)
            ):
                lobby.profiles.pop(owner_id, None)
        if not lobby.connections:
            lobby.last_empty = monotonic()

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
        async with LOBBIES[lobby_id].broadcast_lock:
            await self._broadcast(lobby_id)

    async def _broadcast(self, lobby_id: str) -> None:
        lobby = LOBBIES[lobby_id]
        while lobby.connections:
            clients = list(lobby.connections.items())
            state: dict[str, object] = {
                "type": "lobby_state",
                "lobby_id": lobby_id,
                "tracks_queue": [item.track_id for item in lobby.queue_items],
                "queue_items": [
                    {"id": item.id, "track_id": item.track_id, "owner_id": item.owner_id,
                     "access_revision": item.access_revision}
                    for item in lobby.queue_items
                ],
                "track_titles": dict(lobby.track_titles),
                "playback_revision": lobby.playback_revision,
                "queue_revision": lobby.queue_revision,
                "profiles": dict(lobby.profiles),
                "main_profile": lobby.profiles.get(lobby.owner_ids.get(lobby.host_token, "")),
                "members": [
                    {
                        "member_id": member_id,
                        "role": "Main" if member_id == lobby.main_id else "Member",
                        "owner_id": lobby.member_owners.get(member_id),
                    }
                    for member_id, _ in clients
                ],
            }
            sent = await asyncio.gather(*(self.send(ws, state) for _, ws in clients))
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
        if not lobby.connections and not lobby.pending and monotonic() - lobby.last_empty > 1800:
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
    return {"lobby_id": lobby_id, "host_token": host_token, "role": "Main"}


@router.websocket("/{lobby_id}/ws")
async def lobby_ws(ws: WebSocket, lobby_id: str) -> None:
    lobby = LOBBIES.get(lobby_id)
    if lobby is None:
        await ws.close(code=1008, reason="lobby_not_found")
        return
    origin = ws.headers.get("origin")
    if origin is not None and origin not in origins:
        await ws.close(code=1008, reason="origin_not_allowed")
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
    member_id = await manager.connect(lobby_id, ws, main=host_token is not None,
                                      owner_id=owner_id)
    if member_id is None:
        return
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
            if message.get("action") == "add_track":
                track_id = message.get("track_id")
                if not isinstance(track_id, str) or not re.fullmatch(
                    r"(?:yandex_[0-9]{1,20}|[A-Za-z0-9_-]{11})", track_id
                ):
                    await manager.send(ws, {"type": "error", "error": "invalid_track_id"})
                    continue
                if len(lobby.queue_items) >= 500:
                    await manager.send(ws, {"type": "error", "error": "queue_full"})
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
                lobby.queue_items.append(QueueItem(uuid4().hex, track_id, owner_id, yandex_handle))
                lobby.queue_revision += 1
                title = message.get("title")
                if (
                    isinstance(title, str)
                    and 0 < len(title) <= 200
                ):
                    lobby.track_titles[track_id] = title
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
            elif message.get("action") == "skip_track":
                if member_id != lobby.main_id:
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
                removed = lobby.queue_items.pop(0)
                if not any(item.track_id == removed.track_id for item in lobby.queue_items):
                    lobby.track_titles.pop(removed.track_id, None)
                lobby.playback_revision += 1
                lobby.queue_revision += 1
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
                    if lobby.queue_items[index].owner_id != owner_id:
                        await manager.send(ws, {"type": "error", "error": "not_track_owner"})
                        continue
                    removed = lobby.queue_items.pop(index)
                    if not any(item.track_id == removed.track_id for item in lobby.queue_items):
                        lobby.track_titles.pop(removed.track_id, None)
                    if index == 0:
                        lobby.playback_revision += 1
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
            await manager.broadcast(lobby_id)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        manager.disconnect(lobby_id, member_id)
        await manager.broadcast(lobby_id)
