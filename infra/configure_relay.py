"""Generate private coturn configuration and matching short-lived API credentials."""

import argparse
import ipaddress
import json
import os
import re
import secrets
import tempfile
from pathlib import Path


def configure(directory: Path, public_ip: str) -> None:
    address = ipaddress.IPv4Address(public_ip)
    if not address.is_global:
        raise ValueError("A public IPv4 address is required")
    env = directory / ".env"
    lines = env.read_text().splitlines()
    values = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    secret = values.get("RELAY_TURN_SECRET", "")
    if not re.fullmatch(r"[a-f0-9]{64}", secret):
        secret = secrets.token_hex(32)
    profiles = list(dict.fromkeys([*filter(None, values.get("COMPOSE_PROFILES", "").strip('\"\'').split(",")), "relay"]))
    updates = {"RELAY_TURN_SECRET": secret, "RELAY_TURN_URLS": json.dumps([
        f"turn:{address}:3478?transport=udp", f"turn:{address}:3478?transport=tcp"]),
        "COMPOSE_PROFILES": ",".join(profiles), "TURN_UID": str(os.getuid()),
        "TURN_GID": str(os.getgid())}
    content = []
    for line in lines:
        key = line.partition("=")[0]
        content.append(f"{key}={updates.pop(key)}" if key in updates else line)
    content.extend(f"{key}={value}" for key, value in updates.items())
    turn = directory / ".local" / "turn"
    turn.mkdir(parents=True, exist_ok=True)
    turn.chmod(0o700)
    config = f"""listening-ip={address}
relay-ip={address}
listening-port=3478
min-port=49160
max-port=49359
realm=prada-sound.space
use-auth-secret
static-auth-secret={secret}
fingerprint
relay-threads=1
total-quota=160
user-quota=24
max-bps=64000
bps-capacity=8388608
stale-nonce=600
no-cli
no-tls
no-tcp-relay
no-multicast-peers
no-rfc5780
denied-peer-ip=0.0.0.0-0.255.255.255
denied-peer-ip=10.0.0.0-10.255.255.255
denied-peer-ip=100.64.0.0-100.127.255.255
denied-peer-ip=127.0.0.0-127.255.255.255
denied-peer-ip=169.254.0.0-169.254.255.255
denied-peer-ip=172.16.0.0-172.31.255.255
denied-peer-ip=192.168.0.0-192.168.255.255
denied-peer-ip=224.0.0.0-255.255.255.255
pidfile=/tmp/turnserver.pid
userdb=/tmp/turn.sqlite
log-file=/dev/null
simple-log
no-stdout-log
"""
    for path, text in ((turn / "turnserver.conf", config), (env, "\n".join(content) + "\n")):
        descriptor, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(descriptor, "w") as output:
                output.write(text)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-ip", required=True)
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parents[1])
    options = parser.parse_args()
    configure(options.directory, options.public_ip)
    print("Private TURN configuration prepared; no credentials printed.")
