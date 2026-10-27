"""Check the deployed frontend, API and WebSocket without contacting music providers."""

import argparse
import json
import re
import secrets
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect


def check(base: str, audio_enabled: bool = False) -> None:
    def request(path: str, *, data: bytes | None = None, headers: dict | None = None):
        req = Request(base + path, data=data, headers=headers or {})
        try:
            with urlopen(req, timeout=10) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            return error.code, error.headers, error.read()

    status, _, body = request("/healthcheck")
    assert status == 200 and json.loads(body) == {"status": "ok"}, "API healthcheck failed"
    status, _, body = request("/")
    assert status == 200 and b'id="root"' in body, "Frontend is unavailable"
    asset = re.search(rb'src="(/assets/[^" ]+\.js)"', body)
    assert asset, "Frontend bundle is missing"
    status, headers, _ = request(asset[1].decode())
    assert status == 200 and "immutable" in headers.get("Cache-Control", ""), "Bundle cache failed"
    status, _, body = request("/room/smoke")
    assert status == 200 and b'id="root"' in body, "SPA fallback failed"
    assert request("/assets/missing-smoke.js")[0] == 404, "Missing asset returns HTML"
    status, _, body = request("/api/missing-smoke")
    assert status == 404 and json.loads(body)["detail"] == "Not Found", "API routing failed"
    print("OK: frontend, static assets, SPA fallback, API healthcheck")

    status, _, body = request("/api/lobby/create", data=b"{}", headers={"Content-Type": "application/json"})
    assert status == 201, "Cannot create a room"
    room = json.loads(body)
    url = urlsplit(base)
    ws_url = urlunsplit(("wss" if url.scheme == "https" else "ws", url.netloc,
                        f"/api/lobby/{room['lobby_id']}/ws", "", ""))

    def state(ws):
        for _ in range(8):
            message = json.loads(ws.recv(timeout=10))
            assert message.get("type") != "error", f"Queue rejected a command: {message.get('error')}"
            if message.get("type") == "lobby_state":
                return message
        raise AssertionError("No lobby_state received")

    with connect(ws_url, origin=base, open_timeout=10, proxy=None) as host:
        host.send(json.dumps({"host_token": room["host_token"]}))
        assert state(host)["tracks_queue"] == []
        with connect(ws_url, origin=base, open_timeout=10, proxy=None) as guest:
            guest.send(json.dumps({"member_token": secrets.token_hex(32)}))
            state(host)
            state(guest)
            guest.send(json.dumps({"action": "add_track", "track_id": "dQw4w9WgXcQ", "title": "Smoke test"}))
            added = state(host)
            assert added["tracks_queue"] == state(guest)["tracks_queue"] == ["dQw4w9WgXcQ"]
            host.send(json.dumps({"action": "skip_track", "expected_revision": added["playback_revision"]}))
            assert state(host)["tracks_queue"] == state(guest)["tracks_queue"] == []
    print("OK: host/member WebSockets, shared queue, skip through reverse proxy")

    try:
        with connect(ws_url, origin="https://untrusted.example.invalid", open_timeout=10, proxy=None):
            raise AssertionError("WebSocket accepted an untrusted Origin")
    except InvalidStatus as error:
        assert error.response.status_code == 403, "Unexpected rejection of untrusted Origin"
    print("OK: untrusted WebSocket Origin rejected")

    if audio_enabled:
        print("OK: audio mode enabled; provider playback must be checked separately")
        return
    status, _, _ = request(
        "/api/youtube/audio/dQw4w9WgXcQ", data=b"",
        headers={"X-Lobby-Id": room["lobby_id"], "X-Host-Token": room["host_token"]},
    )
    assert status == 503, "Production should use the YouTube iframe"
    print("OK: production YouTube mode (no extraction)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Public origin, e.g. https://music.example.com")
    parser.add_argument("--audio-enabled", action="store_true", help="Skip the disabled-audio assertion")
    args = parser.parse_args()
    target = args.url.rstrip("/")
    parsed = urlsplit(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path:
        parser.error("Provide an HTTP(S) origin without a path")
    check(target, args.audio_enabled)
