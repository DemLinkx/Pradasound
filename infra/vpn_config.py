"""Convert a private VLESS subscription into a small sing-box proxy config."""

import argparse
import base64
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def build(subscription: str, indices: list[int], pinned_ports: bool = False) -> dict:
    text = subscription.strip()
    if not text.startswith("vless://"):
        text = base64.b64decode(text + "=" * (-len(text) % 4)).decode()
    links = [line.strip() for line in text.splitlines() if line.startswith("vless://")]
    outbounds = []
    for index in indices:
        url = urlsplit(links[index])
        query = parse_qs(url.query)
        get = lambda key, default="": query.get(key, [default])[0]
        if not url.hostname or not url.username or get("security") not in ("reality", "tls"):
            raise ValueError("Unsupported VLESS profile")
        tls = {"enabled": True, "server_name": get("sni") or url.hostname,
               "utls": {"enabled": True, "fingerprint": get("fp", "chrome")}}
        if get("security") == "reality":
            tls["reality"] = {"enabled": True, "public_key": get("pbk"),
                              "short_id": get("sid")}
        node = {"type": "vless", "tag": f"node-{index}", "server": url.hostname,
                "server_port": url.port or 443, "uuid": url.username,
                "tls": tls, "connect_timeout": "10s"}
        if get("flow"):
            node["flow"] = get("flow")
        if get("type", "tcp") == "ws":
            node["transport"] = {"type": "ws", "path": get("path", "/"),
                                 "headers": {"Host": get("host") or get("sni")}}
        elif get("type", "tcp") != "tcp":
            raise ValueError("Unsupported transport")
        outbounds.append(node)
    inbounds = [{"type": "mixed", "tag": "proxy", "listen": "0.0.0.0", "listen_port": 1080}]
    rules = []
    if pinned_ports:
        for offset, node in enumerate(outbounds, 1):
            tag = f"pinned-{offset}"
            inbounds.append({"type": "mixed", "tag": tag, "listen": "0.0.0.0",
                             "listen_port": 1080 + offset})
            rules.append({"inbound": [tag], "outbound": node["tag"]})
    return {"log": {"level": "warn", "timestamp": True},
            "dns": {"servers": [{"type": "local", "tag": "local"}], "final": "local"},
            "inbounds": inbounds,
            "outbounds": [{"type": "urltest", "tag": "auto",
                           "outbounds": [node["tag"] for node in outbounds],
                           "url": "https://www.gstatic.com/generate_204", "interval": "3m",
                           "tolerance": 100, "interrupt_exist_connections": False}, *outbounds],
            "route": {"rules": rules, "final": "auto", "default_domain_resolver": "local"}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subscription", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--nodes", default="14,10,0")
    parser.add_argument("--pinned-ports", action="store_true")
    args = parser.parse_args()
    try:
        config = build(args.subscription.read_text(), [int(n) for n in args.nodes.split(",")], args.pinned_ports)
    except (ValueError, IndexError, KeyError):
        raise SystemExit("Invalid or unsupported subscription; no credentials printed") from None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.touch(mode=0o600, exist_ok=True)
    args.output.chmod(0o600)
    args.output.write_text(json.dumps(config, indent=2) + "\n")
    print(f"Created config with {len(config['outbounds']) - 1} VPN nodes")
