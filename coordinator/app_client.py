"""Local app-session enrollment with a private, per-chat claim namespace."""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import tempfile


def write_private(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Session file must not be a symlink')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.app-session-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(value, output, indent=2); output.write('\n')
            output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def binding(client):
    path = getattr(client, 'app_session_path', None)
    if path is None:
        return None
    path = Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('Private regular app-session file required')
    value = json.loads(path.read_text())
    if value.get('worker') != client.config['worker'] or value.get('state') != 'registered':
        raise ValueError('Session is unregistered or belongs to another worker')
    return value['binding']


def attach(client, path):
    client.app_session_path = Path(path) if path else None
    if path:
        # Claim files cannot accidentally revive another chat's old claim.
        digest = hashlib.sha256(str(Path(path).absolute()).encode()).hexdigest()
        client.state = client.path.parent / 'app-claims' / digest
        client.state.mkdir(parents=True, exist_ok=True, mode=0o700)


def _register(client, path, session_id, host_id, account_sha256, expected_epoch):
    path = Path(path)
    public = {'session_id': session_id, 'host_id': host_id,
              'account_sha256': account_sha256, 'expected_epoch': expected_epoch}
    if path.exists():
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError('Private regular session file required')
        value = json.loads(path.read_text())
        if value.get('worker') != client.config['worker'] or value.get('registration') != public:
            raise ValueError('Existing session file belongs to another enrollment')
    else:
        value = {'worker': client.config['worker'], 'registration': public,
                 'state': 'pending', 'binding': {'session_id': session_id,
                 'epoch': expected_epoch + 1, 'token': secrets.token_urlsafe(48)}}
        # Save before request so an ambiguous response retries the same session.
        write_private(path, value)
    result = client.request('/api/worker/app/register', {**public, 'session_token': value['binding']['token']})
    if (result.get('worker') != value['worker'] or result.get('session_id') != session_id
            or result.get('epoch') != expected_epoch + 1):
        raise RuntimeError('Registration response does not match local enrollment')
    value['state'] = 'registered'; write_private(path, value)
    attach(client, path)
    return result


def register(client, path, session_id, host_id, account_sha256, expected_epoch):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _register(client, path, session_id, host_id, account_sha256, expected_epoch)
