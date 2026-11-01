import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.lobby import LOBBIES
from app.main import app


def test_shared_room_can_be_reclaimed_and_expired() -> None:
    with TestClient(app) as client:
        room = client.post('/api/lobby/create').json()
        path = f"/api/lobby/{room['lobby_id']}/ws"
        with client.websocket_connect(path) as host:
            host.send_json({'host_token': room['host_token']})
            host.receive_json()
            host.receive_json()
            with client.websocket_connect(path) as guest:
                guest.send_json({'member_token': 'a' * 64})
                guest.receive_json()
                guest.receive_json()
                host.receive_json()
                guest.close()
            host.receive_json()
            host.close()
        lobby = LOBBIES[room['lobby_id']]
        assert lobby.ever_shared and lobby.vacated
        with client.websocket_connect(path) as guest:
            guest.send_json({'member_token': 'a' * 64})
            guest.receive_json()
            guest.receive_json()
            granted = guest.receive_json()
            assert granted['type'] == 'host_granted'
            assert granted['host_token'] != room['host_token']
        lobby.last_empty = time.monotonic() - 901
        with client.websocket_connect(path) as expired:
            with pytest.raises(WebSocketDisconnect) as closed:
                expired.receive_json()
            assert closed.value.reason == 'party_ended'


def test_host_can_transfer_control_without_exposing_token() -> None:
    with TestClient(app) as client:
        room = client.post('/api/lobby/create').json()
        path = f"/api/lobby/{room['lobby_id']}/ws"
        with client.websocket_connect(path) as host:
            host.send_json({'host_token': room['host_token'], 'member_token': 'b' * 64})
            host.receive_json()
            host.receive_json()
            with client.websocket_connect(path) as guest:
                guest.send_json({'member_token': 'a' * 64})
                guest.receive_json()
                state = guest.receive_json()
                host.receive_json()
                target = next(m['member_id'] for m in state['members'] if m['role'] == 'Member')
                host.send_json({'action': 'transfer_host', 'member_id': target})
                granted = guest.receive_json()
                assert granted['type'] == 'host_granted'
                assert host.receive_json() == {'type': 'host_revoked'}
                assert granted['host_token'] != room['host_token']
                assert 'host_token' not in guest.receive_json()
                assert 'host_token' not in host.receive_json()
