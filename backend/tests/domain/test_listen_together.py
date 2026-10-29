from contextlib import contextmanager

from fastapi.testclient import TestClient

from app import main
from app.api.lobby import LOBBIES
from app.main import app


@contextmanager
def party():
    LOBBIES.clear()
    main.LISTENER_STREAMS.clear()
    with TestClient(app) as client:
        room = client.post("/api/lobby/create").json()
        path = f"/api/lobby/{room['lobby_id']}/ws"
        with client.websocket_connect(path) as host:
            host.send_json({"host_token": room["host_token"]})
            host.receive_json()
            host.receive_json()
            with client.websocket_connect(path) as guest:
                guest.send_json({"member_token": "a" * 64})
                guest.receive_json()
                guest.receive_json()
                host.receive_json()
                host.send_json({"action": "add_track", "track_id": "dQw4w9WgXcQ"})
                state = host.receive_json()
                assert guest.receive_json() == state
                yield client, room, host, guest, state


def test_listening_is_opt_in_scoped_to_queue_and_revocable(monkeypatch):
    calls = []

    async def prepare(video_id, lobby_id, host_token):
        calls.append(video_id)
        return {"url": "/api/youtube/audio/file/shared-ticket"}

    monkeypatch.setattr(main, "prepare_youtube_audio", prepare)
    with party() as (client, room, host, guest, state):
        path = f"/api/listen/{room['lobby_id']}/{state['queue_items'][0]['id']}/stream"
        assert client.get(path).status_code == 403
        guest.send_json({"action": "listen_together", "enabled": True})
        access = guest.receive_json()
        assert access["type"] == "listener_access" and access["enabled"] is True
        token = access["token"]
        headers = {"X-Listener-Token": token}
        assert client.get(path, headers=headers).json()["url"].endswith("shared-ticket")
        assert client.get(path, headers=headers).status_code == 200
        assert len(calls) == 1
        assert (
            client.get(
                path.replace(state["queue_items"][0]["id"], "f" * 32), headers=headers
            ).status_code
            == 404
        )
        guest.send_json({"action": "listen_together", "enabled": False})
        assert guest.receive_json()["enabled"] is False
        assert client.get(path, headers=headers).status_code == 403
        assert room["host_token"] not in str(access)


def test_only_host_syncs_playback_without_changing_queue():
    with party() as (_, room, host, guest, state):
        guest.send_json({"action": "listen_together", "enabled": True})
        access = guest.receive_json()
        command = {
            "action": "playback_sync",
            "item_id": state["queue_items"][0]["id"],
            "expected_revision": state["playback_revision"],
            "position": 23.5,
            "duration": 180,
            "paused": False,
        }
        guest.send_json(command)
        assert guest.receive_json()["error"] == "main_only"
        host.send_json(command)
        sync = guest.receive_json()
        assert sync["type"] == "playback_sync" and sync["position"] == 23.5
        assert access["token"] not in str(sync) and room["host_token"] not in str(sync)
        assert LOBBIES[room["lobby_id"]].playback_revision == state["playback_revision"]
        host.send_json({**command, "expected_revision": 999})
        assert host.receive_json()["error"] == "stale_playback_sync"
        host.send_json({"action": "skip_track", "expected_revision": state["playback_revision"]})
        changed = guest.receive_json()
        assert host.receive_json() == changed
        assert changed["tracks_queue"] == [] and changed["playback"] is None


def test_disconnect_revokes_listener_and_host_disconnect_pauses_followers():
    with party() as (client, room, host, guest, state):
        guest.send_json({"action": "listen_together", "enabled": True})
        token = guest.receive_json()["token"]
        guest.send_json({"action": "playback_ping", "client_time": 12345})
        assert guest.receive_json()["client_time"] == 12345
        host.send_json(
            {
                "action": "playback_sync",
                "item_id": state["queue_items"][0]["id"],
                "expected_revision": 0,
                "position": 40,
                "duration": 180,
                "paused": False,
            }
        )
        assert guest.receive_json()["paused"] is False
        host.close()
        paused = guest.receive_json()
        assert paused["host_online"] is False and paused["playback"]["paused"] is True
        path = f"/api/listen/{room['lobby_id']}/{state['queue_items'][0]['id']}/stream"
        assert client.get(path, headers={"X-Listener-Token": token}).status_code == 409
        guest.close()
    assert token not in LOBBIES[room["lobby_id"]].listener_tokens


def test_guest_cannot_use_listener_token_as_host_credentials():
    with party() as (client, room, _, guest, state):
        guest.send_json({"action": "listen_together", "enabled": True})
        token = guest.receive_json()["token"]
        assert (
            client.post(
                "/api/youtube/audio/dQw4w9WgXcQ",
                headers={"X-Lobby-Id": room["lobby_id"], "X-Host-Token": token},
            ).status_code
            == 403
        )
        guest.send_json({"action": "skip_track", "expected_revision": state["playback_revision"]})
        assert guest.receive_json()["error"] == "main_only"


def test_non_listening_guest_gets_no_periodic_playback_packets():
    with party() as (client, room, host, guest, state):
        path = f"/api/lobby/{room['lobby_id']}/ws"
        with client.websocket_connect(path) as quiet:
            quiet.send_json({"member_token": "c" * 64})
            quiet.receive_json()
            quiet.receive_json()
            host.receive_json()
            guest.receive_json()
            guest.send_json({"action": "listen_together", "enabled": True})
            guest.receive_json()
            host.send_json(
                {
                    "action": "playback_sync",
                    "item_id": state["queue_items"][0]["id"],
                    "expected_revision": 0,
                    "position": 10,
                    "duration": 180,
                    "paused": False,
                }
            )
            assert guest.receive_json()["type"] == "playback_sync"
            quiet.send_json({"action": "playback_ping", "client_time": 123})
            assert quiet.receive_json()["type"] == "playback_clock"


def test_yandex_listener_uses_author_access_without_exposing_tokens(monkeypatch):
    with party() as (client, room, _, guest, state):
        item = LOBBIES[room["lobby_id"]].queue_items[0]
        item.track_id = "yandex_123"
        item.yandex_session = "private-author-handle"

        def stream(lobby_id, item_id, host_token):
            assert host_token == room["host_token"]
            assert LOBBIES[lobby_id].queue_items[0].yandex_session == "private-author-handle"
            return {"url": "https://music.example/track.mp3"}

        monkeypatch.setattr(main, "yandex_queue_stream", stream)
        guest.send_json({"action": "listen_together", "enabled": True})
        token = guest.receive_json()["token"]
        response = client.get(
            f"/api/listen/{room['lobby_id']}/{item.id}/stream", headers={"X-Listener-Token": token}
        )
        assert response.json() == {"url": "https://music.example/track.mp3"}
        assert (
            "private-author-handle" not in response.text and room["host_token"] not in response.text
        )
