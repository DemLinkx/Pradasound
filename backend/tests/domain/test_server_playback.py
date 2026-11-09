
from fastapi.testclient import TestClient

from app import main
from app.api import lobby as rooms


def until(ws, predicate):
    for _ in range(30):
        value = ws.receive_json()
        if predicate(value):
            return value
    raise AssertionError('Expected room event did not arrive')


def test_server_clock_ignores_device_pause_and_survives_host_disconnect(monkeypatch):
    rooms.LOBBIES.clear()
    monkeypatch.setattr(rooms.room_settings, 'server_playback_enabled', True)
    monkeypatch.setattr(main.settings, 'server_playback_enabled', True)

    async def descriptor(lobby_id, item_id):
        return {'url': '/api/youtube/audio/file/test', 'duration': 120}

    monkeypatch.setattr(main, 'room_stream_descriptor', descriptor)
    with TestClient(main.app) as client:
        created = client.post('/api/lobby/create').json()
        path = f"/api/lobby/{created['lobby_id']}/ws"
        with client.websocket_connect(path) as host:
            host.send_json({'host_token': created['host_token']})
            until(host, lambda s: s['type'] == 'lobby_state')
            for track in ('dQw4w9WgXcQ', 'aaaaaaaaaaa'):
                host.send_json({'action': 'add_track', 'track_id': track})
                until(host, lambda s: s['type'] == 'lobby_state' and s['tracks_queue'][-1] == track)
            with client.websocket_connect(path) as guest:
                guest.send_json({'member_token': 'a' * 64})
                until(guest, lambda s: s['type'] == 'lobby_state')
                guest.send_json({'action': 'listen_together', 'enabled': True})
                access = until(guest, lambda s: s['type'] == 'listener_access')
                state = until(host, lambda s: s['type'] == 'lobby_state' and (s.get('playback') or {}).get('authority') == 'server')
                room = rooms.LOBBIES[created['lobby_id']]
                first = state['queue_items'][0]['id']
                host.send_json({'action': 'playback_sync', 'item_id': first, 'expected_revision': 0,
                                'position': 50, 'duration': 120, 'paused': True})
                host.send_json({'action': 'playback_ping', 'client_time': 1})
                until(host, lambda s: s['type'] == 'playback_clock')
                assert room.playback['paused'] is False
                assert rooms.playback_snapshot(room)['position'] < 10
                # A genuine host button still controls the room.
                command = {'action': 'playback_control', 'item_id': first, 'expected_revision': 0}
                host.send_json({**command, 'command': 'pause'})
                until(host, lambda s: s['type'] == 'lobby_state' and (s.get('playback') or {}).get('paused') is True)
                host.send_json({**command, 'command': 'play'})
                until(host, lambda s: s['type'] == 'lobby_state' and (s.get('playback') or {}).get('paused') is False)
                host.close()
                until(guest, lambda s: s['type'] == 'lobby_state' and s['host_online'] is False)
                assert room.playback['paused'] is False
                assert client.get(f"/api/listen/{created['lobby_id']}/{first}/stream",
                                  headers={'X-Listener-Token': access['token']}).status_code == 200
                # Queue transitions use server time without an ended event from Main.
                room.clock_started -= 121
                changed = until(guest, lambda s: s['type'] == 'lobby_state' and s['tracks_queue'] == ['aaaaaaaaaaa'])
                assert changed['playback_revision'] == 1
                ready = until(guest, lambda s: s['type'] == 'lobby_state' and (s.get('playback') or {}).get('revision') == 1)
                assert ready['playback']['position'] >= 3
                guest.send_json({'action': 'playback_control', 'command': 'pause', 'item_id': ready['queue_items'][0]['id'], 'expected_revision': 1})
                assert until(guest, lambda s: s['type'] == 'error')['error'] == 'main_only'


def test_unsupported_sources_never_become_server_audio(monkeypatch):
    monkeypatch.setattr(rooms.room_settings, 'server_playback_enabled', True)
    for track in ('yandex_123', 'spotify_' + 'a' * 22, 'apple_123'):
        room = rooms.Lobby('host', queue_items=[rooms.QueueItem('a' * 32, track, 'b' * 32)])
        assert not rooms.server_head(room)
