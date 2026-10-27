import asyncio
import importlib
import time

import httpx
from fastapi.testclient import TestClient
from yt_dlp.utils import DownloadError

main = importlib.import_module("app.main")


def test_audio_falls_back_and_keeps_the_successful_proxy(monkeypatch):
    monkeypatch.setattr(main.settings, "youtube_audio_enabled", True)
    monkeypatch.setattr(main.settings, "youtube_proxy_urls", ["http://first:1081", "http://second:1082"])
    monkeypatch.setattr(main, "YOUTUBE_AUDIO_CACHE", {})
    monkeypatch.setattr(main, "YOUTUBE_AUDIO_FAILURES", {})
    monkeypatch.setattr(main, "YOUTUBE_AUDIO_LOCKS", {})
    monkeypatch.setattr(main, "YOUTUBE_METADATA_CACHE", {})
    monkeypatch.setattr(main, "YOUTUBE_ROUTE_HEALTH", {})
    attempted = []
    clients = []
    real_client = httpx.AsyncClient

    def extract(video, proxy):
        attempted.append(proxy)
        if len(attempted) == 1:
            raise DownloadError("Sign in to confirm you are not a bot")
        return "https://audio.googlevideo.com/audio", time.time() + 300, "Track", "Artist", {"User-Agent": "test-browser"}

    def client(*, proxy, trust_env, **kwargs):
        assert trust_env is False
        clients.append(proxy)
        def respond(request):
            assert request.headers["range"] == "bytes=0-0"
            return httpx.Response(206, content=b"a", headers={"Content-Type": "audio/mp4"})
        assert kwargs["headers"] == {"User-Agent": "test-browser"}
        return real_client(headers=kwargs["headers"], transport=httpx.MockTransport(respond))

    monkeypatch.setattr(main, "extract_youtube_audio", extract)
    monkeypatch.setattr(main.httpx, "AsyncClient", client)
    source = asyncio.run(main.youtube_audio_url("dQw4w9WgXcQ"))
    assert source.proxy == "http://second:1082"
    assert attempted == ["http://first:1081", "http://second:1082"]
    assert clients == ["http://second:1082"]
    assert asyncio.run(main.youtube_audio_url("dQw4w9WgXcQ")) is source
    assert asyncio.run(main.youtube_audio_url("28rboCjfW_E")).proxy == "http://second:1082"
    assert attempted == ["http://first:1081", "http://second:1082", "http://second:1082"]


def test_audio_stream_keeps_proxy_and_range_across_redirects(monkeypatch):
    monkeypatch.setattr(main.settings, "youtube_audio_enabled", True)
    source = main.YouTubeAudio("https://audio.googlevideo.com/redirect", time.time() + 300,
                              "http://second:1082", {"User-Agent": "test-browser"})
    monkeypatch.setattr(main, "YOUTUBE_AUDIO_CACHE", {"dQw4w9WgXcQ": source})
    monkeypatch.setattr(main, "YOUTUBE_AUDIO_TICKETS", {"test-ticket": ("dQw4w9WgXcQ", time.monotonic() + 300)})
    real_client = httpx.AsyncClient
    seen = []

    def client(*, proxy, trust_env, **kwargs):
        assert proxy == "http://second:1082" and trust_env is False
        def respond(request):
            assert request.headers["user-agent"] == "test-browser"
            seen.append((request.url.path, request.headers.get("range")))
            if request.url.path == "/redirect":
                return httpx.Response(302, headers={"Location": "https://cdn.googlevideo.com/audio"})
            return httpx.Response(206, stream=httpx.ByteStream(b"audio"), headers={"Content-Type": "audio/mp4",
                                  "Content-Range": "bytes 0-4/100", "Accept-Ranges": "bytes"})
        return real_client(headers=kwargs["headers"], transport=httpx.MockTransport(respond))

    monkeypatch.setattr(main.httpx, "AsyncClient", client)
    with TestClient(main.app) as app:
        response = app.get("/api/youtube/audio/file/test-ticket", headers={"Range": "bytes=0-4"})
    assert response.status_code == 206 and response.content == b"audio"
    assert response.headers["content-range"] == "bytes 0-4/100"
    assert seen == [("/redirect", "bytes=0-4"), ("/audio", "bytes=0-4")]


def test_audio_redirect_cannot_fetch_arbitrary_hosts(monkeypatch):
    async def check():
        def respond(request):
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            try:
                await main.open_youtube_stream(client, "https://audio.googlevideo.com/audio")
            except main.HTTPException as exc:
                assert exc.status_code == 502
            else:
                raise AssertionError("Invalid redirect was accepted")
    asyncio.run(check())
