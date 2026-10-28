import asyncio
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


def test_vk_public_authorization_url_and_direct_audio(client, monkeypatch):
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
        "scope": ["8"],
        "redirect_uri": ["https://oauth.vk.com/blank.html"],
        "display": ["page"],
        "response_type": ["token"],
    }
    assert "secret" not in url.query.lower()
    calls = []

    def handler(request):
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
    login = client.post("/api/music/vk/login", json={"token": "x" * 30})
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
