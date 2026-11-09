#!/usr/bin/env python3
"""Enable bounded encrypted room recovery; never print the key."""
import argparse
import base64
import os
from pathlib import Path
import secrets


def configure(root: Path) -> None:
    path = root / '.env'
    text = path.read_text()
    values = dict(line.split('=', 1) for line in text.splitlines() if '=' in line and not line.lstrip().startswith('#'))
    key = values.get('ROOM_STATE_KEY', '').strip()
    if not key:
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    if len(base64.urlsafe_b64decode(key)) != 32:
        raise ValueError('Existing ROOM_STATE_KEY is invalid; it was not replaced')
    updates = {'ROOM_STATE_FILE': '/app/state/rooms.enc', 'ROOM_STATE_KEY': key,
               'SERVER_PLAYBACK_ENABLED': 'true'}
    lines = []
    for line in text.splitlines():
        name = line.partition('=')[0]
        if name in updates:
            lines.append(name + '=' + updates.pop(name))
        else:
            lines.append(line)
    lines.extend(name + '=' + value for name, value in updates.items())
    temporary = path.with_suffix('.recovery.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write('\n'.join(lines) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    print('Encrypted room recovery and server playback configured; key not displayed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parent.parent)
    configure(parser.parse_args().project_root)
