"""Synthetic fixtures only. The SSH pipe is substituted, the receiver is a real process."""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import credential_sync as c
import credential_sync_setup as setup

spec = importlib.util.spec_from_file_location("new_mac_setup", SCRIPTS / "setup-new-mac.py")
new_mac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(new_mac)


class CredentialSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.a_config = self.config("machine A")
        self.b_config = self.config("machine B")
        self.a_result = setup.install(self.a_config)
        self.b_result = setup.install(self.b_config)
        self.a = c.Store(self.a_config["data_dir"])
        self.b = c.Store(self.b_config["data_dir"])
        host_key = c.read_file(self.b_result["public_key_file"]).decode().strip()
        self.a_config["peer"] = dict(host="example.invalid", user="test_user", port=2222,
                                      host_key=host_key, host_fingerprint=c.public_key(host_key)[1],
                                      store_id=self.b.store_id)
        setup.install(self.a_config)
        self.peer = c.read_json(self.a.root / "peer.json")
        self.calls = []
        self.real_process = c.bounded_process
        self.before_request = None

    def config(self, label):
        return dict(enabled=True, install_dir=str(self.root / label / "installed source"),
                    data_dir=str(self.root / label / "private data"), python=str(Path(sys.executable).resolve()), peer=None)

    def receiver(self, data, original="credential-sync-v1"):
        return subprocess.run([self.b_config["python"], str(Path(self.b_config["install_dir"]) / "credential_sync.py"),
                               "--root", str(self.b.root), "serve"], input=data, capture_output=True,
                              env=dict(os.environ, SSH_ORIGINAL_COMMAND=original, PYTHONDONTWRITEBYTECODE="1"), timeout=5)

    def transport(self, args, data=b"", **kwargs):
        if args[0] != "/usr/bin/ssh":
            return self.real_process(args, data, **kwargs)
        self.calls.append(args)
        request = json.loads(data)
        if self.before_request:
            self.before_request(request)
        result = self.receiver(data)
        if result.returncode:
            raise c.SyncError("Synthetic peer refused request")
        return result.stdout

    def sync(self, **kwargs):
        output = io.StringIO()
        with patch.object(c, "bounded_process", side_effect=self.transport), contextlib.redirect_stdout(output), self.a.lock():
            c.synchronize(self.a, self.peer, **kwargs)
        return output.getvalue()

    def put(self, store, name, raw):
        with store.lock():
            store.put(name, raw, store.digest(name))

    def test_two_way_binary_json_env_transfer_and_readback(self):
        fixtures = {"binary": bytes(range(256)), "json": b'{"fixture":"synthetic"}',
                    "env": b'ONLY_SYNTHETIC=fixture-value\nSECOND=line\n'}
        for name, raw in fixtures.items():
            self.put(self.a, name, raw)
        output = self.sync()
        for name in fixtures:
            self.assertEqual(self.a.get(name), self.b.get(name))
            self.assertNotIn(fixtures[name].decode(errors="replace"), output)
        self.put(self.b, "env", b"remote-change")
        self.put(self.b, "new", b"deliberately-added")
        self.sync()
        self.assertEqual(c.raw_record(self.a.get("env")), b"remote-change")
        self.assertEqual(self.a.get("new"), self.b.get("new"))
        self.assertGreater(len(self.calls), 5)

    def test_preexisting_and_three_way_conflicts_require_single_name_resolution(self):
        self.put(self.a, "one", b"first")
        self.put(self.b, "one", b"second")
        with self.assertRaises(c.SyncError):
            self.sync()
        self.sync(selected="one", prefer="remote")
        self.put(self.a, "one", b"local-change")
        self.put(self.b, "one", b"remote-change")
        with self.assertRaises(c.SyncError):
            self.sync()
        self.assertNotEqual(self.a.get("one"), self.b.get("one"))
        with self.assertRaises(c.SyncError):
            self.sync(prefer="local")
        self.sync(selected="one", prefer="local")
        self.assertEqual(c.raw_record(self.b.get("one")), b"local-change")

    def test_concurrent_remote_put_is_compare_and_swap_protected(self):
        self.put(self.a, "one", b"local")
        def race(req):
            if req["op"] == "put":
                self.put(self.b, "one", b"concurrent")
        self.before_request = race
        with self.assertRaises(c.SyncError):
            self.sync()
        self.assertEqual(c.raw_record(self.b.get("one")), b"concurrent")
        self.assertIsNone(c.read_json(self.a.root / "baselines-v2.json"))

    def test_readback_race_keeps_old_baseline(self):
        self.put(self.a, "one", b"original")
        self.sync()
        before = c.read_file(self.a.root / "baselines-v2.json")
        self.put(self.a, "one", b"next")
        state = {"put": False}
        def race(req):
            if req["op"] == "put":
                state["put"] = True
            elif req["op"] == "inventory" and state["put"]:
                self.put(self.b, "one", b"during-readback")
        self.before_request = race
        with self.assertRaises(c.SyncError):
            self.sync()
        self.assertEqual(c.read_file(self.a.root / "baselines-v2.json"), before)

    def test_pull_snapshot_change_and_local_cas_refuse_overwrite(self):
        self.put(self.b, "one", b"remote")
        def race(req):
            if req["op"] == "get":
                self.put(self.b, "one", b"later")
        self.before_request = race
        with self.assertRaises(c.SyncError):
            self.sync()
        self.assertIsNone(self.a.digest("one"))
        self.put(self.a, "one", b"local")
        with self.a.lock(), self.assertRaises(c.SyncError):
            self.a.put("one", b"stale-overwrite", None)

    def test_peer_host_store_and_identity_changes_do_not_reuse_baselines(self):
        self.put(self.a, "one", b"original")
        self.sync()
        self.put(self.b, "one", b"changed")
        for key, replacement in (("host", "other.invalid"), ("user", "other_user")):
            previous = self.peer[key]
            self.peer[key] = replacement
            if key == "host":
                c.atomic_write(self.peer["known_hosts"], c.known_host(self.peer))
            with self.assertRaises(c.SyncError):
                self.sync()
            self.peer[key] = previous
            c.atomic_write(self.peer["known_hosts"], c.known_host(self.peer))
        self.peer["store_id"] = "00000000-0000-4000-8000-000000000000"
        with self.assertRaises(c.SyncError):
            self.sync()
        self.assertEqual(c.raw_record(self.a.get("one")), b"original")
        self.assertFalse(any(req[-1] != "credential-sync-v1" for req in self.calls))

    def test_deletions_not_propagated(self):
        self.put(self.a, "one", b"keep")
        self.sync()
        self.a.path("one").unlink()  # Deliberate synthetic deletion, no delete protocol exists.
        self.sync()
        self.assertEqual(c.raw_record(self.a.get("one")), b"keep")
        self.b.path("one").unlink()
        self.sync()
        self.assertEqual(c.raw_record(self.b.get("one")), b"keep")

    def test_symlinks_hardlinks_paths_ownership_and_permissions_refused(self):
        self.put(self.a, "one", b"original")
        for name in ("../x", "a/b", "UPPER", "a.json", "", "a\n"):
            with self.assertRaises(c.SyncError):
                self.a.put(name, b"value", None)
        self.a.path("link").symlink_to(self.a.path("one"))
        with self.assertRaises(c.SyncError):
            self.a.digest("link")
        self.a.path("link").unlink()
        os.link(self.a.path("one"), self.a.path("hardlink"))
        with self.assertRaises(c.SyncError):
            self.a.get("one")
        self.a.path("hardlink").unlink()
        self.a.path("one").chmod(0o644)
        with self.assertRaises(c.SyncError):
            self.a.get("one")
        self.a.path("one").chmod(0o600)
        with patch.object(c.os, "getuid", return_value=os.getuid() + 1), self.assertRaises(c.SyncError):
            self.a.get("one")
        alias = self.root / "alias"
        alias.symlink_to(self.a.root, target_is_directory=True)
        with self.assertRaises(c.SyncError):
            c.Store(alias / "nested")
        self.assertFalse((self.a.root / "nested").exists())

    def test_integrity_and_sizes_rejected_without_values(self):
        for raw in (b"", b"x" * (c.MAX_SECRET + 1)):
            with self.assertRaises(c.SyncError):
                self.a.put("one", raw, None)
        self.put(self.a, "one", b"unique-synthetic-private-sentinel")
        rec = self.a.get("one")
        rec["data"] = base64.b64encode(b"altered").decode()
        c.atomic_write(self.a.path("one"), c.json_bytes(rec))
        with self.assertRaises(c.SyncError) as error:
            self.a.get("one")
        self.assertNotIn("sentinel", str(error.exception))
        result = self.receiver(b"X" * (c.MAX_MESSAGE + 1))
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(len(result.stderr), 160)

    def test_protocol_rejects_shell_paths_unknown_operations_and_payloads(self):
        sentinel = b"synthetic-private-not-for-output"
        for command in ("sh", "credential-sync-v1 extra", "", "scp -t /tmp/x"):
            result = self.receiver(sentinel, original=command)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(sentinel, result.stderr + result.stdout)
        for req in ({"op": "delete", "name": "one"}, {"op": "get", "name": "../escape"},
                    {"op": "inventory", "path": "/etc/passwd"}, {"op": "put", "data": "x"}, ["inventory"]):
            result = self.receiver(json.dumps(req).encode())
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(result.stdout)
        self.assertFalse(self.receiver(b"{invalid-secret-sentinel").stdout)

    def test_actual_ssh_config_contract_handles_spaces_and_disables_inherited_access(self):
        args = c.ssh_args(self.peer)
        result = subprocess.run(args[:1] + ["-G"] + args[1:], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        output = result.stdout.decode()
        for fragment in ("batchmode yes", "stricthostkeychecking true", "identityagent none", "identitiesonly yes",
                         "clearallforwardings yes", "passwordauthentication no", "port 2222", "user test_user"):
            self.assertIn(fragment, output)
        self.assertIn(self.peer["known_hosts"], output)
        self.assertIn(self.peer["identity"], output)
        self.assertFalse(any(line.startswith("proxycommand ") for line in output.splitlines()))

    def test_pin_or_identity_mismatch_fails_before_ssh(self):
        self.peer["host_fingerprint"] = "SHA256:wrong"
        with patch.object(c, "bounded_process") as run, self.assertRaises(c.SyncError):
            c.ssh_args(self.peer)
        run.assert_not_called()
        self.peer["host_fingerprint"] = c.public_key(self.peer["host_key"])[1]
        c.atomic_write(self.peer["known_hosts"], b"different host key\n")
        with self.assertRaises(c.SyncError):
            c.ssh_args(self.peer)
        c.atomic_write(self.peer["known_hosts"], c.known_host(self.peer))
        self.peer["identity_fingerprint"] = "SHA256:wrong"
        with self.assertRaises(c.SyncError):
            c.ssh_args(self.peer)

    def test_install_repeat_distinct_keys_and_private_modes(self):
        before = hashlib.sha256(c.read_file(Path(self.peer["identity"]))).digest()
        self.assertNotEqual(self.a_result["identity_fingerprint"], self.b_result["identity_fingerprint"])
        self.assertEqual(setup.install(self.a_config)["store_id"], self.a.store_id)
        self.assertEqual(hashlib.sha256(c.read_file(Path(self.peer["identity"]))).digest(), before)
        for store in (self.a, self.b):
            self.assertEqual(store.root.stat().st_mode & 0o777, 0o700)
            for p in store.root.rglob("*"):
                self.assertEqual(p.stat().st_mode & 0o777, 0o700 if p.is_dir() else 0o600)

    def test_interrupted_install_recovers_same_key_and_preserves_unrelated_paths(self):
        config = self.config("interrupted")
        write = c.atomic_write
        def interrupt(path, *args, **kwargs):
            if str(path).endswith(".pub"):
                raise OSError("simulated interruption")
            return write(path, *args, **kwargs)
        with patch.object(setup, "atomic_write", side_effect=interrupt), self.assertRaises(OSError):
            setup.install(config)
        key = Path(config["data_dir"]) / "identity/id_ed25519"
        prior = hashlib.sha256(key.read_bytes()).digest()
        setup.install(config)
        self.assertEqual(hashlib.sha256(key.read_bytes()).digest(), prior)
        other = self.config("existing")
        root = c.private_dir(other["install_dir"])
        c.atomic_write(root / "unrelated", b"keep")
        with self.assertRaises(c.SyncError):
            setup.install(other)
        self.assertEqual((root / "unrelated").read_bytes(), b"keep")
        self.assertFalse(Path(other["data_dir"]).exists())

    def test_invalid_host_pin_does_not_create_install(self):
        config = self.config("badpin")
        config["peer"] = dict(self.a_config["peer"], host_fingerprint="SHA256:wrong")
        with self.assertRaises(c.SyncError):
            setup.install(config)
        self.assertFalse(Path(config["install_dir"]).exists())

    def test_explicit_enrollment_preserves_ssh_entries_and_exact_restrictions(self):
        ssh = c.private_dir(self.root / "ssh directory")
        auth = ssh / "authorized_keys"
        original = (Path(self.b_result["public_key_file"]).read_text().strip() + " existing-normal-SSH\n").encode()
        c.atomic_write(auth, original)
        public = Path(self.a_result["public_key_file"]).read_text().strip()
        preview = setup.enroll(self.b_config["install_dir"], public, ssh)
        self.assertEqual(auth.read_bytes(), original)
        self.assertIn('restrict,command="', preview["authorized_keys_line"])
        self.assertTrue(setup.enroll(self.b_config["install_dir"], public, ssh, True)["changed"])
        final = auth.read_bytes()
        self.assertTrue(final.startswith(original))
        self.assertFalse(setup.enroll(self.b_config["install_dir"], public, ssh, True)["changed"])
        self.assertEqual(auth.read_bytes(), final)
        # Execute the exact generated forced command with spaces, then reject a shell request.
        option = shlex.split(preview["authorized_keys_line"])[0]
        command = option.split("command=", 1)[1]
        for request, success in (("credential-sync-v1", True), ("sh", False)):
            r = subprocess.run(shlex.split(command), input=b'{"op":"inventory"}', capture_output=True,
                               env=dict(os.environ, SSH_ORIGINAL_COMMAND=request), timeout=5)
            self.assertEqual(r.returncode == 0, success)

    def test_ambiguous_or_unrestricted_existing_key_is_not_accepted(self):
        ssh = c.private_dir(self.root / "ssh")
        auth = ssh / "authorized_keys"
        public = Path(self.a_result["public_key_file"]).read_text().strip()
        line = setup.enroll(self.b_config["install_dir"], public, ssh)["authorized_keys_line"]
        for bad in (public + " general-access\n", line + "\n" + line + "\n", "# " + public + "\n"):
            c.atomic_write(auth, bad.encode())
            with self.assertRaises(c.SyncError):
                setup.enroll(self.b_config["install_dir"], public, ssh, True)
            self.assertEqual(auth.read_text(), bad)

    def test_legacy_store_and_peer_require_explicit_migration(self):
        legacy = c.private_dir(self.root / "legacy")
        records = c.private_dir(legacy / "credentials")
        c.atomic_write(records / "old.json", b"synthetic old data")
        with self.assertRaises(c.SyncError):
            c.Store(legacy)
        with self.assertRaises(c.SyncError):
            c.ssh_args({"peer": "legacy"})
        self.assertEqual((records / "old.json").read_bytes(), b"synthetic old data")

    def test_client_add_export_only_explicit_file_and_no_secret_output(self):
        cli = [sys.executable, str(SCRIPTS / "credential_sync.py"), "--root", str(self.a.root)]
        raw = b"synthetic-secret-never-print-901"
        source = self.root / "source.env"
        c.atomic_write(source, raw)
        def invoke(*args):
            r = subprocess.run(cli + list(args), capture_output=True, timeout=5)
            self.assertNotIn(raw, r.stdout + r.stderr)
            return r
        self.assertEqual(invoke("add", "specific", "--file", str(source)).returncode, 0)
        target = self.root / "export.env"
        self.assertEqual(invoke("export", "specific", "--file", str(target)).returncode, 0)
        self.assertEqual(target.read_bytes(), raw)
        self.assertNotEqual(invoke("export", "specific", "--file", str(target)).returncode, 0)
        self.assertEqual(invoke("export", "specific", "--file", str(target), "--replace").returncode, 0)
        self.assertEqual(set(self.a.inventory()), {"specific"})
        repo = c.private_dir(self.root / "export repo")
        (repo / ".git").mkdir()
        self.assertNotEqual(invoke("export", "specific", "--file", str(repo / "secret.env")).returncode, 0)
        self.assertFalse((repo / "secret.env").exists())

    def test_exact_size_limit_roundtrip_through_receiver(self):
        raw = b"s" * c.MAX_SECRET
        self.put(self.a, "limit", raw)
        self.sync(selected="limit")
        self.assertEqual(self.b.get("limit")["digest"], hashlib.sha256(raw).hexdigest())

    def test_bounded_transport_reaps_timeouts_and_limits_both_streams(self):
        with self.assertRaises(c.SyncError):
            c.bounded_process([sys.executable, "-c", "import time;time.sleep(10)"], timeout=0.1)
        for stream in ("stdout", "stderr"):
            with self.assertRaises(c.SyncError) as err:
                c.bounded_process([sys.executable, "-c", "import sys;sys." + stream + ".write('synthetic-secret'*1000)"], limit=100)
            self.assertNotIn("synthetic-secret", str(err.exception))

    def test_repository_store_request_timeout_and_store_swap_refused(self):
        repo = c.private_dir(self.root / "source repo")
        (repo / ".git").mkdir()
        with self.assertRaises(c.SyncError):
            c.Store(repo / "data")
        self.assertFalse((repo / "data").exists())
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(read_fd, "rb") as reader, self.assertRaises(c.SyncError):
                c.bounded_input(reader, timeout=0.05)
        finally:
            os.close(write_fd)
        result = self.receiver(c.json_bytes({"op": "put", "name": "one", "expected": None,
                                            "data": base64.b64encode(b"synthetic").decode(), "store_id": self.a.store_id}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(self.b.digest("one"))

    def test_setup_phase_and_read_only_plan_with_old_and_new_config(self):
        home = c.private_dir(self.root / "fake home")
        config = json.loads((SCRIPTS.parent / "setup/new-mac.example.json").read_text())
        config["credential_sync"] = self.config("phase")
        config_file = home / "config.json"
        c.atomic_write(config_file, c.json_bytes(config))
        before = set(home.rglob("*"))
        result = subprocess.run([sys.executable, str(SCRIPTS / "setup-new-mac.py"), "--config", str(config_file),
                                 "--phase", "credential_sync"], capture_output=True, env=dict(os.environ, HOME=str(home)), timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(json.loads(result.stdout)["phases"], ["credential_sync"])
        self.assertEqual(set(home.rglob("*")), before)
        self.assertFalse(Path(config["credential_sync"]["install_dir"]).exists())
        # Exercise the actual public setup entry point, selecting only the independent sync phase.
        result = subprocess.run([sys.executable, str(SCRIPTS / "setup-new-mac.py"), "--config", str(config_file),
                                 "--phase", "credential_sync", "--apply"], capture_output=True,
                                env=dict(os.environ, HOME=str(home)), timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        report = json.loads(next((home / ".local/state/nova-setup").glob("*/report.json")).read_text())
        self.assertEqual(set(report["phases"]), {"credential_sync"})
        self.assertFalse(report["credential_sync"]["credential_transfer_performed"])
        self.assertFalse(report["feature_tests_run"])
        del config["credential_sync"]
        c.atomic_write(config_file, c.json_bytes(config))
        result = subprocess.run([sys.executable, str(SCRIPTS / "setup-new-mac.py"), "--config", str(config_file)],
                                capture_output=True, env=dict(os.environ, HOME=str(home)), timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["credential_sync"]["enabled"])


if __name__ == "__main__":
    unittest.main()
