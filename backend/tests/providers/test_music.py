import asyncio
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from time import monotonic
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.providers import music


@pytest.fixture(autouse=True)
def clear_state(monkeypatch):
    monkeypatch.delenv("SPOTIFY_NATIVE_HELPER", raising=False)
    monkeypatch.setattr(music.settings, "cors_origins", ["http://localhost:5173"])
    music.PENDING.clear()
    music.PENDING_DEVICES.clear()
    music.SESSIONS.clear()
    music.TICKETS.clear()
    music.RESOLVE_CACHE.clear()
    music.ROUTE_FAILURES.clear()
    yield
    music.PENDING.clear()
    music.PENDING_DEVICES.clear()
    music.SESSIONS.clear()
    music.TICKETS.clear()
    music.ROUTE_FAILURES.clear()


def test_native_spotify_library_pages_without_web_api(client, monkeypatch):
    monkeypatch.setenv("SPOTIFY_NATIVE_HELPER", "/reader")
    handle = music._store(
        "spotify", "access-secret", client_id=music.SPOTIFY_DEVICE_ID, proxy="socks5://music:1081"
    )
    received = []

    class Process:
        returncode = 0

        async def communicate(self, payload):
            received.append(json.loads(payload))
            return json.dumps(
                {
                    "tracks": [{"id": "a" * 22, "title": "Private song", "artist": "Singer"}],
                    "cursor": "next-private-page" if len(received) == 1 else "",
                    "account": "connected-account",
                    "stats": {"pages": 3, "items": 100, "removed": 70, "other": 29},
                }
            ).encode(), None

    async def spawn(*args, **kwargs):
        assert args == ("/reader",)
        assert kwargs["env"]["ALL_PROXY"] == "socks5://music:1081"
        assert kwargs["env"]["HTTPS_PROXY"] == "socks5://music:1081"
        return Process()

    monkeypatch.setattr(music.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        music, "_client", lambda *args: pytest.fail("Native import must not call Web API")
    )
    first = client.get("/api/music/spotify/tracks", headers={"X-Music-Session": handle})
    assert first.status_code == 200
    assert first.json()["authenticated"] is True
    assert first.json()["tracks"][0]["title"] == "Private song"
    assert first.json()["next_cursor"] == "50"
    assert first.json()["account_url"] == "https://open.spotify.com/user/connected-account"
    assert first.json()["empty_library"] is False
    assert "access-secret" not in first.text and "next-private-page" not in first.text
    second = client.get("/api/music/spotify/tracks?cursor=50", headers={"X-Music-Session": handle})
    assert second.status_code == 200 and second.json()["has_more"] is False
    assert received == [
        {"token": "access-secret", "cursor": ""},
        {"token": "access-secret", "cursor": "next-private-page"},
    ]
    assert (
        client.get(
            "/api/music/spotify/tracks?cursor=100", headers={"X-Music-Session": handle}
        ).status_code
        == 400
    )


def test_native_spotify_failure_is_not_an_empty_library(client, monkeypatch, caplog):
    monkeypatch.setenv("SPOTIFY_NATIVE_HELPER", "/reader")
    handle = music._store("spotify", "access-secret", client_id=music.SPOTIFY_DEVICE_ID)

    class Process:
        returncode = 0

        async def communicate(self, payload):
            return b'{"error":"collection","tracks":null,"cursor":""}', None

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr(music.asyncio, "create_subprocess_exec", spawn)
    response = client.get("/api/music/spotify/tracks", headers={"X-Music-Session": handle})
    assert response.status_code == 502
    assert "collection" in caplog.text and "access-secret" not in caplog.text


def test_native_spotify_timeout_stops_reader(client, monkeypatch):
    monkeypatch.setenv("SPOTIFY_NATIVE_HELPER", "/reader")
    handle = music._store("spotify", "access-secret", client_id=music.SPOTIFY_DEVICE_ID)
    stopped = []

    class Process:
        returncode = None

        async def communicate(self, payload):
            raise TimeoutError

        def kill(self):
            stopped.append(True)

        async def wait(self):
            self.returncode = -9

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr(music.asyncio, "create_subprocess_exec", spawn)
    response = client.get("/api/music/spotify/tracks", headers={"X-Music-Session": handle})
    assert response.status_code == 502 and stopped == [True]


def test_native_empty_library_is_explicit(client, monkeypatch):
    monkeypatch.setenv("SPOTIFY_NATIVE_HELPER", "/reader")
    handle = music._store("spotify", "access-secret", client_id=music.SPOTIFY_DEVICE_ID)

    class Process:
        returncode = 0

        async def communicate(self, payload):
            return b'{"tracks":[],"cursor":"","account":"owner","stats":{"context":true}}', None

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr(music.asyncio, "create_subprocess_exec", spawn)
    response = client.get("/api/music/spotify/tracks", headers={"X-Music-Session": handle})
    assert response.status_code == 200
    assert response.json()["empty_library"] is True
    assert response.json()["has_more"] is False
    assert response.json()["account_url"] == "https://open.spotify.com/user/owner"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(music.router)
    return TestClient(app)


def test_spotify_oauth_state_cookie_and_session_isolation(client, monkeypatch):
    monkeypatch.setattr(music, "_credentials", lambda provider: ("id", "secret"))

    def handler(request):
        if request.url.path.endswith("/api/token"):
            return httpx.Response(200, json={"access_token": "token", "refresh_token": "refresh"})
        if request.url.path.endswith("/me/tracks"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "track": {
                                "id": "spotify123",
                                "name": "Song",
                                "artists": [{"name": "Artist"}],
                            }
                        }
                    ],
                    "next": None,
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    uri = "http://localhost:5173/oauth/spotify"
    login = client.post("/api/music/spotify/login", json={"redirect_uri": uri})
    assert login.status_code == 200
    login_id = login.json()["login_id"]
    state = music.PENDING[login_id].state
    bad = client.post(
        "/api/music/spotify/callback", json={"login_id": login_id, "code": "code", "state": "wrong"}
    )
    assert bad.status_code == 400
    assert login_id not in music.PENDING

    login = client.post("/api/music/spotify/login", json={"redirect_uri": uri})
    login_id = login.json()["login_id"]
    state = music.PENDING[login_id].state
    callback = client.post(
        "/api/music/spotify/callback", json={"login_id": login_id, "code": "code", "state": state}
    )
    assert callback.status_code == 200
    handle = callback.json()["session"]
    assert "token" not in str(callback.json())
    tracks = client.get("/api/music/spotify/tracks", headers={"X-Music-Session": handle})
    assert tracks.status_code == 200
    assert tracks.json()["tracks"][0]["title"] == "Song"
    assert music.validate_track("spotify", handle, "spotify123")["artist"] == "Artist"
    with pytest.raises(HTTPException) as exc:
        music.validate_track("soundcloud", handle, "spotify123")
    assert exc.value.status_code == 401


def test_oauth_expiry_and_ssrf_rejected(client, monkeypatch):
    monkeypatch.setattr(music, "_credentials", lambda provider: ("id", "secret"))
    login = client.post(
        "/api/music/spotify/login", json={"redirect_uri": "https://evil.example/oauth/spotify"}
    )
    assert login.status_code == 400
    login = client.post(
        "/api/music/spotify/login", json={"redirect_uri": "http://localhost:5173/oauth/spotify"}
    )
    key = login.json()["login_id"]
    pending = music.PENDING[key]
    pending.expires = monotonic() - 1
    expired = client.post(
        "/api/music/spotify/callback",
        json={"login_id": key, "code": "code", "state": pending.state},
    )
    assert expired.status_code == 400
    assert not music._safe_media_url("https://sndcdn.com.evil.example/audio.mp3")
    assert not music._safe_media_url("https://127.0.0.1/audio.mp3")
    assert not music._safe_media_url("https://sndcdn.com:bad/audio.mp3")
    assert not music._safe_provider_url("https://soundcloud.com.evil.example/x/likes", "soundcloud")
    assert not music._safe_provider_url("https://soundcloud.com:bad/x/likes", "soundcloud")
    malicious_origin = client.post(
        "/api/music/spotify/login",
        json={"redirect_uri": "http://localhost:5173/oauth/spotify"},
        headers={"Origin": "https://evil.example"},
    )
    assert malicious_origin.status_code == 403


def test_refresh_is_serialized(monkeypatch):
    monkeypatch.setattr(music, "_credentials", lambda provider: ("id", "secret"))
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    session = music.MusicSession("spotify", "old", "refresh", monotonic() - 1, monotonic() + 3600)

    async def run():
        await asyncio.gather(music._refresh(session), music._refresh(session))

    asyncio.run(run())
    assert calls == 1
    assert session.access_token == "new"


def test_hls_manifest_only_maps_allowed_hosts():
    ticket = music.AudioTicket("https://host.soundcloud.cloud/master.m3u8", None, monotonic() + 60)
    playlist = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:5,\nseg.m4s\n'
    result = music._rewrite_manifest("ticket", ticket, ticket.source, playlist)
    assert "init.mp4" not in result
    assert "seg.m4s" not in result
    assert len(ticket.children) == 2
    with pytest.raises(HTTPException):
        music._rewrite_manifest(
            "ticket", ticket, ticket.source, "#EXTM3U\nhttps://evil.example/segment.ts"
        )
    with pytest.raises(HTTPException):
        music._rewrite_manifest(
            "ticket", ticket, ticket.source, '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"'
        )


def test_public_soundcloud_import_restores_page(client, monkeypatch):
    offsets = []

    async def extract(url, *, flat=False, offset=0):
        offsets.append(offset)
        assert url == "https://soundcloud.com/clalberg/likes"
        assert flat
        return {
            "entries": [
                {
                    "id": "12345",
                    "title": "A song",
                    "uploader": "An artist",
                    "url": "https://soundcloud.com/artist/a-song",
                }
            ]
        }

    monkeypatch.setattr(music, "_extract_async", extract)
    first = client.post(
        "/api/music/soundcloud/public", json={"url": "https://soundcloud.com/clalberg/likes"}
    )
    assert first.status_code == 200
    handle = first.json()["session"]
    assert first.json()["authenticated"] is False
    assert music.validate_track("soundcloud", handle, "12345")["title"] == "A song"
    next_page = client.get(
        "/api/music/soundcloud/tracks?cursor=50", headers={"X-Music-Session": handle}
    )
    assert next_page.status_code == 200
    assert offsets == [0, 50]
    mismatch = client.post(
        "/api/music/soundcloud/public",
        json={"url": "https://soundcloud.com/other/likes", "session": handle},
    )
    assert mismatch.status_code == 400


def test_vk_login_route_is_not_shadowed(client):
    response = client.post("/api/music/vk/login", json={"token": "x" * 30})
    assert response.status_code == 503


@pytest.mark.parametrize("token_input", [
    "x" * 30,
    "token= " + "x" * 30,
    "access_token=" + "x" * 30,
    "https://oauth.vk.com/blank.html#access_token=" + "x" * 30 + "&expires_in=0",
    "https://oauth.vk.ru/blank.html?access_token=" + "x" * 30,
])
def test_vk_public_authorization_url_and_direct_audio(client, monkeypatch, token_input):
    monkeypatch.setattr(music.settings, "vk_audio_enabled", True)
    original_value = music._value
    monkeypatch.setattr(
        music,
        "_value",
        lambda name, default="": (
            "VKAudioTest/1" if name == "vk_audio_user_agent" else original_value(name, default)
        ),
    )
    capability = client.get("/api/music/providers").json()["providers"]["vk"]
    assert capability["experimental_token"] is True
    assert capability["token_application"] == "Kate Mobile"
    url = urlparse(capability["token_authorization_url"])
    assert url.scheme == "https" and url.netloc == "oauth.vk.com"
    assert parse_qs(url.query) == {
        "client_id": ["2685278"],
        "scope": ["audio,offline"],
        "redirect_uri": ["https://oauth.vk.com/blank.html"],
        "display": ["page"],
        "response_type": ["token"],
    }
    assert "secret" not in url.query.lower()
    calls = []

    def handler(request):
        assert parse_qs(request.content.decode())["access_token"] == ["x" * 30]
        calls.append(
            (request.url.path, request.headers.get("User-Agent"), request.url.query.decode())
        )
        if request.url.path.endswith("audio.getById"):
            return httpx.Response(
                200, json={"response": [{"url": "https://cs1.vkuseraudio.net/full.mp3"}]}
            )
        return httpx.Response(
            200,
            json={
                "response": {
                    "count": 1,
                    "items": [
                        {
                            "owner_id": 1,
                            "id": 2,
                            "artist": "Artist",
                            "title": "Song",
                            "url": "https://cs1.vkuseraudio.net/full.mp3",
                        }
                    ],
                }
            },
        )

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    login = client.post("/api/music/vk/login", json={"token": token_input})
    assert login.status_code == 200
    assert login.json()["tracks"][0]["id"] == "1_2"
    handle = login.json()["session"]
    monkeypatch.setattr(music, "_queue_item", lambda *args: ("vk", handle, "1_2"))
    stream = client.get("/api/music/queue/room/item/stream")
    assert stream.status_code == 200
    assert stream.json() == {"url": "https://cs1.vkuseraudio.net/full.mp3", "format": "audio"}
    assert [path for path, _, _ in calls] == [
        "/method/audio.get",
        "/method/audio.get",
        "/method/audio.getById",
    ]
    assert all(agent == "VKAudioTest/1" and query == "" for _, agent, query in calls)


def test_vk_hls_ticket_uses_direct_connection_and_vk_only_hosts(client, monkeypatch):
    handle = music._store("vk", "x" * 30)
    music.SESSIONS[handle].imported["1_2"] = {
        "id": "1_2",
        "title": "Song",
        "artist": "Artist",
        "canonical_url": "https://vk.com/audio1_2",
        "playable": True,
        "reason": "",
    }
    monkeypatch.setattr(music, "_queue_item", lambda *args: ("vk", handle, "1_2"))
    proxies = []

    def handler(request):
        if request.url.path.endswith("audio.getById"):
            return httpx.Response(
                200, json={"response": [{"url": "https://cs1.vkuseraudio.net/playlist.m3u8"}]}
            )
        return httpx.Response(200, text="#EXTM3U\n#EXTINF:5,\nsegment.ts\n")

    def client_factory(proxy=None):
        proxies.append(proxy)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(music, "_client", client_factory)
    stream = client.get("/api/music/queue/room/item/stream")
    assert stream.status_code == 200
    assert stream.json()["format"] == "hls"
    ticket_id = stream.json()["url"].split("/")[4]
    assert music.TICKETS[ticket_id].provider == "vk"
    manifest = client.get(stream.json()["url"])
    assert manifest.status_code == 200
    assert "/api/music/hls/" in manifest.text
    assert "segment.ts" not in manifest.text
    assert proxies == [None, None]
    assert not music._safe_media_url("https://host.soundcloud.cloud/segment.ts", "vk")
    with pytest.raises(HTTPException):
        music._rewrite_manifest(
            ticket_id,
            music.TICKETS[ticket_id],
            "https://cs1.vkuseraudio.net/playlist.m3u8",
            "#EXTM3U\nhttps://host.soundcloud.cloud/segment.ts",
        )


def test_vk_login_rejects_missing_audio_response(client, monkeypatch):
    monkeypatch.setattr(music.settings, "vk_audio_enabled", True)
    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"response": {}})
            ),
            trust_env=False,
        ),
    )
    response = client.post("/api/music/vk/login", json={"token": "x" * 30})
    assert response.status_code == 502
    assert not music.SESSIONS


@pytest.mark.parametrize(
    "code,status",
    [(5, 401), (6, 429), (9, 429), (14, 403), (15, 403), (17, 403), (200, 403), (201, 403)],
)
def test_vk_errors_are_explicit(code, status):
    with pytest.raises(HTTPException) as exc:
        music._vk_error({"error": {"error_code": code, "error_msg": "secret-provider-body"}})
    assert exc.value.status_code == status
    assert "secret-provider-body" not in str(exc.value.detail)


def test_modern_soundcloud_urn_is_supported_and_preview_not_playable():
    track = music._track(
        "soundcloud",
        {
            "urn": "soundcloud:tracks:12345",
            "title": "Song",
            "metadata_artist": "Artist",
            "permalink_url": "https://soundcloud.com/artist/song",
            "access": "preview",
        },
    )
    assert track["id"] == "12345"
    assert track["artist"] == "Artist"
    assert track["playable"] is False


def test_public_extraction_falls_back_and_preserves_selected_proxy(monkeypatch):
    music.ROUTE_FAILURES.clear()
    monkeypatch.setattr(music.settings, "music_proxy_urls", ["http://first:1", "http://second:2"])
    calls = []

    def extract(url, *, flat=False, offset=0, proxy=None):
        calls.append(proxy)
        if proxy == "http://first:1":
            raise ValueError("route unavailable")
        return {"id": "12345"}

    monkeypatch.setattr(music, "_extract", extract)
    info = asyncio.run(music._extract_async("https://soundcloud.com/artist/song"))
    assert calls == ["http://first:1", "http://second:2"]
    assert info["_prada_proxy"] == "http://second:2"
    calls.clear()
    asyncio.run(music._extract_async("https://soundcloud.com/artist/another-song"))
    assert calls == ["http://second:2"]
    music.ROUTE_FAILURES.clear()


def test_soundcloud_disconnect_revokes_then_drops_session(client, monkeypatch):
    requests = []

    def handler(request):
        requests.append((request.url.path, request.headers.get("Authorization")))
        return httpx.Response(204)

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    handle = music._store("soundcloud", "access")
    response = client.post("/api/music/soundcloud/disconnect", headers={"X-Music-Session": handle})
    assert response.status_code == 200
    assert handle not in music.SESSIONS
    assert requests == [("/disconnect", "OAuth access")]


def test_public_disconnect_is_local(client):
    handle = music._store("soundcloud", "")
    music.SESSIONS[handle].public_url = "https://soundcloud.com/clalberg/likes"
    response = client.post("/api/music/soundcloud/disconnect", headers={"X-Music-Session": handle})
    assert response.status_code == 200
    assert handle not in music.SESSIONS


def test_guest_cannot_get_soundcloud_stream(client):
    from app.api.lobby import LOBBIES

    LOBBIES["room"] = SimpleNamespace(
        host_token="main-secret",
        queue_items=[SimpleNamespace(id="item", track_id="sc_123", provider_session="session")],
    )
    try:
        response = client.get(
            "/api/music/queue/room/item/stream", headers={"X-Host-Token": "guest"}
        )
        assert response.status_code == 403
    finally:
        LOBBIES.pop("room", None)


def test_authorized_soundcloud_uses_official_full_stream(client, monkeypatch):
    handle = music._store("soundcloud", "access")
    music.SESSIONS[handle].imported["123"] = {
        "id": "123",
        "title": "Private song",
        "artist": "Artist",
        "canonical_url": "https://soundcloud.com/artist/private-song",
        "playable": True,
        "reason": "",
    }
    music.SESSIONS[handle].secret_tokens["123"] = "private-key"
    monkeypatch.setattr(music, "_queue_item", lambda *args: ("soundcloud", handle, "123"))

    async def no_public_extract(*args, **kwargs):
        raise AssertionError("Authenticated tracks must use the official API")

    monkeypatch.setattr(music, "_extract_async", no_public_extract)
    seen = []

    def handler(request):
        seen.append(
            (
                request.url.path,
                request.headers.get("Authorization"),
                request.url.params.get("secret_token"),
            )
        )
        if request.url.path == "/tracks/soundcloud:tracks:123":
            return httpx.Response(200, json={"access": "playable"})
        return httpx.Response(
            200, json={"hls_aac_160_url": "https://host.soundcloud.cloud/full.m3u8"}
        )

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    response = client.get("/api/music/queue/room/item/stream")
    assert response.status_code == 200
    assert response.json()["url"].endswith("/index.m3u8")
    assert seen == [
        ("/tracks/soundcloud:tracks:123", "OAuth access", "private-key"),
        ("/tracks/soundcloud:tracks:123/streams", "OAuth access", "private-key"),
    ]


def test_soundcloud_preview_is_rejected_without_public_fallback(client, monkeypatch):
    handle = music._store("soundcloud", "access")
    music.SESSIONS[handle].imported["123"] = {
        "id": "123",
        "title": "Preview",
        "artist": "Artist",
        "canonical_url": "https://soundcloud.com/artist/preview",
        "playable": True,
        "reason": "",
    }
    monkeypatch.setattr(music, "_queue_item", lambda *args: ("soundcloud", handle, "123"))

    async def no_public_extract(*args, **kwargs):
        raise AssertionError("Preview must not fall back to public extraction")

    monkeypatch.setattr(music, "_extract_async", no_public_extract)
    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"access": "preview"})
            ),
            trust_env=False,
        ),
    )
    response = client.get("/api/music/queue/room/item/stream")
    assert response.status_code == 422


def test_hls_resource_rejects_large_segment_before_buffering(client, monkeypatch):
    ticket = music._new_ticket("https://host.soundcloud.cloud/audio.m4s", None)
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, content=b"a" * 16_000_001)

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    response = client.get(f"/api/music/hls/{ticket}/segment.bin")
    assert response.status_code == 404  # Child resources require an issued opaque ID.
    child = music._child(ticket, music.TICKETS[ticket], "https://host.soundcloud.cloud/audio.m4s")
    response = client.get(child)
    assert response.status_code == 502
    assert calls == ["host.soundcloud.cloud"]


def test_media_redirect_cannot_escape_soundcloud_hosts(client, monkeypatch):
    ticket = music._new_ticket("https://host.soundcloud.cloud/master.m3u8", None)
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://evil.example/file.m3u8"})

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    response = client.get(f"/api/music/hls/{ticket}/index.m3u8")
    assert response.status_code == 502
    assert calls == ["host.soundcloud.cloud"]


def test_spotify_device_pending_cookie_interval_and_slow_down(client, monkeypatch):
    monkeypatch.setattr(music.settings, "spotify_device_enabled", True, raising=False)
    monkeypatch.setattr(
        music.settings, "spotify_device_client_id", music.SPOTIFY_DEVICE_ID, raising=False
    )
    capability = client.get("/api/music/providers").json()["providers"]["spotify"]
    assert capability["device_login"] is True
    responses = ["authorization_pending", "slow_down"]
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/device/authorize"):
            return httpx.Response(
                200,
                json={
                    "device_code": "server-only-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://www.spotify.com/pair",
                    "expires_in": 600,
                    "interval": 1,
                },
            )
        return httpx.Response(400, json={"error": responses.pop(0)})

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    started = client.post("/api/music/spotify/device")
    assert started.status_code == 200
    payload = started.json()
    assert payload["interval"] == 5
    assert payload["verification_url"] == "https://www.spotify.com/pair"
    assert "device_code" not in str(payload) and "server-only-code" not in str(payload)
    login_id = payload["login_id"]
    early = client.get(f"/api/music/spotify/device/{login_id}")
    assert early.json()["status"] == "pending"
    assert calls == ["/oauth2/device/authorize"]
    cookie = client.cookies.get("music_device")
    client.cookies.clear()
    wrong_browser = client.get(f"/api/music/spotify/device/{login_id}")
    assert wrong_browser.status_code == 404
    client.cookies.set("music_device", cookie)
    music.PENDING_DEVICES[login_id].next_poll = monotonic() - 1
    pending = client.get(f"/api/music/spotify/device/{login_id}")
    assert pending.json() == {"status": "pending", "retry_after": 5}
    music.PENDING_DEVICES[login_id].next_poll = monotonic() - 1
    slow = client.get(f"/api/music/spotify/device/{login_id}")
    assert slow.json() == {"status": "pending", "retry_after": 10}
    assert music.PENDING_DEVICES[login_id].interval == 10


def test_spotify_device_approved_library_and_refresh_without_secret(client, monkeypatch, caplog):
    caplog.set_level("INFO", logger="uvicorn.error")
    monkeypatch.setattr(music.settings, "spotify_device_enabled", True, raising=False)
    monkeypatch.setattr(
        music.settings, "spotify_device_client_id", music.SPOTIFY_DEVICE_ID, raising=False
    )
    calls = []

    def handler(request):
        calls.append((request.url.path, request.content.decode()))
        if request.url.path.endswith("/device/authorize"):
            return httpx.Response(
                200,
                json={
                    "device_code": "private-code",
                    "user_code": "XYZ",
                    "verification_uri": "https://spotify.com/pair",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        if request.url.path.endswith("/api/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {"track": {"id": "spotify123", "name": "Song", "artists": [{"name": "Artist"}]}}
                ],
                "next": None,
            },
        )

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    started = client.post("/api/music/spotify/device").json()
    login_id = started["login_id"]
    music.PENDING_DEVICES[login_id].next_poll = monotonic() - 1
    result = client.get(f"/api/music/spotify/device/{login_id}")
    assert result.status_code == 200
    assert result.json()["status"] == "authorized"
    assert result.json()["tracks"][0]["title"] == "Song"
    assert "access-secret" not in str(result.json())
    handle = result.json()["session"]
    assert music.SESSIONS[handle].client_id == music.SPOTIFY_DEVICE_ID
    assert (
        "music_device_authorized provider=spotify library_access=True tracks=1 http_status=200"
        in caplog.text
    )
    assert "access-secret" not in caplog.text
    assert "refresh-secret" not in caplog.text
    assert "XYZ" not in caplog.text
    assert "Song" not in caplog.text
    repeat = client.get(f"/api/music/spotify/device/{login_id}")
    assert repeat.json() == result.json()
    assert len([path for path, _ in calls if path.endswith("/api/token")]) == 1

    music.SESSIONS[handle].expires = monotonic() - 1
    monkeypatch.setattr(music, "_credentials", lambda provider: ("", ""))
    asyncio.run(music._refresh(music.SESSIONS[handle]))
    token_calls = [body for path, body in calls if path.endswith("/api/token")]
    assert "client_secret" not in token_calls[-1]
    assert "grant_type=refresh_token" in token_calls[-1]


def test_spotify_device_authorized_without_library_access(client, monkeypatch):
    monkeypatch.setattr(music.settings, "spotify_device_enabled", True, raising=False)
    monkeypatch.setattr(
        music.settings, "spotify_device_client_id", music.SPOTIFY_DEVICE_ID, raising=False
    )

    def handler(request):
        if request.url.path.endswith("/device/authorize"):
            return httpx.Response(
                200,
                json={
                    "device_code": "private",
                    "user_code": "XYZ",
                    "verification_uri": "https://spotify.com/pair",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        if request.url.path.endswith("/api/token"):
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        return httpx.Response(403)

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    login_id = client.post("/api/music/spotify/device").json()["login_id"]
    music.PENDING_DEVICES[login_id].next_poll = monotonic() - 1
    result = client.get(f"/api/music/spotify/device/{login_id}").json()
    assert result["status"] == "authorized"
    assert result["authenticated"] is False
    assert result["tracks"] == []
    assert "library_error" in result
    assert result["session"] in music.SESSIONS


def test_spotify_device_denied_and_expired(client, monkeypatch):
    monkeypatch.setattr(music.settings, "spotify_device_enabled", True, raising=False)
    monkeypatch.setattr(
        music.settings, "spotify_device_client_id", music.SPOTIFY_DEVICE_ID, raising=False
    )

    def handler(request):
        if request.url.path.endswith("/device/authorize"):
            return httpx.Response(
                200,
                json={
                    "device_code": "private",
                    "user_code": "XYZ",
                    "verification_uri": "https://spotify.com/pair",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        return httpx.Response(400, json={"error": "access_denied"})

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    login_id = client.post("/api/music/spotify/device").json()["login_id"]
    music.PENDING_DEVICES[login_id].next_poll = monotonic() - 1
    denied = client.get(f"/api/music/spotify/device/{login_id}")
    assert denied.status_code == 403 and login_id not in music.PENDING_DEVICES
    login_id = client.post("/api/music/spotify/device").json()["login_id"]
    music.PENDING_DEVICES[login_id].expires = monotonic() - 1
    expired = client.get(f"/api/music/spotify/device/{login_id}")
    assert expired.status_code == 410


def test_spotify_quota_reason_and_retry_after_are_sanitized(monkeypatch, caplog):
    caplog.set_level("WARNING", logger="uvicorn.error")

    def handler(request):
        return httpx.Response(
            429,
            headers={"Retry-After": "120"},
            json={"error": {"reason": "QUOTA_EXCEEDED", "message": "private-user-id secret"}},
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await music._call(client, "GET", "https://api.spotify.com/v1/me/tracks")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(run())
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "120"
    assert exc.value.headers["X-Music-Rate-Reason"] == "QUOTA_EXCEEDED"
    assert "повторный вход это не исправляет" in exc.value.detail
    assert (
        "music_provider_rate_limit provider=spotify reason=QUOTA_EXCEEDED retry_after=120"
        in caplog.text
    )
    assert "private-user-id" not in caplog.text


def test_retry_after_http_date_and_bad_body_are_bounded():
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=75), usegmt=True)
    assert 73 <= music._retry_after_seconds(future) <= 75
    assert music._retry_after_seconds("999999") == 86400
    assert music._retry_after_seconds("9" * 40) == 86400
    assert music._retry_after_seconds("not-a-date") == 60
    response = httpx.Response(429, content=b"not json")
    assert music._rate_reason(response) == "unknown"
    unknown = httpx.Response(429, json={"reason": "some-private-reason"})
    assert music._rate_reason(unknown) == "unknown"


def test_spotify_library_cooldown_blocks_second_external_request(client, monkeypatch):
    handle = music._store("spotify", "access")
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            429, headers={"Retry-After": "90"}, json={"error": {"reason": "QUOTA_EXCEEDED"}}
        )

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    first = client.get("/api/music/spotify/tracks", headers={"X-Music-Session": handle})
    second = client.get("/api/music/spotify/tracks", headers={"X-Music-Session": handle})
    assert first.status_code == second.status_code == 429
    assert calls == 1
    assert 89 <= int(second.headers["Retry-After"]) <= 90
    assert "повторный вход это не исправляет" in second.json()["detail"]


def test_device_approval_preserves_session_when_library_rate_limited(client, monkeypatch):
    monkeypatch.setattr(music.settings, "spotify_device_enabled", True)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/device/authorize"):
            return httpx.Response(
                200,
                json={
                    "device_code": "hidden",
                    "user_code": "ABC",
                    "verification_uri": "https://spotify.com/pair",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        if request.url.path.endswith("/api/token"):
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        return httpx.Response(429, headers={"Retry-After": "75"}, json={"reason": "QUOTA_EXCEEDED"})

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    login_id = client.post("/api/music/spotify/device").json()["login_id"]
    music.PENDING_DEVICES[login_id].next_poll = monotonic() - 1
    approved = client.get(f"/api/music/spotify/device/{login_id}").json()
    assert approved["status"] == "authorized"
    assert approved["authenticated"] is False
    assert approved["library_error_status"] == 429
    assert approved["retry_after"] == 75
    assert approved["quota_reason"] == "QUOTA_EXCEEDED"
    assert approved["session"] in music.SESSIONS
    retry = client.get(
        "/api/music/spotify/tracks", headers={"X-Music-Session": approved["session"]}
    )
    assert retry.status_code == 429
    assert calls.count("/v1/me/tracks") == 1


def _spotify_embed_page(count: int) -> str:
    tracks = [
        {
            "uri": f"spotify:track:{i:022d}" if i else "spotify:track:0000000000000000000001",
            "title": f"Song {i}",
            "subtitle": "Artist",
            "duration": 180000,
            "isPlayable": i != 2,
            "entityType": "track",
        }
        for i in range(count)
    ]
    page = {
        "props": {
            "pageProps": {
                "state": {
                    "data": {
                        "entity": {"trackList": tracks, "accessToken": "secret-embedded-token"}
                    }
                }
            }
        }
    }
    payload = json.dumps(page)
    return f'<html><script id="__NEXT_DATA__" type="application/json">{payload}</script></html>'


def test_spotify_public_playlist_15_tracks_without_credentials(client, monkeypatch):
    html = _spotify_embed_page(15)
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=html)

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    response = client.post(
        "/api/music/spotify/public",
        json={"url": "https://open.spotify.com/intl-de/playlist/7EtiSLKUiHY7vmJ7SikOaj?si=share"},
    )
    assert response.status_code == 200
    page = response.json()
    assert page["authenticated"] is False
    assert len(page["tracks"]) == 15
    assert page["tracks"][0]["id"] == page["tracks"][1]["id"]  # Positions preserved.
    assert page["tracks"][2]["playable"] is False
    assert page["has_more"] is False
    assert "100" in page["import_notice"]
    assert "secret-embedded-token" not in str(page)
    assert seen == ["https://open.spotify.com/embed/playlist/7EtiSLKUiHY7vmJ7SikOaj"]
    handle = page["session"]
    assert music.validate_track("spotify", handle, page["tracks"][1]["id"])["title"] == "Song 1"


def test_spotify_public_playlist_caps_at_100_and_pages_locally(client, monkeypatch):
    html = _spotify_embed_page(105)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=html)

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    first = client.post(
        "/api/music/spotify/public",
        json={"url": "https://open.spotify.com/playlist/7EtiSLKUiHY7vmJ7SikOaj"},
    ).json()
    assert len(first["tracks"]) == 50 and first["has_more"] is True
    assert first["next_cursor"] == "50"
    second = client.get(
        "/api/music/spotify/tracks?cursor=50", headers={"X-Music-Session": first["session"]}
    ).json()
    assert len(second["tracks"]) == 50 and second["has_more"] is False
    assert second["next_cursor"] is None
    assert calls == 1  # Public pagination never calls private Spotify API.
    assert len(music.SESSIONS[first["session"]].public_tracks) == 100


def test_spotify_public_playlist_rejects_missing_embed_and_ssrf(client, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://evil.example/playlist"})

    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    invalid = client.post(
        "/api/music/spotify/public",
        json={"url": "https://open.spotify.com.evil.example/playlist/7EtiSLKUiHY7vmJ7SikOaj"},
    )
    assert invalid.status_code == 400 and calls == []
    redirected = client.post(
        "/api/music/spotify/public",
        json={"url": "https://open.spotify.com/playlist/7EtiSLKUiHY7vmJ7SikOaj"},
    )
    assert redirected.status_code == 502
    assert calls == ["open.spotify.com"]
    with pytest.raises(HTTPException) as exc:
        music._spotify_embed_tracks("<html>editorial page without trackList</html>")
    assert exc.value.status_code == 422


def test_spotify_public_embed_body_is_bounded(client, monkeypatch):
    monkeypatch.setattr(
        music,
        "_client",
        lambda proxy=None: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"x" * 2_000_001)
            ),
            trust_env=False,
        ),
    )
    response = client.post(
        "/api/music/spotify/public",
        json={"url": "https://open.spotify.com/playlist/7EtiSLKUiHY7vmJ7SikOaj"},
    )
    assert response.status_code == 502
    assert not music.SESSIONS


@pytest.mark.parametrize("value", [
    "https://evil.example/blank.html#access_token=" + "x" * 30,
    "https://evil.example@oauth.vk.com/blank.html#access_token=" + "x" * 30,
    "access_token=" + "x" * 30 + "&token=" + "y" * 30,
    "access_token=",
])
def test_vk_token_input_rejects_ambiguous_or_wrong_links(value):
    with pytest.raises(HTTPException) as exc:
        music._vk_access_token(value)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("message,subcode,expected", [
    ("User authorization failed: access_token was given to another ip address", None, "сети входа"),
    ("User authorization failed", 1130, "сети входа"),
    ("access token expired", None, "Срок токена"),
    ("invalid access token", None, "VK отклонил"),
])
def test_vk_auth_reason_is_safe_and_distinguishes_expiry(message, subcode, expected):
    with pytest.raises(HTTPException) as exc:
        music._vk_error({"error": {"error_code": 5, "error_subcode": subcode,
                                   "error_msg": message + " SECRET_PROVIDER_VALUE"}})
    assert expected in exc.value.detail
    assert "SECRET_PROVIDER_VALUE" not in exc.value.detail


@pytest.mark.parametrize("payload,total", [
    ({"count": 1, "items": [{"id": 2, "owner_id": 1}]}, 1),
    ([1, {"id": 2, "owner_id": 1}], 1),
    ([{"id": 2, "owner_id": 1}], None),
    ([], None),
    ([0], 0),
])
def test_vk_audio_accepts_object_and_legacy_list(payload, total):
    rows, count = music._vk_audio_page({"response": payload})
    assert count == total
    assert all(isinstance(row, dict) for row in rows)


@pytest.mark.parametrize("payload", [None, True, "private-data", {},
    {"items": None}, {"items": [], "count": -1}, [1, "private-song"]])
def test_vk_audio_rejects_bad_shapes_without_logging_payload(payload, caplog):
    with pytest.raises(HTTPException) as exc:
        music._vk_audio_page({"response": payload})
    assert exc.value.status_code == 502
    assert "private" not in caplog.text
    assert "private" not in exc.value.detail


@pytest.mark.parametrize("code", [3, 8, 5, 15])
def test_vk_audio_never_turns_provider_error_into_empty_library(code):
    with pytest.raises(HTTPException):
        music._vk_audio_page({"error": {"error_code": code}})


def test_vk_legacy_audio_pages_work_through_login_and_load_more(client, monkeypatch):
    monkeypatch.setattr(music.settings, "vk_audio_enabled", True)

    def handler(request):
        data = parse_qs(request.content.decode())
        offset = int(data.get("offset", ["0"])[0])
        count = int(data["count"][0])
        rows = [{"id": i + 1, "owner_id": 1, "title": "Song", "artist": "Artist"}
                for i in range(offset, min(101, offset + count))]
        return httpx.Response(200, json={"response": [101, *rows]})

    monkeypatch.setattr(music, "_client", lambda proxy=None: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False))
    first = client.post("/api/music/vk/login", json={"token": "x" * 30})
    assert first.status_code == 200
    page = first.json()
    assert len(page["tracks"]) == 50 and page["has_more"] is True
    headers = {"X-Music-Session": page["session"]}
    next_page = client.get("/api/music/vk/tracks?cursor=50", headers=headers).json()
    assert len(next_page["tracks"]) == 50 and next_page["next_cursor"] == "100"
    last = client.get("/api/music/vk/tracks?cursor=100", headers=headers).json()
    assert len(last["tracks"]) == 1 and last["has_more"] is False



def test_vk_default_kate_header_matches_token_application(monkeypatch):
    original = music._value
    monkeypatch.setattr(music, "_value", lambda name, default="": "" if
                        name == "vk_audio_user_agent" else original(name, default))
    assert music._vk_headers()["User-Agent"].startswith("KateMobileAndroid/")


def test_vk_compatible_version_is_used_for_login_pages_and_playback(client, monkeypatch):
    monkeypatch.setattr(music.settings, "vk_audio_enabled", True)
    seen = []

    def handler(request):
        data = parse_qs(request.content.decode())
        seen.append(request.url.path)
        assert data["v"] == ["5.131"]
        assert request.headers["User-Agent"].startswith("KateMobileAndroid/")
        row = {"id": 2, "owner_id": 1, "title": "Song", "artist": "Artist",
               "url": "https://cs1.vkuseraudio.net/full.mp3"}
        payload = [row] if request.url.path.endswith("audio.getById") else {
            "count": 1, "items": [row]}
        return httpx.Response(200, json={"response": payload})

    monkeypatch.setattr(music, "_client", lambda proxy=None: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False))
    login = client.post("/api/music/vk/login", json={"token": "x" * 30})
    assert login.status_code == 200
    monkeypatch.setattr(music, "_queue_item", lambda *args: (
        "vk", login.json()["session"], "1_2"))
    stream = client.get("/api/music/queue/room/item/stream")
    assert stream.status_code == 200
    assert len(seen) == 3


@pytest.mark.parametrize("url", [
    "https://soundcloud.com/clalberg",
    "https://soundcloud.com/clalberg/",
    "https://soundcloud.com/clalberg/likes",
    "https://m.soundcloud.com/clalberg?utm_source=share",
    "https://www.soundcloud.com/clalberg/likes/?si=test",
])
def test_soundcloud_profile_links_resolve_to_likes_for_all_pages(client, monkeypatch, url):
    calls = []

    async def extract(target, *, flat=False, offset=0):
        calls.append((target, offset))
        return {"entries": []}

    monkeypatch.setattr(music, "_extract_async", extract)
    response = client.post("/api/music/soundcloud/public", json={"url": url})
    assert response.status_code == 200
    handle = response.json()["session"]
    assert client.get("/api/music/soundcloud/tracks?cursor=50", headers={
        "X-Music-Session": handle}).status_code == 200
    assert calls == [("https://soundcloud.com/clalberg/likes", 0),
                     ("https://soundcloud.com/clalberg/likes", 50)]


@pytest.mark.parametrize("url", [
    "https://evil.example/clalberg", "http://soundcloud.com/clalberg",
    "https://soundcloud.com/clalberg/a-track", "https://soundcloud.com/",
])
def test_soundcloud_profile_import_rejects_other_hosts_and_paths(client, monkeypatch, url):
    monkeypatch.setattr(music, "_extract_async", lambda *a, **kw: pytest.fail("unexpected fetch"))
    assert client.post("/api/music/soundcloud/public", json={"url": url}).status_code == 400
