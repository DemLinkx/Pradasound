import base64
import hashlib
import hmac
import time

from test_listen_together import party

from app.api.lobby import LOBBIES, relay_ice

SDP = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


def state(ws):
    for _ in range(8):
        message = ws.receive_json()
        if message["type"] == "lobby_state":
            return message
    raise AssertionError("Missing lobby state")


def test_host_relay_signals_only_opted_in_room_members_and_only_audio():
    with party() as (_, room, host, guest, initial):
        lobby = LOBBIES[room["lobby_id"]]
        lobby.queue_items[0].track_id = "spotify_" + "a" * 22
        host_id = next(m["member_id"] for m in initial["members"] if m["role"] == "Main")
        guest_id = next(m["member_id"] for m in initial["members"] if m["role"] == "Member")
        host.send_json({"action": "relay_request"})
        active = state(host)
        assert state(guest) == active
        generation = active["relay"]["generation"]
        guest.send_json({"action": "relay_join"})
        assert guest.receive_json()["type"] == "error"
        guest.send_json({"action": "listen_together", "enabled": True})
        guest.receive_json()
        state(guest)
        state(host)
        guest.send_json({"action": "relay_join"})
        access = guest.receive_json()
        assert access["type"] == host.receive_json()["type"] == "relay_access"
        assert "host_token" not in str(access)
        assert state(host)["relay"]["listeners"] == [guest_id]
        state(guest)
        offer = {"action": "relay_signal", "target": guest_id, "generation": generation,
                 "kind": "offer", "data": SDP}
        host.send_json({**offer, "data": SDP + "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"})
        assert host.receive_json()["type"] == "error"
        host.send_json({**offer, "generation": generation - 1})
        assert host.receive_json()["type"] == "error"
        host.send_json({**offer, "target": "f" * 32})
        assert host.receive_json()["type"] == "error"
        host.send_json(offer)
        assert guest.receive_json() == {"type": "relay_signal", "from": host_id,
                                       "generation": generation, "kind": "offer", "data": SDP}
        guest.send_json({**offer, "kind": "answer", "target": host_id})
        assert host.receive_json()["kind"] == "answer"
        guest.send_json({**offer, "target": host_id})
        assert guest.receive_json()["type"] == "error"
        guest.send_json({"action": "listen_together", "enabled": False})
        guest.receive_json()
        assert state(host)["relay"]["listeners"] == []
        state(guest)
        host.send_json(offer)
        assert host.receive_json()["type"] == "error"
        host.send_json({"action": "relay_stop"})
        assert state(host)["relay"]["source_id"] is None
        state(guest)


def test_guest_source_requires_host_approval_and_only_leads_spotify():
    with party() as (_, room, host, guest, initial):
        lobby = LOBBIES[room["lobby_id"]]
        item = lobby.queue_items[0]
        item.track_id = "spotify_" + "a" * 22
        guest_id = next(m["member_id"] for m in initial["members"] if m["role"] == "Member")
        guest.send_json({"action": "relay_request"})
        assert state(guest)["relay"]["source_id"] is None
        state(host)
        guest.send_json({"action": "relay_grant", "member_id": guest_id})
        assert guest.receive_json()["type"] == "error"
        host.send_json({"action": "relay_grant", "member_id": guest_id})
        assert state(host)["relay"]["source_id"] == guest_id
        state(guest)
        command = {"action": "playback_sync", "item_id": item.id, "expected_revision": 0,
                   "position": 46, "duration": 180, "paused": False}
        guest.send_json(command)
        assert host.receive_json()["position"] == 46
        host.send_json({**command, "position": 2})
        host.send_json({"action": "playback_ping", "client_time": 1})
        assert host.receive_json()["type"] == "playback_clock"
        assert lobby.playback["position"] == 46
        control = {"action": "relay_control", "item_id": item.id,
                   "expected_revision": 0, "command": "pause"}
        guest.send_json(control)
        assert guest.receive_json()["type"] == "error"
        host.send_json(control)
        assert guest.receive_json()["command"] == "pause"
        ended = {"action": "skip_track", "expected_revision": 0,
                 "ended": True, "item_id": item.id}
        guest.send_json({**ended, "item_id": "f" * 32})
        assert guest.receive_json()["type"] == "error"
        guest.send_json(ended)
        assert state(host)["tracks_queue"] == []
        state(guest)
        guest.send_json(ended)
        assert guest.receive_json()["type"] == "error"
        host.send_json({"action": "add_track", "track_id": "dQw4w9WgXcQ"})
        state(host)
        following = state(guest)
        guest.send_json({**command, "item_id": following["queue_items"][0]["id"],
                         "expected_revision": following["playback_revision"]})
        assert guest.receive_json()["error"] == "main_only"
        guest.close()
        assert state(host)["relay"]["source_id"] is None


def test_turn_credentials_are_short_lived_and_do_not_expose_shared_secret(monkeypatch):
    secret = "test-server-only-shared-secret"
    monkeypatch.setenv("RELAY_TURN_SECRET", secret)
    monkeypatch.setenv("RELAY_TURN_URLS", '["turn:example.org:3478?transport=udp"]')
    servers = relay_ice("room", "member")
    turn = servers[-1]
    expiry = int(turn["username"].split(":")[0])
    assert 3500 < expiry - time.time() <= 3600
    expected = base64.b64encode(hmac.new(secret.encode(), turn["username"].encode(),
                                       hashlib.sha1).digest()).decode()
    assert turn["credential"] == expected
    assert secret not in str(servers)
