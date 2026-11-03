import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

spec = spec_from_file_location("configure_relay", Path(__file__).parent.parent / "configure_relay.py")
relay = module_from_spec(spec)
spec.loader.exec_module(relay)


def test_relay_config_preserves_settings_and_private_secret_across_runs(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=keep-private\nCOMPOSE_PROFILES=vpn\n")
    relay.configure(tmp_path, "157.22.174.64")
    first = env.read_text()
    relay.configure(tmp_path, "157.22.174.64")
    assert env.read_text() == first
    values = dict(line.split("=", 1) for line in first.splitlines())
    assert values["TELEGRAM_BOT_TOKEN"] == "keep-private"
    assert values["COMPOSE_PROFILES"] == "vpn,relay"
    assert len(values["RELAY_TURN_SECRET"]) == 64
    assert len(json.loads(values["RELAY_TURN_URLS"])) == 2
    config = tmp_path / ".local/turn/turnserver.conf"
    assert values["RELAY_TURN_SECRET"] in config.read_text()
    assert config.stat().st_mode & 0o777 == env.stat().st_mode & 0o777 == 0o600
    assert "denied-peer-ip=127.0.0.0-127.255.255.255" in config.read_text()


def test_relay_config_rejects_private_bind_addresses_before_writing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("UNCHANGED=yes\n")
    with pytest.raises(ValueError):
        relay.configure(tmp_path, "127.0.0.1")
    assert env.read_text() == "UNCHANGED=yes\n"
    assert not (tmp_path / ".local").exists()
