import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "skills/mac-vm-setup/scripts/identity_baseline.py"
spec = importlib.util.spec_from_file_location("identity_baseline", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class BaselineTests(unittest.TestCase):
    def setUp(self):
        # Use an owner-controlled parent; production refuses world-writable ancestors.
        self.temp = tempfile.TemporaryDirectory(prefix="nova-baseline-test-", dir=Path.home())
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "guest"
        self.home.mkdir(mode=0o700)
        self.source = self.root / "profiles"
        self.source.mkdir(mode=0o700)
        self.pub = self.root / "key.pub"
        blob = b"\0\0\0\x0bssh-ed25519\0\0\0\x20" + bytes(range(32))
        self.pub.write_bytes(b"ssh-ed25519 " + base64.b64encode(blob) + b" test\n")
        self.cert = "A" * 40
        self.profiles = []
        for i, role in enumerate(("mac_development", "mac_distribution", "ios_development", "ios_distribution")):
            name = f"00000000-0000-0000-0000-{i:012d}.mobileprovision"
            data = ("synthetic-profile-" + role).encode()
            (self.source / name).write_bytes(data)
            self.profiles.append({"file": name, "sha256": m.digest(data), "team": "1234567890",
                                  "role": role, "certificate_fingerprints": [self.cert]})
        self.baseline = {"schema": 1, "github": {"policy": "shared_host_key", "account": "owner",
                         "repository": "git@github.com:owner/repo.git", "public_fingerprint": m.public_fingerprint(self.pub)},
                         "apple_team": "1234567890", "signing_identities": [{"fingerprint": self.cert, "kind": "Apple Development"}],
                         "profiles": self.profiles}
        self.baseline["revision"] = m.revision(self.baseline)
        self.profile_patch = patch.object(m, "profile", side_effect=self.fake_profile)
        self.profile_patch.start()

    def tearDown(self):
        self.profile_patch.stop()
        self.temp.cleanup()

    def fake_profile(self, path, team):
        path = Path(path)
        if not path.exists():
            raise ValueError("Missing synthetic profile")
        item = next(x for x in self.profiles if x["file"] == path.name)
        return {**item, "sha256": m.digest(path.read_bytes())}

    def install(self):
        return m.apply_profiles(self.baseline, self.source, self.home)

    def route(self):
        return ("user git\nhostname github.com\nport 22\nidentitiesonly yes\n"
                "stricthostkeychecking true\nidentityfile " + str(self.pub).removesuffix(".pub") + "\n").encode()

    def test_revision_detects_tampering(self):
        m.validate(self.baseline)
        self.baseline["github"]["account"] = "different-owner"
        with self.assertRaises(ValueError):
            m.validate(self.baseline)

    def test_rehashed_path_traversal_still_rejected(self):
        self.baseline["profiles"][0]["file"] = "../../credential"
        self.baseline["revision"] = m.revision(self.baseline)
        with self.assertRaises(ValueError):
            m.validate(self.baseline)

    def test_apply_is_idempotent_and_private(self):
        first = self.install()
        second = self.install()
        self.assertEqual(len(first["installed_paths"]), 8)
        self.assertEqual(second["installed_paths"], [])
        for name in first["verified_paths"]:
            self.assertEqual(Path(name).stat().st_mode & 0o777, 0o600)

    def test_conflict_preflight_preserves_all_destinations(self):
        directory = m.profile_destinations(self.home)[1]
        directory.mkdir(parents=True)
        target = directory / self.profiles[-1]["file"]
        target.write_bytes(b"existing authored bytes")
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(target.read_bytes(), b"existing authored bytes")
        self.assertFalse(m.profile_destinations(self.home)[0].exists())

    def test_symlink_destination_refused(self):
        directory = m.profile_destinations(self.home)[0]
        directory.mkdir(parents=True)
        (directory / self.profiles[0]["file"]).symlink_to(self.pub)
        with self.assertRaises(ValueError):
            self.install()

    def test_inventory_does_not_probe_network_or_claim_release_readiness(self):
        self.install()
        with patch.object(m, "run", return_value=(0, self.route(), b"")) as run, patch.object(m, "available_identities", return_value={self.cert: "Apple Development"}):
            result = m.check(self.baseline, self.pub, self.home)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(result["identity_parity"])
        self.assertEqual(result["checks"]["github_authentication"]["status"], "not_exercised")
        self.assertEqual(result["release_readiness"], "not_established_by_asset_inventory")

    def test_missing_identity_and_profile_cannot_be_ready(self):
        self.install()
        (m.profile_destinations(self.home)[0] / self.profiles[0]["file"]).unlink()
        with patch.object(m, "run", return_value=(0, self.route(), b"")), patch.object(m, "available_identities", return_value={}):
            result = m.check(self.baseline, self.pub, self.home)
        self.assertFalse(result["identity_parity"])
        self.assertEqual(result["checks"]["profile_identity_coverage:mac_development"]["status"], "failed")

    def test_one_supported_cache_is_sufficient_but_absence_everywhere_fails(self):
        self.install()
        for item in self.profiles:
            (m.profile_destinations(self.home)[1] / item["file"]).unlink()
        with patch.object(m, "run", return_value=(0, self.route(), b"")), patch.object(m, "available_identities", return_value={self.cert: "Apple Development"}):
            result = m.check(self.baseline, self.pub, self.home)
            self.assertTrue(result["identity_parity"])
            (m.profile_destinations(self.home)[0] / self.profiles[0]["file"]).unlink()
            result = m.check(self.baseline, self.pub, self.home)
        self.assertFalse(result["identity_parity"])
        self.assertEqual(result["checks"]["profile_identity_coverage:mac_development"]["status"], "failed")

    def test_conflicting_second_cache_cannot_be_ready(self):
        self.install()
        (m.profile_destinations(self.home)[1] / self.profiles[0]["file"]).write_bytes(b"different bytes")
        with patch.object(m, "run", return_value=(0, self.route(), b"")), patch.object(m, "available_identities", return_value={self.cert: "Apple Development"}):
            result = m.check(self.baseline, self.pub, self.home)
        self.assertFalse(result["identity_parity"])

    def test_github_exit_one_greeting_and_repo_read(self):
        self.install()
        responses = [(0, self.route(), b""), (1, b"", b"Hi owner! You've successfully authenticated, but GitHub does not provide shell access."),
                     (0, b"git@github.com:owner/repo.git\n", b""), (0, ("b" * 40 + "\trefs/heads/main\n").encode(), b"")]
        with patch.object(m, "run", side_effect=responses) as run, patch.object(m, "available_identities", return_value={self.cert: "Apple Development"}):
            result = m.check(self.baseline, self.pub, self.home, network=True)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(result["checks"]["github_repository_read"]["status"], "passed")
        self.assertEqual(result["checks"]["normal_push"]["status"], "not_exercised")

    def test_wrong_account_stops_before_repository_read(self):
        self.install()
        responses = [(0, self.route(), b""), (1, b"", b"Hi other! You've successfully authenticated")]
        with patch.object(m, "run", side_effect=responses) as run, patch.object(m, "available_identities", return_value={}):
            result = m.check(self.baseline, self.pub, self.home, network=True)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["checks"]["github_authentication"]["status"], "failed")

    def test_changed_url_stops_before_network_repository_read(self):
        self.install()
        responses = [(0, self.route(), b""), (1, b"", b"Hi owner! You've successfully authenticated"), (0, b"https://other.example/repo\n", b"")]
        with patch.object(m, "run", side_effect=responses) as run, patch.object(m, "available_identities", return_value={}):
            result = m.check(self.baseline, self.pub, self.home, network=True)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(result["checks"]["github_repository_read"]["status"], "failed")

    def test_timeout_reaps_owned_command(self):
        code, _, _ = m.run([sys.executable, "-c", "import time;time.sleep(30)"], timeout=0.05)
        self.assertEqual(code, 124)

    def test_receipt_never_overwrites(self):
        p = self.root / "receipt.json"
        m.private_write(p, b"original")
        with self.assertRaises(FileExistsError):
            m.private_write(p, b"replacement")
        self.assertEqual(p.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
