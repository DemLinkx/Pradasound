from fastapi.testclient import TestClient

from app.api.lobby import LOBBIES
from app.main import app
from app.providers import music


def import_track(handle: str, track_id: str) -> None:
    music.SESSIONS[handle].imported[track_id] = {
        "id": track_id,
        "title": "Song",
        "artist": "Artist",
        "playable": True,
        "canonical_url": f"https://soundcloud.com/artist/song-{track_id}",
        "reason": "",
    }


def test_imported_tracks_keep_author_access_private_and_host_can_resolve(monkeypatch):
    LOBBIES.clear()
    handle = music._store("soundcloud", "", expires_in=3600)
    import_track(handle, "12345")

    async def extract(url, *, flat=False, offset=0):
        assert url == "https://soundcloud.com/artist/song-12345"
        return {
            "formats": [
                {
                    "url": "https://host.soundcloud.cloud/full.m3u8",
                    "acodec": "aac",
                    "protocol": "m3u8_native",
                    "abr": 160,
                }
            ]
        }

    monkeypatch.setattr(music, "_extract_async", extract)
    with TestClient(app) as client:
        room = client.post("/api/lobby/create").json()
        path = f"/api/lobby/{room['lobby_id']}/ws"
        with client.websocket_connect(path) as host:
            host.send_json({"host_token": room["host_token"]})
            host.receive_json()
            host.receive_json()
            with client.websocket_connect(path) as guest:
                guest.send_json({"member_token": "a" * 64})
                owner = guest.receive_json()["owner_id"]
                guest.receive_json()
                host.receive_json()
                guest.send_json(
                    {
                        "action": "add_track",
                        "track_id": "sc_12345",
                        "provider_session": handle,
                        "title": "Untrusted title",
                    }
                )
                state = guest.receive_json()
                assert host.receive_json() == state
                item = state["queue_items"][0]
                assert item["owner_id"] == owner
                assert handle not in str(state)
                assert "provider_session" not in item
                assert state["track_titles"]["sc_12345"] == "Song — Artist"
                stream = f"/api/music/queue/{room['lobby_id']}/{item['id']}/stream"
                assert client.get(stream).status_code == 403
                audio = client.get(stream, headers={"X-Host-Token": room["host_token"]})
                assert audio.status_code == 200
                assert audio.json()["url"].endswith("/index.m3u8")
                guest.send_json(
                    {"action": "add_track", "track_id": "sc_99999", "provider_session": handle}
                )
                assert guest.receive_json()["type"] == "error"
    music.SESSIONS.pop(handle, None)


def test_session_reconnect_updates_only_owners_tracks():
    LOBBIES.clear()
    first = music._store("soundcloud", "", expires_in=3600)
    second = music._store("soundcloud", "", expires_in=3600)
    for handle in (first, second):
        import_track(handle, "12345")
    with TestClient(app) as client:
        room = client.post("/api/lobby/create").json()
        path = f"/api/lobby/{room['lobby_id']}/ws"
        with client.websocket_connect(path) as host:
            host.send_json({"host_token": room["host_token"]})
            host.receive_json()
            host.receive_json()
            host.send_json(
                {"action": "add_track", "track_id": "sc_12345", "provider_session": first}
            )
            host.receive_json()
            with client.websocket_connect(path) as guest:
                guest.send_json({"member_token": "a" * 64})
                guest.receive_json()
                guest.receive_json()
                host.receive_json()
                guest.send_json(
                    {"action": "add_track", "track_id": "sc_12345", "provider_session": first}
                )
                guest.receive_json()
                host.receive_json()
                guest.send_json(
                    {
                        "action": "set_music_session",
                        "provider": "soundcloud",
                        "provider_session": second,
                    }
                )
                state = guest.receive_json()
                host.receive_json()
                stored = LOBBIES[room["lobby_id"]].queue_items
                assert stored[0].provider_session == first
                assert stored[1].provider_session == second
                assert [item["access_revision"] for item in state["queue_items"]] == [0, 1]
                assert first not in str(state) and second not in str(state)
    for handle in (first, second):
        music.SESSIONS.pop(handle, None)
