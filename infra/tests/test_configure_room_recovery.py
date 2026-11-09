import importlib.util
from pathlib import Path
import stat

script = Path(__file__).resolve().parents[1] / 'configure_room_recovery.py'
spec = importlib.util.spec_from_file_location('configure_room_recovery', script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_configure_preserves_existing_secrets_and_generated_key(tmp_path, capsys):
    path = tmp_path / '.env'
    path.write_text('OTHER_SECRET=unchanged\nROOM_STATE_KEY=\nSERVER_PLAYBACK_ENABLED=false\n')
    module.configure(tmp_path)
    saved = path.read_text()
    assert 'OTHER_SECRET=unchanged' in saved
    assert 'SERVER_PLAYBACK_ENABLED=true' in saved
    assert 'ROOM_STATE_FILE=/app/state/rooms.enc' in saved
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    module.configure(tmp_path)
    assert path.read_text() == saved
    assert saved.partition('ROOM_STATE_KEY=')[2].splitlines()[0] not in capsys.readouterr().out
