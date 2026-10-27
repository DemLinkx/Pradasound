import hashlib
import hmac
import importlib
import json
from time import monotonic, time
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import lobby
from app.main import app


def signed_data(
    user_id: int, *, name: str = "Аня", photo: str = "https://example.org/avatar.jpg"
) -> str:
    fields = {
        "auth_date": str(int(time())),
        "user": json.dumps({
            "id": user_id, "first_name": name, "username": f"guest_{user_id}",
            "photo_url": photo,
        }, ensure_ascii=False, separators=(",", ":")),
    }
    secret = hmac.new(b"WebAppData", b"test_bot_token", hashlib.sha256).digest()
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture(autouse=True)
def telegram_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lobby, "telegram_bot_token", "test_bot_token")
    lobby.LOBBIES.clear()
    lobby.USERS.clear()


def test_telegram_signature_and_tampering() -> None:
    init_data = signed_data(12345)
    user = lobby.verify_telegram(init_data)
    assert (user.id, user.username, user.photo_url) == (12345, "guest_12345", "https://example.org/avatar.jpg")
    with pytest.raises(ValueError, match="signature"):
        lobby.verify_telegram(init_data.replace("guest_12345", "guest_99999"))


def test_telegram_webhook_requires_secret_and_answers_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lobby, "telegram_webhook_secret", "test-webhook-secret")
    monkeypatch.setattr(lobby, "telegram_mini_app_url", "https://sound.example")
    update = {"message": {"chat": {"id": 12345, "type": "private"}, "text": "/start"}}
    with TestClient(app) as client:
        route = "/api/lobby/telegram/webhook"
        assert client.post(route, json=update).status_code == 403
        response = client.post(
            route, json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["method"] == "sendMessage"
        assert result["chat_id"] == 12345
        assert result["reply_markup"]["inline_keyboard"][0][0]["web_app"] == {
            "url": "https://sound.example"
        }


def test_telegram_host_and_queue_attribution() -> None:
    host = signed_data(12345)
    guest = signed_data(67890, name="Борис")
    with TestClient(app) as client:
        me = client.post("/api/lobby/telegram/me", json={"init_data": host})
        assert me.status_code == 200
        assert me.json()["profile"]["username"] == "guest_12345"
        created = client.post("/api/lobby/create", json={"init_data": host})
        assert created.status_code == 201
        room = created.json()
        with client.websocket_connect(f"/api/lobby/{room['lobby_id']}/ws") as main:
            main.send_json({"host_token": room["host_token"], "telegram_init_data": host})
            assert main.receive_json()["type"] == "identity"
            initial = main.receive_json()
            assert initial["main_profile"]["username"] == "guest_12345"
            with client.websocket_connect(f"/api/lobby/{room['lobby_id']}/ws") as member:
                member.send_json({"member_token": "a" * 64, "telegram_init_data": guest})
                identity = member.receive_json()
                assert identity["type"] == "identity"
                main.receive_json()
                state = member.receive_json()
                assert state["main_profile"]["photo_url"] == "https://example.org/avatar.jpg"
                member.send_json({
                    "action": "add_track", "track_id": "dQw4w9WgXcQ", "title": "Песня"
                })
                updated = member.receive_json()
                main_updated = main.receive_json()
                assert updated["queue_items"][0]["owner_id"] == identity["owner_id"]
                assert main_updated["profiles"][identity["owner_id"]]["username"] == "guest_67890"


def test_host_token_cannot_be_used_by_another_telegram_account() -> None:
    with TestClient(app) as client:
        room = client.post("/api/lobby/create", json={"init_data": signed_data(12345)}).json()
        with client.websocket_connect(f"/api/lobby/{room['lobby_id']}/ws") as ws:
            ws.send_json({
                "host_token": room["host_token"], "telegram_init_data": signed_data(67890)
            })
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1008


def test_malformed_websocket_telegram_data_is_rejected() -> None:
    with TestClient(app) as client:
        room = client.post("/api/lobby/create").json()
        with client.websocket_connect(f"/api/lobby/{room['lobby_id']}/ws") as ws:
            ws.send_json({"member_token": "a" * 64, "telegram_init_data": []})
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1008


def test_yandex_queue_uses_each_authors_session_without_exposing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = signed_data(12345)
    first = signed_data(67890, name="Борис")
    second = signed_data(98765, name="Вера")
    first_handle = "a" * 43
    second_handle = "b" * 43
    resolved: list[tuple[str, str]] = []
    main_module = importlib.import_module("app.main")

    def fake_stream(track_id: str, x_yandex_session: str) -> dict[str, str]:
        resolved.append((track_id, x_yandex_session))
        return {"url": f"https://music.example/{len(resolved)}.mp3"}

    monkeypatch.setattr(main_module, "yandex_stream", fake_stream)
    with TestClient(app) as client:
        room = client.post("/api/lobby/create", json={"init_data": host}).json()
        lobby_id = room["lobby_id"]
        with client.websocket_connect(f"/api/lobby/{lobby_id}/ws") as main:
            main.send_json({"host_token": room["host_token"], "telegram_init_data": host})
            main.receive_json()
            main.receive_json()
            with client.websocket_connect(f"/api/lobby/{lobby_id}/ws") as guest_a:
                guest_a.send_json({"member_token": "a" * 64, "telegram_init_data": first})
                first_owner = guest_a.receive_json()["owner_id"]
                guest_a.receive_json()
                main.receive_json()
                with client.websocket_connect(f"/api/lobby/{lobby_id}/ws") as guest_b:
                    guest_b.send_json({"member_token": "b" * 64, "telegram_init_data": second})
                    guest_b.receive_json()
                    guest_b.receive_json()
                    main.receive_json()
                    guest_a.receive_json()

                    guest_a.send_json({"action": "add_track", "track_id": "yandex_123"})
                    assert guest_a.receive_json() == {
                        "type": "error", "error": "yandex_session_required"
                    }
                    guest_a.send_json({
                        "action": "add_track", "track_id": "yandex_123",
                        "yandex_session": first_handle,
                    })
                    first_state = main.receive_json()
                    guest_a.receive_json()
                    guest_b.receive_json()
                    guest_b.send_json({
                        "action": "add_track", "track_id": "yandex_123",
                        "yandex_session": second_handle,
                    })
                    state = main.receive_json()
                    guest_a.receive_json()
                    guest_b.receive_json()
                    assert len(state["queue_items"]) == 2
                    assert state["queue_items"][0]["owner_id"] == first_owner
                    assert first_state["queue_items"][0]["access_revision"] == 0
                    assert first_handle not in json.dumps(state)
                    assert second_handle not in json.dumps(state)
                    items = lobby.LOBBIES[lobby_id].queue_items
                    assert [item.yandex_session for item in items] == [first_handle, second_handle]

                    endpoint = f"/api/yandex/queue/{lobby_id}"
                    assert client.get(f"{endpoint}/{items[0].id}/stream").status_code == 403
                    headers = {"X-Host-Token": room["host_token"]}
                    for item in items:
                        response = client.get(f"{endpoint}/{item.id}/stream", headers=headers)
                        assert response.status_code == 200
                        assert set(response.json()) == {"url"}
                    assert resolved == [("123", first_handle), ("123", second_handle)]

                    new_handle = "c" * 43
                    guest_a.send_json({
                        "action": "set_yandex_session", "yandex_session": new_handle,
                    })
                    refreshed = main.receive_json()
                    guest_a.receive_json()
                    guest_b.receive_json()
                    assert [item.yandex_session for item in items] == [new_handle, second_handle]
                    assert [item["access_revision"] for item in refreshed["queue_items"]] == [1, 0]
                    assert new_handle not in json.dumps(refreshed)
                    assert client.get(
                        f"{endpoint}/{items[0].id}/stream", headers=headers
                    ).status_code == 200
                    assert resolved[-1] == ("123", new_handle)


def test_expired_yandex_author_session_is_reported_to_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = importlib.import_module("app.main")

    def expired_stream(track_id: str, x_yandex_session: str) -> dict[str, str]:
        raise HTTPException(status_code=401, detail="expired")

    monkeypatch.setattr(main_module, "yandex_stream", expired_stream)
    with TestClient(app) as client:
        room = client.post("/api/lobby/create").json()
        lobby_id = room["lobby_id"]
        lobby.LOBBIES[lobby_id].queue_items.append(
            lobby.QueueItem("queued", "yandex_123", "guest", "d" * 43)
        )
        response = client.get(
            f"/api/yandex/queue/{lobby_id}/queued/stream",
            headers={"X-Host-Token": room["host_token"]},
        )
        assert response.status_code == 401
        assert "Автору трека" in response.json()["detail"]


def test_yandex_queue_resolves_mp3_with_author_client() -> None:
    main_module = importlib.import_module("app.main")
    handle = "member-session"

    class Mp3Variant:
        codec = "mp3"
        preview = False
        bitrate_in_kbps = 320

        def get_direct_link(self, timeout: int) -> str:
            assert timeout == 10
            return "https://music.example/author-track.mp3"

    class AuthorTrack:
        def get_download_info(self, timeout: int) -> list[Mp3Variant]:
            assert timeout == 10
            return [Mp3Variant()]

    class AuthorClient:
        def tracks(self, ids: list[str], timeout: int) -> list[AuthorTrack]:
            assert ids == ["123"]
            assert timeout == 10
            return [AuthorTrack()]

    main_module.YANDEX_SESSIONS[handle] = main_module.YandexSession(
        AuthorClient(), monotonic() + 60
    )
    try:
        with TestClient(app) as client:
            room = client.post("/api/lobby/create").json()
            lobby_id = room["lobby_id"]
            lobby.LOBBIES[lobby_id].queue_items.append(
                lobby.QueueItem("queued", "yandex_123", "member", handle)
            )
            response = client.get(
                f"/api/yandex/queue/{lobby_id}/queued/stream",
                headers={"X-Host-Token": room["host_token"]},
            )
            assert response.status_code == 200
            assert response.json() == {"url": "https://music.example/author-track.mp3"}
    finally:
        main_module.YANDEX_SESSIONS.pop(handle, None)
