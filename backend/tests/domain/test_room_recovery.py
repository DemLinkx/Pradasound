import json
import stat
import time

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.api import lobby as rooms


def test_encrypted_recovery_preserves_queue_votes_and_host_but_not_live_access(tmp_path, monkeypatch):
    monkeypatch.setattr(rooms.room_settings, 'room_state_file', tmp_path / 'rooms.enc')
    key = Fernet.generate_key()
    monkeypatch.setattr(rooms.room_settings, 'room_state_key', SecretStr(key.decode()))
    monkeypatch.setattr(rooms, 'LOBBIES', {})
    room = rooms.Lobby(host_token='private-host-secret', ever_connected=True, ever_shared=True)
    item = rooms.QueueItem('a' * 32, 'dQw4w9WgXcQ', 'b' * 32, added_order=7, votes={'c' * 32: 1})
    room.queue_items.append(item)
    room.queue_revision = 9
    room.owner_ids['private-host-secret'] = 'b' * 32
    room.listener_tokens['private-listener-secret'] = 'connection'
    room.connections['connection'] = object()
    rooms.LOBBIES['f' * 32] = room
    payload = rooms.room_records()
    assert b'private-listener-secret' not in payload
    rooms.write_room_records(payload)
    path = rooms.room_settings.room_state_file
    encrypted = path.read_bytes()
    assert b'private-host-secret' not in encrypted and b'dQw4w9WgXcQ' not in encrypted
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    rooms.LOBBIES.clear()
    rooms.load_rooms()
    restored = rooms.LOBBIES['f' * 32]
    assert restored.host_token == 'private-host-secret'
    assert restored.queue_revision == 9 and restored.queue_items[0].rating == 1
    assert restored.queue_items[0].added_order == 7
    assert restored.listener_tokens == {} and restored.connections == {} and restored.main_id is None
    assert not rooms.is_expired(restored)
    assert not path.with_suffix('.tmp').exists()
    # Integrity failure never silently replaces the recovery file with an empty room list.
    path.write_bytes(encrypted[:-1] + bytes([encrypted[-1] ^ 1]))
    with pytest.raises(RuntimeError, match='recovery failed'):
        rooms.load_rooms()


def test_started_solo_room_survives_a_mobile_disconnect():
    room = rooms.Lobby('host', ever_connected=True)
    room.last_empty = time.monotonic() - 35
    assert not rooms.is_expired(room)
    room.last_empty -= 900
    assert rooms.is_expired(room)
    never_started = rooms.Lobby('host')
    never_started.last_empty -= 31
    assert rooms.is_expired(never_started)


def test_recovery_does_not_extend_an_already_empty_rooms_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(rooms.room_settings, 'room_state_file', tmp_path / 'rooms.enc')
    monkeypatch.setattr(rooms.room_settings, 'room_state_key', SecretStr(Fernet.generate_key().decode()))
    monkeypatch.setattr(rooms, 'LOBBIES', {})
    room = rooms.Lobby('host', ever_connected=True)
    room.last_empty -= 800
    rooms.LOBBIES['f' * 32] = room
    payload = json.loads(rooms.room_records())
    payload['rooms']['f' * 32]['empty_since'] -= 101
    rooms.write_room_records(json.dumps(payload).encode())
    rooms.LOBBIES.clear()
    rooms.load_rooms()
    assert rooms.LOBBIES == {}


def test_repeated_stale_disconnect_cannot_pause_or_expire_a_room(monkeypatch):
    room = rooms.Lobby('host', ever_connected=True)
    room.connections['live'] = object()
    monkeypatch.setattr(rooms, 'LOBBIES', {'room': room})
    before = room.last_empty
    rooms.manager.disconnect('room', 'old-connection')
    assert room.connections and room.last_empty == before and not room.vacated
