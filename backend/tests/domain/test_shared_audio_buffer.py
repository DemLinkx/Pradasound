import asyncio

import pytest
from fastapi import HTTPException

from app import main


def test_listeners_share_upstream_blocks_with_correct_ranges_and_budget(monkeypatch):
    async def check():
        monkeypatch.setattr(main, 'MEDIA_PARTS', main.OrderedDict())
        monkeypatch.setattr(main, 'MEDIA_PENDING', {})
        monkeypatch.setattr(main, 'MEDIA_BYTES', 0)
        monkeypatch.setattr(main, 'MEDIA_LOAD_LIMIT', asyncio.Semaphore(4))
        body = bytes(range(256)) * 2200
        calls = []

        async def fetch(byte_range, limit):
            calls.append(byte_range)
            await asyncio.sleep(.005)
            start, end = (int(n) for n in byte_range[6:].split('-'))
            end = min(end, len(body) - 1)
            return 206, {'content-range': f'bytes {start}-{end}/{len(body)}', 'content-type': 'audio/mp4'}, body[start:end + 1]

        async def listen(byte_range):
            response = await main.shared_audio_response('track', byte_range, fetch)
            return response, b''.join([part async for part in response.body_iterator])

        clients = await asyncio.gather(*(listen('bytes=0-300000') for _ in range(12)))
        assert all(content == body[:300001] for _, content in clients)
        assert calls.count('bytes=0-0') == 1
        assert calls.count(f'bytes=0-{main.MEDIA_BLOCK - 1}') == 1
        assert calls.count(f'bytes={main.MEDIA_BLOCK}-{main.MEDIA_BLOCK * 2 - 1}') == 1
        suffix, content = await listen('bytes=-99')
        assert content == body[-99:]
        assert suffix.status_code == 206
        assert suffix.headers['content-length'] == '99'
        assert main.MEDIA_BYTES <= main.MEDIA_BUDGET
        for invalid in ('bytes=-', 'bytes=999999999999-', 'bytes=4-2', 'bytes=0-1,4-5'):
            with pytest.raises(HTTPException) as error:
                await listen(invalid)
            assert error.value.status_code == 416
        monkeypatch.setattr(main, 'MEDIA_BUDGET', main.MEDIA_BLOCK)
        await main.media_part(('other', '0'), lambda: fetch('bytes=0-9', 10))
        assert main.MEDIA_BYTES <= main.MEDIA_BUDGET
        assert main.MEDIA_PENDING == {}
    asyncio.run(check())


def test_failed_fragment_is_not_cached_or_shared_as_success(monkeypatch):
    async def check():
        monkeypatch.setattr(main, 'MEDIA_PARTS', main.OrderedDict())
        monkeypatch.setattr(main, 'MEDIA_PENDING', {})
        monkeypatch.setattr(main, 'MEDIA_BYTES', 0)
        monkeypatch.setattr(main, 'MEDIA_LOAD_LIMIT', asyncio.Semaphore(4))
        attempts = 0

        async def load():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise HTTPException(502, 'upstream unavailable')
            return 200, {}, b'audio'

        with pytest.raises(HTTPException):
            await main.media_part(('one', 'part'), load)
        assert ('one', 'part') not in main.MEDIA_PARTS
        assert (await main.media_part(('one', 'part'), load))[2] == b'audio'
        assert attempts == 2
    asyncio.run(check())
