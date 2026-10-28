import importlib
from types import SimpleNamespace as Item

import pytest
from fastapi.testclient import TestClient
from yandex_music.exceptions import TimedOutError

main = importlib.import_module("app.main")


@pytest.fixture(autouse=True)
def clear_sessions():
    main.DEVICE_LOGINS.clear()
    main.YANDEX_SESSIONS.clear()
    yield
    main.DEVICE_LOGINS.clear()
    main.YANDEX_SESSIONS.clear()


def test_mixed_likes_do_not_reject_account_and_pagination_uses_source_count(caplog):
    requested = []
    likes = [Item(track_id=str(index)) for index in range(51)]
    records = [
        Item(id=123, title="Song", artists=[Item(name="Artist")]),
        Item(id="456:789", title="Album song", artists=[]),
        Item(
            id="ugc-upload-uuid",
            title=None,
            artists=[Item(name=None)],
            meta_data=Item(title="My upload", artist="My artist"),
        ),
        Item(id="789", title=None, artists=[Item(name=None), Item(name="  Singer ")]),
        None,
    ]

    def tracks(ids, **kwargs):
        requested.extend(ids)
        return records

    client = Item(users_likes_tracks=lambda **kwargs: Item(tracks=likes), tracks=tracks)
    page = main.liked_page(client, 0)
    assert [track.id for track in page.tracks] == ["123", "456", "ugc-upload-uuid", "789"]
    assert [track.playable for track in page.tracks] == [True, True, False, True]
    assert page.tracks[2].title == "My upload"
    assert page.tracks[2].artist == "My artist"
    assert page.tracks[3].title == "Без названия"
    assert page.tracks[3].artist == "Singer"
    assert page.next_offset == 50 and page.has_more
    assert len(requested) == 50 and page.unsupported_count == 2
    assert "Yandex likes normalized" in caplog.text
    assert "My upload" not in caplog.text and "ugc-upload-uuid" not in caplog.text


def test_empty_likes_are_a_valid_connected_library():
    client = Item(users_likes_tracks=lambda **kwargs: Item(tracks=[]))
    page = main.liked_page(client, 0)
    assert page.authenticated and page.tracks == []
    assert not page.has_more and page.next_offset == 0


@pytest.mark.parametrize("library_fails", [False, True])
def test_device_login_preserves_authorization_for_unusual_or_temporarily_failed_library(
    monkeypatch,
    library_fails,
):
    attempts = 0

    def likes(**kwargs):
        nonlocal attempts
        attempts += 1
        if library_fails and attempts == 1:
            raise TimedOutError("provider unavailable")
        return Item(tracks=[Item(track_id="123"), Item(track_id="private-upload")])

    client = Item(
        users_likes_tracks=likes,
        tracks=lambda *args, **kwargs: [
            Item(id="123", title="Song", artists=[Item(name="Artist")]),
            Item(id="private-upload", title="Upload", artists=[]),
        ],
    )

    class AuthClient:
        async def request_device_code(self):
            return Item(
                device_code="device",
                user_code="CODE",
                verification_url="https://ya.ru",
                expires_in=600,
                interval=1,
            )

        async def poll_device_token(self, code):
            assert code == "device"
            return Item(access_token="private-provider-token", expires_in=3600)

    monkeypatch.setattr(main, "ClientAsync", AuthClient)
    monkeypatch.setattr(main, "Client", lambda token: Item(init=lambda **kwargs: client))
    with TestClient(main.app) as http:
        login = http.post("/api/yandex/login").json()
        authorized = http.get(f"/api/yandex/login/{login['login_id']}")
        assert authorized.status_code == 200
        data = authorized.json()
        assert data["authenticated"] is True and data["status"] == "authorized"
        assert "private-provider-token" not in str(data)
        assert bool(data["library_error"]) == library_fails
        handle = data["session"]
        assert main.YANDEX_SESSIONS[handle].client is client
        refreshed = http.get("/api/yandex/tracks", headers={"X-Yandex-Session": handle})
        assert refreshed.status_code == 200
        assert refreshed.json()["tracks"][0]["id"] == "123"
        assert refreshed.json()["tracks"][1]["playable"] is False
