import importlib

from fastapi.testclient import TestClient

from app.main import app


def test_production_does_not_extract_youtube_audio(monkeypatch):
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main.settings, "youtube_audio_enabled", False)

    def forbidden_extraction(*args, **kwargs):
        raise AssertionError("yt-dlp must not run in production")

    monkeypatch.setattr(main, "extract_youtube_audio", forbidden_extraction)
    with TestClient(app) as client:
        room = client.post("/api/lobby/create").json()
        response = client.post(
            "/api/youtube/audio/dQw4w9WgXcQ",
            headers={"X-Lobby-Id": room["lobby_id"], "X-Host-Token": room["host_token"]},
        )
        assert response.status_code == 503
        assert "YouTube" in response.json()["detail"]


def test_healthcheck():
    with TestClient(app) as client:
        response = client.get("/healthcheck")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
