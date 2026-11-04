from app.api.lobby import LOBBIES
from test_listen_together import party


def test_votes_rank_pending_tracks_and_keep_current_playback():
    with party() as (_, room, host, guest, state):
        for track in ('aaaaaaaaaaa', 'bbbbbbbbbbb', 'ccccccccccc'):
            host.send_json({'action': 'add_track', 'track_id': track})
            state = host.receive_json()
            guest.receive_json()
        first, a, b, c = [item['id'] for item in state['queue_items']]
        revision = state['playback_revision']

        def vote(ws, item, value):
            ws.send_json({'action': 'vote_track', 'item_id': item, 'vote': value})
            h, g = host.receive_json(), guest.receive_json()
            assert h['queue_items'] == g['queue_items']
            assert h['playback_revision'] == revision
            assert h['queue_items'][0]['id'] == first
            return h, g

        h, g = vote(guest, c, 1)
        assert [i['id'] for i in h['queue_items']] == [first, c, a, b]
        assert g['my_votes'] == {c: 1} and h['my_votes'] == {}
        h, g = vote(guest, c, 1)
        assert h['queue_items'][1]['rating'] == 1  # retries don't add votes
        h, g = vote(host, b, 1)
        assert [i['id'] for i in h['queue_items']] == [first, b, c, a]
        h, g = vote(guest, c, -1)
        assert [i['id'] for i in h['queue_items']] == [first, b, a, c]
        assert h['queue_items'][-1]['rating'] == -1
        assert h['queue_items'][-1]['likes'] == 0
        assert h['queue_items'][-1]['dislikes'] == 1
        h, g = vote(host, b, 0)
        h, g = vote(guest, c, 0)
        assert [i['id'] for i in h['queue_items']] == [first, a, b, c]
        # A new neutral item belongs before disliked pending tracks.
        h, g = vote(guest, a, -1)
        host.send_json({'action': 'add_track', 'track_id': 'ddddddddddd'})
        h, g = host.receive_json(), guest.receive_json()
        assert h['tracks_queue'] == [state['tracks_queue'][0], 'bbbbbbbbbbb', 'ccccccccccc', 'ddddddddddd', 'aaaaaaaaaaa']
        # Current track can be rated without being restarted or displaced.
        h, g = vote(guest, first, -1)
        assert h['queue_items'][0]['rating'] == -1
        for value in (True, 2, '1', None):
            guest.send_json({'action': 'vote_track', 'item_id': a, 'vote': value})
            assert guest.receive_json()['error'] == 'invalid_track_vote'
        guest.send_json({'action': 'vote_track', 'item_id': '0' * 32, 'vote': 1})
        assert guest.receive_json()['error'] == 'invalid_track_vote'
        assert len(LOBBIES[room['lobby_id']].queue_items) == 5


def test_votes_survive_reconnection_without_exposing_other_voters():
    with party() as (client, room, host, guest, state):
        item = state['queue_items'][0]['id']
        guest.send_json({'action': 'vote_track', 'item_id': item, 'vote': 1})
        host.receive_json()
        guest.receive_json()
        with client.websocket_connect(f"/api/lobby/{room['lobby_id']}/ws") as reconnect:
            reconnect.send_json({'member_token': 'a' * 64})
            reconnect.receive_json()
            restored = reconnect.receive_json()
            assert restored['my_votes'] == {item: 1}
            assert restored['queue_items'][0]['rating'] == 1
