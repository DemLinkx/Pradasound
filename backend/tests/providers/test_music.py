import asyncio
from time import monotonic
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.providers import music


@pytest.fixture(autouse=True)
def clear_state():
    music.PENDING.clear()
    music.SESSIONS.clear()
    music.TICKETS.clear()
    music.RESOLVE_CACHE.clear()
    yield
    music.PENDING.clear()
    music.SESSIONS.clear()
    music.TICKETS.clear()


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
