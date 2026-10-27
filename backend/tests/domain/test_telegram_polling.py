import asyncio
import importlib
import json
from contextlib import suppress

import httpx

lobby = importlib.import_module("app.api.lobby")


def test_polling_uses_proxy_replies_and_acknowledges_updates(monkeypatch):
    monkeypatch.setattr(lobby, "telegram_bot_token", "test-token")
    monkeypatch.setattr(lobby, "telegram_proxy_url", "http://vpn:1080")
    monkeypatch.setattr(lobby, "telegram_mini_app_url", "https://music.example.com")
    monkeypatch.setattr(lobby, "TELEGRAM_STATUS", {"connected": False, "polls": 0})
    real_client = httpx.AsyncClient

    async def run():
        done = asyncio.Event()
        calls = []

        async def respond(request):
            method = request.url.path.rsplit("/", 1)[-1]
            payload = json.loads(request.content)
            calls.append((method, payload))
            if method == "getUpdates":
                if payload["offset"] == 0:
                    result = [{"update_id": 1, "message": {"text": "/start",
                              "chat": {"type": "private", "id": 123}}}]
                else:
                    done.set()
                    await asyncio.Event().wait()
            else:
                result = True
            return httpx.Response(200, json={"ok": True, "result": result})

        def client(*, proxy, trust_env, **kwargs):
            assert proxy == "http://vpn:1080" and trust_env is False
            return real_client(transport=httpx.MockTransport(respond))

        monkeypatch.setattr(lobby.httpx, "AsyncClient", client)
        task = asyncio.create_task(lobby.telegram_polling_loop())
        try:
            await asyncio.wait_for(done.wait(), 2)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        assert [method for method, _ in calls] == ["deleteWebhook", "getUpdates", "sendMessage", "getUpdates"]
        assert calls[-1][1]["offset"] == 2
        reply = calls[2][1]
        assert reply["chat_id"] == 123
        assert reply["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"] == "https://music.example.com"
        assert lobby.TELEGRAM_STATUS["connected"] is True

    asyncio.run(run())
