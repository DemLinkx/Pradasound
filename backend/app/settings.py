from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    yandex_import_enabled: bool = False
    yandex_playback_enabled: bool = False
    youtube_audio_enabled: bool = True
    youtube_cookies_file: Path | None = None
    youtube_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_mini_app_url: str = ""
