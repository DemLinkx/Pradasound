"""Configure the bot from environment or repository-root .env after HTTPS deployment."""

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values


def main() -> None:
    config = {**dotenv_values(Path(__file__).resolve().parents[1] / ".env"), **os.environ}
    token = (config.get("TELEGRAM_BOT_TOKEN") or "").strip()
    secret = (config.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()
    site = (config.get("TELEGRAM_MINI_APP_URL") or "").strip().rstrip("/")
    parsed = urlsplit(site)
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and a valid TELEGRAM_WEBHOOK_SECRET")
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.path or parsed.query or parsed.fragment):
        raise SystemExit("TELEGRAM_MINI_APP_URL must be the public HTTPS origin")
    webhook = f"{site}/api/lobby/telegram/webhook"

    with httpx.Client(proxy=config.get("TELEGRAM_PROXY_URL") or None,
                      trust_env=False, timeout=20) as client:
        def call(method: str, payload: dict[str, object] | None = None) -> object:
            try:
                response = client.post(
                    f"https://api.telegram.org/bot{token}/{method}", json=payload or {}
                )
                data = response.json()
            except (httpx.HTTPError, ValueError):
                raise SystemExit(f"Telegram API unavailable: {method}") from None
            if not isinstance(data, dict) or not data.get("ok"):
                raise SystemExit(f"Telegram API rejected {method}; check bot settings")
            return data["result"]

        bot = call("getMe")
        if not isinstance(bot, dict) or not isinstance(bot.get("username"), str):
            raise SystemExit("Telegram did not return a bot identity")
        print(f"Bot identity verified: @{bot['username']}")
        call("setChatMenuButton", {"menu_button": {
            "type": "web_app", "text": "Открыть Prada Sound", "web_app": {"url": site},
        }})
        call("setMyCommands", {"commands": [
            {"command": "start", "description": "Открыть Prada Sound"},
            {"command": "help", "description": "Как начать вечеринку"},
        ]})
        call("setWebhook", {
            "url": webhook, "secret_token": secret,
            "allowed_updates": ["message"], "max_connections": 10,
        })
        info = call("getWebhookInfo")
        if not isinstance(info, dict) or info.get("url") != webhook:
            raise SystemExit("Telegram did not confirm the expected webhook URL")
        print(f"Menu, commands and webhook configured: {webhook}")


if __name__ == "__main__":
    main()
