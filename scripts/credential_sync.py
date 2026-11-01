#!/usr/bin/env python3
"""On-demand named-file sync. Values travel only through private files or SSH pipes."""
import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid

MAX_SECRET = 262144
MAX_MESSAGE = 400000
MAX_NAMES = 1024
DEFAULT_ROOT = "~/.local/share/nova-credential-sync"


class SyncError(ValueError):
    """Messages are static and must never contain untrusted data or credentials."""


def canonical(value):
    p = Path(value).expanduser().absolute()
    if p != p.resolve() or any(ord(c) < 32 or ord(c) == 127 for c in str(p)):
        raise SyncError("Paths must be canonical, without symlinks or control characters")
    return p


def private_dir(value):
    p = canonical(value)
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    s = p.stat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
        raise SyncError("Private directory must belong to this user with mode 0700")
    return p


def outside_repository(value):
    p = canonical(value)
    if any((parent / ".git").exists() for parent in (p, *p.parents)):
        raise SyncError("Credential data must stay outside source repositories")
    return p


def checked_stat(s, private=True):
    if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_nlink != 1
            or (private and s.st_mode & 0o077)):
        raise SyncError("Expected an owner-only regular file without additional links")


def read_file(value, limit=MAX_MESSAGE, missing=None, private=True):
    p = canonical(value)
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return missing
    with os.fdopen(fd, "rb") as f:
        checked_stat(os.fstat(f.fileno()), private)
        data = f.read(limit + 1)
    if len(data) > limit:
        raise SyncError("File exceeds its size bound")
    return data


def atomic_write(value, data, replace=True):
    p = canonical(value)
    private_dir(p.parent)
    prior = read_file(p)
    if not replace and prior is not None:
        if prior != data:
            raise SyncError("Existing file differs; preserved")
        return
    fd, name = tempfile.mkstemp(dir=p.parent, prefix=".sync-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if replace:
            if read_file(p) != prior:
                raise SyncError("File changed concurrently; preserved")
            os.replace(name, p)
        else:
            os.link(name, p)  # Never replace a concurrently installed file.
    finally:
        if os.path.exists(name):
            os.unlink(name)


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def read_json(path, default=None):
    raw = read_file(path)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise SyncError("Invalid private JSON file") from None


@contextmanager
def lock_file(value):
    p = canonical(value)
    private_dir(p.parent)
    fd = os.open(p, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        checked_stat(os.fstat(fd))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("Another operation owns this store; try after it settles") from None
        yield
    finally:
        os.close(fd)


def name_check(name):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise SyncError("Invalid credential name")
    return name


def digest_check(digest, nullable=False):
    if nullable and digest is None:
        return digest
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise SyncError("Invalid credential digest")
    return digest


def raw_record(record):
    try:
        if not isinstance(record, dict) or set(record) != {"data", "digest"}:
            raise ValueError()
        raw = base64.b64decode(record["data"], validate=True)
        if not 0 < len(raw) <= MAX_SECRET or hashlib.sha256(raw).hexdigest() != digest_check(record["digest"]):
            raise ValueError()
        return raw
    except (ValueError, TypeError, KeyError):
        raise SyncError("Credential record failed integrity or size validation") from None


def public_key(value):
    """Only Ed25519 is supported; parse the wire type and exact key length."""
    try:
        parts = value.split()
        if len(parts) != 2 or parts[0] != "ssh-ed25519":
            raise ValueError()
        wire = base64.b64decode(parts[1], validate=True)
        expected = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
        if len(wire) != 51 or not wire.startswith(expected):
            raise ValueError()
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(wire).digest()).decode().rstrip("=")
        return " ".join(parts), fingerprint
    except (ValueError, TypeError, AttributeError):
        raise SyncError("Expected a valid Ed25519 public key without a comment") from None


class Store:
    def __init__(self, root):
        target = outside_repository(root)
        self.root = private_dir(target)
        self.records = private_dir(self.root / "credentials")
        with self.lock():
            identity = read_json(self.root / "store.json")
            if identity is None:
                # A legacy store must be migrated deliberately, not silently adopted.
                if any(self.records.iterdir()) or (self.root / "baselines.json").exists():
                    raise SyncError("Legacy store requires explicit migration to a new empty store")
                identity = {"schema": 2, "store_id": str(uuid.uuid4())}
                atomic_write(self.root / "store.json", json_bytes(identity), replace=False)
            if (not isinstance(identity, dict) or identity.get("schema") != 2
                    or str(uuid.UUID(identity.get("store_id", ""))) != identity["store_id"]):
                raise SyncError("Invalid store identity")
            self.store_id = identity["store_id"]

    def lock(self):
        return lock_file(self.root / "store.lock")

    def path(self, name):
        return self.records / (name_check(name) + ".json")

    def get(self, name):
        record = read_json(self.path(name))
        if record is None:
            raise SyncError("Named credential does not exist")
        raw_record(record)
        return record

    def digest(self, name):
        p = self.path(name)
        return self.get(name)["digest"] if os.path.lexists(p) else None

    def inventory(self):
        files = list(self.records.iterdir())
        if len(files) > MAX_NAMES:
            raise SyncError("Store exceeds the named-file limit")
        if any(p.suffix != ".json" for p in files):
            raise SyncError("Unexpected file in credential store")
        return {p.stem: self.get(p.stem)["digest"] for p in files}

    def put(self, name, raw, expected):
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_SECRET:
            raise SyncError("Credential must be 1–262144 bytes")
        digest_check(expected, nullable=True)
        if self.digest(name) != expected:
            raise SyncError("Credential changed concurrently; sync again")
        if expected is None and len(self.inventory()) >= MAX_NAMES:
            raise SyncError("Store exceeds the named-file limit")
        digest = hashlib.sha256(raw).hexdigest()
        atomic_write(self.path(name), json_bytes({"data": base64.b64encode(raw).decode(), "digest": digest}))
        return digest


def handle(store, request):
    if not isinstance(request, dict):
        raise SyncError("Invalid protocol request")
    op = request.get("op")
    keys = {"inventory": {"op"}, "get": {"op", "name"}, "put": {"op", "name", "data", "expected"}}
    if op not in keys or set(request) - {"store_id"} != keys[op]:
        raise SyncError("Unsupported protocol request")
    if "store_id" in request and request["store_id"] != store.store_id:
        raise SyncError("Peer store identity changed; no operation performed")
    with store.lock():
        if op == "inventory":
            return {"ok": True, "store_id": store.store_id, "credentials": store.inventory()}
        if op == "get":
            return {"ok": True, "record": store.get(request["name"])}
        try:
            raw = base64.b64decode(request["data"], validate=True)
        except (ValueError, TypeError):
            raise SyncError("Invalid encoded credential") from None
        return {"ok": True, "digest": store.put(request["name"], raw, request["expected"])}


def bounded_process(args, data=b"", timeout=30, limit=MAX_MESSAGE):
    """Bound time and both output streams; reap the exact child on all outcomes."""
    with subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as p:
        with selectors.DefaultSelector() as selector:
            for stream in (p.stdin, p.stdout, p.stderr):
                os.set_blocking(stream.fileno(), False)
            selector.register(p.stdout, selectors.EVENT_READ, "out")
            selector.register(p.stderr, selectors.EVENT_READ, "err")
            if data:
                selector.register(p.stdin, selectors.EVENT_WRITE, "in")
            else:
                p.stdin.close()
            output, sent, size = bytearray(), 0, 0
            deadline = time.monotonic() + timeout
            try:
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise SyncError("Peer operation timed out; inspect state before retry")
                    for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                        if key.data == "in":
                            sent += os.write(key.fd, data[sent:sent + 16384])
                            if sent == len(data):
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                        else:
                            chunk = os.read(key.fd, 16384)
                            if not chunk:
                                selector.unregister(key.fileobj)
                            else:
                                size += len(chunk)
                                if size > limit:
                                    raise SyncError("Peer output exceeds its size bound")
                                if key.data == "out":
                                    output.extend(chunk)
                code = p.wait(timeout=max(0.01, deadline - time.monotonic()))
                if code:
                    raise SyncError("Peer connection or command failed; no response data logged")
                return bytes(output)
            finally:
                if p.poll() is None:
                    p.kill()
                p.wait()


def known_host(config):
    key, fingerprint = public_key(config["host_key"])
    if fingerprint != config["host_fingerprint"]:
        raise SyncError("Peer host fingerprint mismatch")
    host = config["host"]
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", host):
        raise SyncError("Invalid peer hostname or address")
    if not isinstance(config["user"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", config["user"]):
        raise SyncError("Invalid peer SSH user")
    port = config["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise SyncError("Invalid peer SSH port")
    alias = host if port == 22 else "[" + host + "]:" + str(port)
    return (alias + " " + key + "\n").encode()


def ssh_args(config):
    if config.get("schema") != 2:
        raise SyncError("Peer needs explicit schema-2 enrollment; legacy baselines cannot be reused")
    expected = known_host(config)
    identity, hosts = canonical(config["identity"]), canonical(config["known_hosts"])
    if any(c in str(hosts) for c in ('%', '$', '\\', '"')):
        raise SyncError("Pinned host filename contains unsupported SSH expansion characters")
    if read_file(hosts) != expected or read_file(identity) is None:
        raise SyncError("Pinned host file or dedicated SSH identity is missing or changed")
    own_public = " ".join(bounded_process(["/usr/bin/ssh-keygen", "-y", "-P", "", "-f", str(identity)], limit=4096).decode().split()[:2])
    if public_key(own_public)[1] != config["identity_fingerprint"]:
        raise SyncError("Dedicated SSH identity changed; reenroll explicitly")
    if str(uuid.UUID(config["store_id"])) != config["store_id"]:
        raise SyncError("Invalid peer store identity")
    # -F disables inherited ProxyCommand/agent/forwarding/configuration surprises.
    return ["/usr/bin/ssh", "-F", "/dev/null", "-T", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "PreferredAuthentications=publickey", "-o", "ClearAllForwardings=yes",
            "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "PermitLocalCommand=no",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", 'UserKnownHostsFile="' + str(hosts).replace('"', '\\"') + '"',
            "-o", "HostKeyAlgorithms=ssh-ed25519", "-o", "UpdateHostKeys=no",
            "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
            "-i", str(identity), "-p", str(config["port"]), "-l", config["user"], config["host"], "credential-sync-v1"]


def remote_call(config, request):
    data = json_bytes(dict(request, store_id=config["store_id"]))
    if len(data) > MAX_MESSAGE:
        raise SyncError("Request exceeds its size bound")
    raw = bounded_process(ssh_args(config), data)
    try:
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ValueError()
        return result
    except (ValueError, UnicodeError):
        raise SyncError("Peer refused request or returned an invalid response") from None


def remote_inventory(config):
    result = remote_call(config, {"op": "inventory"})
    if result.get("store_id") != config["store_id"]:
        raise SyncError("Peer store identity changed or needs migration; no sync performed")
    inventory = result.get("credentials")
    if not isinstance(inventory, dict) or len(inventory) > MAX_NAMES:
        raise SyncError("Invalid peer inventory")
    for name, digest in inventory.items():
        name_check(name)
        digest_check(digest)
    return inventory


def direction(local, remote, base):
    if local == remote:
        return "same"
    if local is None or (base is not None and local == base):
        return "pull" if remote is not None else "push"
    if remote is None or (base is not None and remote == base):
        return "push"
    return "conflict"


def synchronize(store, config, selected=None, prefer=None):
    if prefer not in (None, "local", "remote") or (prefer and not selected):
        raise SyncError("Conflict resolution requires one explicit name and local or remote")
    if selected is not None:
        name_check(selected)
    # Caller holds the store lock throughout. Remote writes remain CAS protected.
    local, remote = store.inventory(), remote_inventory(config)
    binding = {k: config[k] for k in ("host", "user", "port", "host_fingerprint", "store_id", "identity_fingerprint")}
    binding["local_store_id"] = store.store_id
    peer = hashlib.sha256(json_bytes(binding)).hexdigest()
    baselines = read_json(store.root / "baselines-v2.json", {})
    base = baselines.get(peer, {})
    conflicts = []
    for name in ([selected] if selected else sorted(set(local) | set(remote))):
        l, r = local.get(name), remote.get(name)
        if l is None and r is None:
            raise SyncError("Named credential does not exist")
        way = direction(l, r, digest_check(base.get(name), nullable=True))
        if way == "conflict":
            if not prefer:
                print(name + ": conflict")
                conflicts.append(name)
                continue
            way = "push" if prefer == "local" else "pull"
        if way == "push":
            record = store.get(name)
            if record["digest"] != l:
                raise SyncError("Local credential changed before transfer")
            digest = remote_call(config, {"op": "put", "name": name, "data": record["data"], "expected": r}).get("digest")
            if digest != l:
                raise SyncError("Peer digest verification failed")
        elif way == "pull":
            record = remote_call(config, {"op": "get", "name": name}).get("record")
            raw = raw_record(record)
            if record["digest"] != r:
                raise SyncError("Peer credential changed before transfer")
            digest = store.put(name, raw, l)
        else:
            digest = l
        if remote_inventory(config).get(name) != digest or store.digest(name) != digest:
            raise SyncError("Credential changed during readback; old baseline retained")
        base[name] = digest
        baselines[peer] = base
        atomic_write(store.root / "baselines-v2.json", json_bytes(baselines))
        print(name + ": " + ("already matches" if way == "same" else "verified " + way))
    if conflicts:
        raise SyncError("Conflicting named files; resolve individually using sync NAME --prefer local or remote")


def bounded_input(stream, timeout=30):
    deadline = time.monotonic() + timeout
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise SyncError("Protocol request timed out")
            chunk = os.read(stream.fileno(), min(16384, MAX_MESSAGE + 1 - len(output)))
            if not chunk:
                return bytes(output)
            output.extend(chunk)
            if len(output) > MAX_MESSAGE:
                raise SyncError("Request exceeds its size bound")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    commands.add_parser("list")
    commands.add_parser("identity")
    add = commands.add_parser("add")
    add.add_argument("name")
    add.add_argument("--file", required=True)
    export = commands.add_parser("export")
    export.add_argument("name")
    export.add_argument("--file", required=True)
    export.add_argument("--replace", action="store_true")
    sync = commands.add_parser("sync")
    sync.add_argument("name", nargs="?")
    sync.add_argument("--prefer", choices=("local", "remote"))
    args = parser.parse_args()
    if args.command == "serve" and os.environ.get("SSH_ORIGINAL_COMMAND") != "credential-sync-v1":
        raise SyncError("Only credential-sync-v1 is allowed")
    store = Store(args.root)
    if args.command == "serve":
        # A forced command accepts a single bounded request; no remote path or shell.
        raw = bounded_input(sys.stdin.buffer)
        if len(raw) > MAX_MESSAGE:
            raise SyncError("Request exceeds its size bound")
        print(json.dumps(handle(store, json.loads(raw))))
        return
    with store.lock():
        if args.command == "identity":
            print(store.store_id)
        elif args.command == "list":
            for name in sorted(store.inventory()):
                print(name)
        elif args.command == "add":
            raw = read_file(args.file, MAX_SECRET)
            if raw is None:
                raise SyncError("Choose an owner-only regular credential file")
            store.put(args.name, raw, store.digest(args.name))
            print(name_check(args.name) + ": stored privately")
        elif args.command == "export":
            outside_repository(args.file)
            if os.path.lexists(canonical(args.file)) and not args.replace:
                raise SyncError("Destination exists; use --replace to overwrite it intentionally")
            atomic_write(args.file, raw_record(store.get(args.name)), replace=args.replace)
            print(name_check(args.name) + ": exported privately")
        elif args.command == "sync":
            config = read_json(store.root / "peer.json")
            if not config:
                raise SyncError("Configure and verify a peer explicitly before syncing")
            synchronize(store, config, args.name, args.prefer)


def entrypoint():
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, SyncError) else "Credential operation failed; no private data logged", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(entrypoint())
