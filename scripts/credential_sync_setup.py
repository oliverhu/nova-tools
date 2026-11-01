"""Explicit, resumable local enrollment; never connects or copies credentials."""
import hashlib
import os
from pathlib import Path
import shlex
import tempfile
import uuid

from credential_sync import (Store, SyncError, atomic_write, bounded_process, canonical,
                             json_bytes, known_host, lock_file, private_dir, public_key,
                             read_file, read_json)

FIELDS = {"enabled", "install_dir", "data_dir", "python", "peer"}
PEER_FIELDS = {"host", "user", "port", "host_key", "host_fingerprint", "store_id"}


def validate(config):
    if not isinstance(config, dict) or set(config) != FIELDS or type(config["enabled"]) is not bool:
        raise SyncError("Use the documented credential_sync configuration fields")
    if not config["enabled"]:
        return
    for key in ("install_dir", "data_dir", "python"):
        if not isinstance(config[key], str) or not config[key]:
            raise SyncError("Credential-sync paths must be nonempty strings")
    install, data, python = (canonical(config[k]) for k in ("install_dir", "data_dir", "python"))
    if any((p / ".git").exists() for p in (data, *data.parents)):
        raise SyncError("Credential data must stay outside source repositories")
    if install == data or install in data.parents or data in install.parents:
        raise SyncError("Use separate, non-nested installation and data directories")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise SyncError("Select an installed executable Python interpreter")
    peer = config["peer"]
    if peer is not None:
        if not isinstance(peer, dict) or set(peer) != PEER_FIELDS:
            raise SyncError("Use the documented explicit peer fields")
        known_host(peer)  # Verify supplied key against independently obtained fingerprint before writes.
        try:
            if str(uuid.UUID(peer["store_id"])) != peer["store_id"]:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise SyncError("Peer store ID must be obtained explicitly from that peer") from None


def install(config):
    validate(config)
    if not config["enabled"]:
        return {"enabled": False, "changes_performed": False}
    root, data, python = (canonical(config[k]) for k in ("install_dir", "data_dir", "python"))
    source = Path(__file__).with_name("credential_sync.py").read_bytes()
    binding = {"schema": 2, "install_dir": str(root), "data_dir": str(data), "python": str(python),
               "source_sha256": hashlib.sha256(source).hexdigest()}
    # First install may use only empty directories. A matching journal permits a resume.
    if root.exists() and read_json(root / "installation.json") is None and any(root.iterdir()):
        raise SyncError("Unrecognized installation directory; existing files preserved")
    if data.exists() and read_json(data / "setup-binding.json") is None and any(data.iterdir()):
        raise SyncError("Existing data is not this enrollment; use a new empty store")
    root = private_dir(root)
    # Write intent before the first resumable operation, including lock creation.
    atomic_write(root / "installation.json", json_bytes(binding), replace=False)
    with lock_file(root / "install.lock"):
        old = read_json(root / "installation.json")
        if old is not None and old != binding:
            raise SyncError("Installation binding changed; use explicit migration, existing identity preserved")
        if data.exists() and read_json(data / "setup-binding.json") is None and any(data.iterdir()):
            raise SyncError("Existing data is not this enrollment; use a new empty store")
        atomic_write(root / "installation.json", json_bytes(binding), replace=False)
        private_dir(data)
        atomic_write(data / "setup-binding.json", json_bytes(binding), replace=False)
        store = Store(data)
        atomic_write(root / "credential_sync.py", source, replace=False)
        keydir = private_dir(data / "identity")
        identity = keydir / "id_ed25519"
        if read_file(identity) is None:
            if os.path.lexists(str(identity) + ".pub"):
                raise SyncError("Public identity exists without its private key; preserved for inspection")
            # Unique private staging directory; canonical key is linked once, never replaced.
            with tempfile.TemporaryDirectory(prefix=".key-stage-", dir=keydir) as staging:
                temp = Path(staging) / "key"
                bounded_process(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "nova-credential-sync-v1", "-f", str(temp)], limit=4096)
                checked = read_file(temp)
                if checked is None:
                    raise SyncError("SSH identity generation did not finish")
                os.link(temp, identity)
        # If interrupted after installing the private key, regenerate only its public half.
        public = " ".join(bounded_process(["/usr/bin/ssh-keygen", "-y", "-P", "", "-f", str(identity)], limit=4096).decode().split()[:2])
        public, fingerprint = public_key(public)
        atomic_write(Path(str(identity) + ".pub"), (public + "\n").encode(), replace=False)
        marker = {"fingerprint": fingerprint, "store_id": store.store_id}
        atomic_write(data / "identity.json", json_bytes(marker), replace=False)
        peer = config["peer"]
        if peer is not None:
            hosts = data / "known_hosts"
            atomic_write(hosts, known_host(peer), replace=False)
            settings = dict(peer, schema=2, identity=str(identity), identity_fingerprint=fingerprint,
                            known_hosts=str(hosts))
            atomic_write(data / "peer.json", json_bytes(settings), replace=False)
        elif read_json(data / "peer.json") is not None:
            raise SyncError("Peer is already configured; omission does not remove or replace it")
        return {"enabled": True, "store_id": store.store_id, "public_key_file": str(identity) + ".pub",
                "identity_fingerprint": fingerprint, "peer_configured": peer is not None,
                "peer_key_enrolled": False, "credential_transfer_performed": False,
                "next_step": "Review and enroll only this public key on the intended peer; no SSH access was changed here"}


def enrollment_line(install_dir, public):
    root = canonical(install_dir)
    binding = read_json(root / "installation.json")
    if not binding or binding.get("install_dir") != str(root) or binding.get("schema") != 2:
        raise SyncError("Unknown credential-sync installation")
    script = root / "credential_sync.py"
    raw = read_file(script)
    if raw is None or hashlib.sha256(raw).hexdigest() != binding["source_sha256"]:
        raise SyncError("Installed receiver differs from its enrollment binding")
    data = canonical(binding["data_dir"])
    if read_json(data / "setup-binding.json") != binding:
        raise SyncError("Receiver data binding changed")
    key, fingerprint = public_key(public)
    command = shlex.join([binding["python"], str(script), "--root", str(data), "serve"])
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    return 'restrict,command="' + escaped + '" ' + key + " nova-credential-sync-v1", fingerprint


def enroll(install_dir, public, ssh_dir, apply=False):
    """Run locally on the peer after reviewing the public key. No remote shell."""
    line, fingerprint = enrollment_line(install_dir, public)
    key, _ = public_key(public)
    blob = key.split()[1]
    if not apply:
        return {"mode": "plan", "fingerprint": fingerprint, "authorized_keys_line": line, "changes_performed": False}
    ssh = private_dir(ssh_dir)
    target = ssh / "authorized_keys"
    with lock_file(ssh / "nova-credential-enrollment.lock"):
        old = read_file(target) or b""
        try:
            lines = old.decode().splitlines()
        except UnicodeError:
            raise SyncError("Existing authorized_keys cannot be safely inspected; preserved") from None
        occurrences = [entry for entry in lines if blob in entry]
        if occurrences:
            if occurrences != [line]:
                raise SyncError("Key has ambiguous or different restrictions; SSH access preserved, reconcile explicitly")
            return {"mode": "applied", "fingerprint": fingerprint, "changed": False}
        # Atomic append preserves unrelated entries and independent appends; no key removal.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            from credential_sync import checked_stat
            checked_stat(os.fstat(fd))
            if (read_file(target) or b"") != old:
                raise SyncError("SSH enrollment changed concurrently; inspect before retry")
            addition = (("\n" if old and not old.endswith(b"\n") else "") + line + "\n").encode()
            if os.write(fd, addition) != len(addition):
                raise SyncError("Partial enrollment write; inspect before retry")
            os.fsync(fd)
        finally:
            os.close(fd)
        result = read_file(target).decode().splitlines()
        if [entry for entry in result if blob in entry] != [line]:
            raise SyncError("Enrollment readback is ambiguous; inspect before retry")
        return {"mode": "applied", "fingerprint": fingerprint, "changed": True}
