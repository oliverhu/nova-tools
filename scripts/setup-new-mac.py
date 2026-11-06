#!/usr/bin/env python3
"""Prepare a Nova development Mac. Default is a read-only plan; use --apply to run."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import ssl
import subprocess
import sys
import tarfile
import urllib.request
import uuid

sys.dont_write_bytecode = True
from mac_provisioning import MAX_BUNDLE, TEAM, private_directory, sha, unpack_bundle, write_new
import credential_sync_setup

ROOT = Path(__file__).resolve().parents[1]
PHASES = ("tools", "repositories", "storage", "profiles", "signing", "coordinator", "permissions", "runners", "credential_sync")
DEPENDENCIES = {"repositories": ("tools",), "storage": ("repositories",),
                "profiles": ("repositories",), "signing": ("profiles",),
                "coordinator": ("storage",), "permissions": ("tools",),
                "runners": ("storage", "signing")}
FORMULAS = ("git", "git-lfs", "gh", "python@3.12", "node", "cmake", "ninja", "jq", "ripgrep", "ffmpeg", "openclaw/tap/peekaboo")
RUNNERS = ("browser-features", "community-ui", "amazon-playback", "mini-player", "password-focus")
XCODE = Path("/Applications/Xcode.app/Contents/Developer")
REPOSITORY = "git@github.com:oliverhu/nova-browser.git"
PERMISSIONS = {"accessibility": ("Accessibility", "Privacy_Accessibility"),
               "screen-recording": ("Screen Recording", "Privacy_ScreenCapture"),
               "event-synthesizing": ("Event Synthesizing", "Privacy_Accessibility")}


def permission_plan(config):
    """Workload selection only; never inspect or request live consent here."""
    value = config.get("permissions", {"peekaboo": ["accessibility", "screen-recording"],
                                       "runner_families": ["browser-features"]})
    if not isinstance(value, dict) or set(value) != {"peekaboo", "runner_families"}:
        raise ValueError("permissions needs peekaboo and runner_families lists")
    for name, choices in (("peekaboo", PERMISSIONS), ("runner_families", RUNNERS)):
        items = value[name]
        if (not isinstance(items, list) or any(not isinstance(x, str) or x not in choices for x in items)
                or len(items) != len(set(items))):
            raise ValueError("Unknown or repeated permissions selection: " + name)
    return {name: list(items) for name, items in value.items()}


def require_interactive_desktop():
    if (os.stat("/dev/console").st_uid != os.getuid() or os.environ.get("SSH_CONNECTION")
            or os.environ.get("SSH_TTY") or not sys.stdin.isatty()):
        raise SetupRequired("Complete the planned consent session in this Mac's logged-in desktop Terminal; no prompts from SSH/background tests")


class SetupRequired(RuntimeError):
    pass


def path(value):
    return Path(value).expanduser().absolute()


def acknowledge(message):
    print(message, flush=True)
    if sys.stdin.isatty():
        input("Complete this step, then press Return (or Ctrl+C to resume later): ")


class Setup:
    def __init__(self, config, phases):
        self.config = config
        self.phases = phases
        self.browser = path(config["browser_path"])
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.record = private_directory(Path.home() / ".local/state/nova-setup" / stamp)
        self.report = {"schema": 1, "host": platform.node(), "phases": {}, "manual_steps": [],
                       "feature_tests_run": False, "release_or_upload_performed": False}
        self.env = dict(os.environ, DEVELOPER_DIR=str(XCODE), PYTHONDONTWRITEBYTECODE="1",
                        HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_INSTALL_CLEANUP="1")
        self.env["PATH"] = "/opt/homebrew/bin:/opt/homebrew/sbin:" + os.environ.get("PATH", "/usr/bin:/bin")

    def save(self):
        # The run directory is private and unique; replace only this run's report.
        temporary = self.record / ("report-" + uuid.uuid4().hex + ".json")
        write_new(temporary, (json.dumps(self.report, indent=2) + "\n").encode())
        os.replace(temporary, self.record / "report.json")

    def command(self, args, *, cwd=None, capture=False, interactive=False, allow_failure=False, input_bytes=None):
        args = [str(item) for item in args]
        log = self.record / ("command-" + uuid.uuid4().hex[:8] + ".log")
        if interactive:
            result = subprocess.run(args, cwd=cwd, env=self.env)
        else:
            with log.open("xb") as output:
                result = subprocess.run(args, cwd=cwd, env=self.env,
                                        input=input_bytes, stdout=subprocess.PIPE if capture else output, stderr=output)
        if result.returncode and not allow_failure:
            message = ("Command failed: " + args[0] + ". See " + str(log) if not interactive
                       else "Complete the interactive step and rerun this phase: " + args[0])
            raise SetupRequired(message)
        return result

    def tools(self):
        if not (XCODE / "usr/bin/xcodebuild").is_file():
            self.command(["/usr/bin/open", "macappstore://itunes.apple.com/app/id497799835"], allow_failure=True)
            raise SetupRequired("Install full Xcode at /Applications/Xcode.app, open it, accept its license and install the iOS platform; rerun.")
        first = self.command(["/usr/bin/xcodebuild", "-checkFirstLaunchStatus"], allow_failure=True)
        if first.returncode:
            self.command(["/usr/bin/open", "-a", "Xcode"], allow_failure=True)
            raise SetupRequired("Finish Xcode's first-launch setup in its app, then rerun.")
        brew = Path("/opt/homebrew/bin/brew")
        if not brew.is_file():
            url = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
            with urllib.request.urlopen(url, timeout=60) as response:
                if response.geturl() != url:
                    raise SetupRequired("Homebrew installer redirected; inspect the official installation instructions.")
                data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise SetupRequired("Homebrew installer exceeds its size bound")
            installer = self.record / "homebrew-install.sh"
            write_new(installer, data)
            self.report["homebrew_installer_sha256"] = sha(data)
            self.command(["/bin/bash", installer], interactive=True)
        for formula in FORMULAS:
            found = self.command([brew, "list", "--versions", formula], capture=True, allow_failure=True)
            if found.returncode or not found.stdout.strip():
                self.command([brew, "install", formula], interactive=True)
        detail = json.loads(self.command([brew, "info", "--json=v2", "peekaboo"], capture=True).stdout)
        if not any(item.get("full_name") == "openclaw/tap/peekaboo" and item.get("installed") for item in detail.get("formulae", [])):
            raise SetupRequired("Peekaboo must be the signed openclaw/tap/peekaboo formula. Resolve an existing core-formula conflict explicitly.")
        self.command(["git", "lfs", "install", "--skip-repo"])

    def repositories(self):
        if self.browser != self.browser.resolve():
            raise SetupRequired("Keep the source checkout on a canonical local path")
        if self.browser.exists():
            remote = self.command(["git", "-C", self.browser, "remote", "get-url", "origin"], capture=True).stdout.decode().strip()
            if remote not in (REPOSITORY, "https://github.com/oliverhu/nova-browser.git"):
                raise SetupRequired("Existing browser directory has a different origin; it was preserved")
            # Existing source/branches/staged work are never pulled, reset or replaced.
            self.command(["git", "-C", self.browser, "fetch", "--no-tags", "origin", "main"])
        else:
            self.browser.parent.mkdir(parents=True, exist_ok=True)
            self.command(["git", "clone", REPOSITORY, self.browser])
        self.report["browser_commit"] = self.command(["git", "-C", self.browser, "rev-parse", "HEAD"], capture=True).stdout.decode().strip()

    def storage(self):
        config = self.config["storage"]
        volume = path(config["volume"])
        artifact = path(config["artifact_root"])
        expected = config["volume_uuid"]
        if not re.fullmatch(r"[0-9A-Fa-f-]{36}", expected):
            raise SetupRequired("Set the new Mac's actual APFS volume UUID in the configuration")
        if volume != volume.resolve() or not volume.is_dir() or artifact != artifact.resolve():
            raise SetupRequired("Mount the selected APFS volume; volume and artifact paths must be canonical")
        disk = plistlib.loads(self.command(["/usr/sbin/diskutil", "info", "-plist", volume], capture=True).stdout)
        if (disk.get("VolumeUUID", "").upper() != expected.upper() or disk.get("MountPoint") != str(volume)
                or disk.get("FilesystemType", "").lower() != "apfs" or disk.get("ReadOnlyVolume") is not False
                or artifact == volume or not artifact.is_relative_to(volume)
                or artifact.is_relative_to(self.browser) or self.browser.is_relative_to(artifact)):
            raise SetupRequired("Selected build storage failed UUID/APFS/writable/path checks")
        directory = private_directory(self.browser / ".build/config", allow_readable=True)
        record = {"schema": 1, "hostname": platform.node(), "volume": str(volume),
                  "volume_uuid": expected, "artifact_root": str(artifact)}
        write_new(directory / "build-storage.json", (json.dumps(record, indent=2) + "\n").encode())
        self.command([sys.executable, self.browser / "scripts/build_storage.py", "--no-create", ".build/new-mac-setup/preflight"], cwd=self.browser)

    def profiles(self):
        config = self.config["provisioning"]
        host, source, digest = config["ssh_host"], config["bundle_path"], config["sha256"]
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", host)
                or not source.startswith("/") or any(c in source for c in "\x00\r\n")
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise SetupRequired("Configure the trusted SSH host, absolute bundle path and SHA-256 from export-mac-provisioning.py")
        # Bounded, encrypted transfer through an already enrolled SSH identity/host pin.
        command = "dd if=" + shlex.quote(source) + " bs=1048576 count=65 2>/dev/null"
        with (self.record / "profile-fetch.log").open("xb") as error_log:
            result = subprocess.run(["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                                     "-o", "ConnectTimeout=10", host, command], stdout=subprocess.PIPE,
                                    stderr=error_log, timeout=120)
        if result.returncode:
            raise SetupRequired("Profile transfer failed. Enroll SSH access and verify the source host key; inspect profile-fetch.log.")
        data = result.stdout
        if len(data) > MAX_BUNDLE or sha(data) != digest:
            raise SetupRequired("Provisioning bundle size/checksum mismatch; nothing installed")
        write_new(self.record / "provisioning.tar.gz", data)
        profiles = unpack_bundle(data, self.record / "validated-profiles")
        platforms = {item for _, info, _ in profiles for item in info["platforms"]}
        if not {"OSX", "iOS"}.issubset(platforms):
            raise SetupRequired("Bundle must include current Nova macOS and iOS profiles; refresh/export on the source Mac")
        # Validate the whole bundle before installing any profile.
        destinations = [private_directory(Path.home() / folder, allow_readable=True) for folder in
                        ("Library/Developer/Xcode/UserData/Provisioning Profiles", "Library/MobileDevice/Provisioning Profiles")]
        for source_path, _, _ in profiles:
            for destination in destinations:
                write_new(destination / source_path.name, source_path.read_bytes(), restrict_existing=True)
        self.report["profile_count"] = len(profiles)
        self.report["provisioning_sha256"] = digest
        self.validated_profiles = profiles

    def signing(self):
        output = self.command(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"], capture=True).stdout.decode()
        identities = re.findall(r'\b([0-9A-F]{40})\s+"([^"]+)"', output)
        expected = ("Apple Development:", "Apple Distribution:", "Developer ID Application:")
        valid = {kind: set() for kind in expected}
        # Apple Development's display-name suffix can be a person identifier.
        # Bind actual certificate OU/team and fingerprint to an available private-key identity.
        for kind in expected:
            certificates = self.command(["/usr/bin/security", "find-certificate", "-a", "-c", kind, "-p"],
                                        capture=True, allow_failure=True).stdout or b""
            for certificate in re.findall(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", certificates, re.DOTALL):
                fingerprint = hashlib.sha1(ssl.PEM_cert_to_DER_cert(certificate.decode())).hexdigest().upper()
                if not any(value == fingerprint and name.startswith(kind) for value, name in identities):
                    continue
                subject = self.command(["/usr/bin/openssl", "x509", "-noout", "-subject", "-nameopt", "RFC2253"],
                                       input_bytes=certificate, capture=True).stdout.decode()
                if re.search(r"(?:^|[, ])OU=" + re.escape(TEAM) + r"(?:,|\s*$)", subject):
                    valid[kind].add(fingerprint)
        missing = [kind for kind in expected if not valid[kind]]
        if missing:
            self.command(["/usr/bin/open", "-a", "Xcode"], allow_failure=True)
            raise SetupRequired("Import the existing encrypted .developerprofile through Xcode Settings > Accounts, or the existing .p12 in Keychain Access. Missing: " + ", ".join(missing) + ". Profile downloads do not include private keys.")
        developer_ids = valid["Developer ID Application:"]
        candidates = []
        for source, info, profile in getattr(self, "validated_profiles", []):
            ent = profile.get("Entitlements", {})
            certs = {hashlib.sha1(cert).hexdigest().upper() for cert in profile.get("DeveloperCertificates", [])}
            environment = ent.get("com.apple.developer.icloud-container-environment", [])
            services = ent.get("com.apple.developer.icloud-services", [])
            if (info["application"] == "app.novabrowser.Nova" and profile.get("ProvisionsAllDevices") is True
                    and "OSX" in info["platforms"] and certs & developer_ids
                    and "iCloud.app.novabrowser.Nova" in ent.get("com.apple.developer.icloud-container-identifiers", [])
                    and "Production" in ([environment] if isinstance(environment, str) else environment)
                    and "CloudKit" in ([services] if isinstance(services, str) else services)):
                candidates.append((info["expires"], source))
        if not candidates:
            raise SetupRequired("Need a current Nova Developer ID/Production CloudKit profile matching the imported identity; refresh the source bundle")
        chosen = max(candidates, key=lambda item: item[0])[1]
        target = private_directory(self.browser / ".build/provisioning", allow_readable=True) / "NovaMac-DeveloperID.provisionprofile"
        write_new(target, chosen.read_bytes(), restrict_existing=True)
        self.report["signing_identity_types_present"] = list(expected)
        self.report["manual_steps"].append("Sign into the Nova team in Xcode. If this Mac will notarize releases, configure its own NovaNotary Keychain profile with: xcrun notarytool store-credentials NovaNotary")

    def coordinator(self):
        config = self.config["coordinator"]
        worker = config["worker"]
        if worker not in ("oliver-mac", "oliver-mini", "oliverhu-mac-studio", "oliverhu-mac-studio-vm1", "oliverhu-mac-studio-vm2", "oliverhu-mini-vm1", "oliverhu-mini-vm2"):
            raise SetupRequired("Select a recognized worker identity and enroll its unique service credential first.")
        if worker in ("oliver-mac", "oliver-mini") and not config.get("old_worker_stopped", False):
            raise SetupRequired("Stop/settle the previous host for this worker role, then set old_worker_stopped=true. Do not share a live identity.")
        host = config["cluster_host"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", host):
            raise SetupRequired("Invalid cluster SSH host")
        self.command([sys.executable, ROOT / "scripts/bootstrap.py", "--worker", worker, "--cluster-host", host,
                      "--repository-path", self.browser, "--build-path", path(self.config["storage"]["artifact_root"]),
                      "--no-start", *(["--trust-internal-ca"] if config.get("trust_internal_ca", False) else [])], interactive=True)
        self.report["coordinator"] = "Installed; capacity reporter/dashboard agents are deliberately not started by onboarding"

    def permissions(self):
        selected = permission_plan(self.config)["peekaboo"]
        self.report["permission_selection"] = permission_plan(self.config)
        self.report["manual_steps"].append("During onboarding, open Codex in the actual worker chat and qualify its required Browser/Computer Use target-app access before timed tests. Reuse valid caller/app-context evidence; Terminal/Peekaboo grants do not transfer.")
        if not selected:
            self.report["peekaboo_permissions"] = {"status": "not requested"}
            return
        def granted():
            result = self.command(["peekaboo", "permissions", "status", "--no-remote", "--json"], capture=True, allow_failure=True)
            value = json.loads(result.stdout)
            entries = value.get("data", {}).get("permissions")
            if result.returncode or not isinstance(entries, list):
                raise SetupRequired("Installed Peekaboo permission schema changed; inspect its help before proceeding")
            states = {item.get("name"): item.get("isGranted") for item in entries if isinstance(item, dict)}
            if any(type(states.get(PERMISSIONS[kind][0])) is not bool for kind in selected):
                raise SetupRequired("Selected permission status is unknown; inspect the requester before prompting")
            return states
        before = granted()
        missing = [kind for kind in selected if not before[PERMISSIONS[kind][0]]]
        self.report["peekaboo_permissions"] = before
        self.report["missing_selected_permissions"] = missing
        self.report["permission_requests"] = []
        if not missing:
            return
        require_interactive_desktop()
        print("Onboarding consent needed for the selected local Peekaboo actor: " +
              ", ".join(PERMISSIONS[kind][0] for kind in missing), flush=True)
        for kind in missing:
            self.report["permission_requests"].append(kind)
            self.command(["peekaboo", "permissions", "request", kind, "--no-remote"], interactive=True, allow_failure=True)
        acknowledge("Complete only those listed grants for the exact requesting app/binary using its supported System Settings instructions. Relaunch if required; a denial remains pending.")
        after = granted()
        self.report["peekaboo_permissions"] = after
        missing = [PERMISSIONS[kind][0] for kind in selected if not after[PERMISSIONS[kind][0]]]
        if missing:
            raise SetupRequired("Permissions still missing: " + ", ".join(missing) + ". Relaunch the requesting app if macOS requires it; no automatic retries.")

    def runners(self):
        selected = permission_plan(self.config)["runner_families"]
        if not selected:
            self.report["runner_permissions"] = []
            return
        require_interactive_desktop()
        script = self.browser / "scripts/ui_test_runner_setup.py"
        print("Onboarding permission-only setup for: " + ", ".join(selected) +
              ". Existing grants are reused; complete only genuine macOS prompts for these signed runners.", flush=True)
        for role in selected:
            self.command([sys.executable, script, "prepare", "--runner", role], cwd=self.browser)
            self.command([sys.executable, script, "preflight", "--runner", role, "--launch-services", "--timeout-seconds", "75"], cwd=self.browser)
        self.report["runner_permissions"] = selected

    def credential_sync(self):
        config = self.config.get("credential_sync")
        if not config or not config.get("enabled"):
            self.report["credential_sync"] = {"enabled": False, "changes_performed": False}
            return
        self.report["credential_sync"] = credential_sync_setup.install(config)
        self.report["manual_steps"].append("Review the generated public key and enroll it locally on the intended peer using enroll-credential-peer.py. No SSH access or credentials were transferred by setup.")

    def run(self):
        for phase in PHASES:
            if phase not in self.phases:
                continue
            blocked = [item for item in DEPENDENCIES.get(phase, ()) if self.report["phases"].get(item, {}).get("status") != "complete"]
            if blocked:
                self.report["phases"][phase] = {"status": "pending", "action": "Include/complete prerequisites: " + ", ".join(blocked)}
                self.save()
                continue
            print("\nPreparing " + phase + "…", flush=True)
            try:
                getattr(self, phase)()
                self.report["phases"][phase] = {"status": "complete"}
            except (SetupRequired, OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
                self.report["phases"][phase] = {"status": "pending", "action": str(error)}
                print("Needs attention: " + str(error), flush=True)
            self.save()
        self.report["status"] = "pending" if any(item["status"] != "complete" for item in self.report["phases"].values()) else "setup-prepared"
        self.report["all_app_permissions_verified"] = False
        self.report["feature_tasks_ready"] = False
        self.report["manual_steps"].append("Before feature task pickup, the setup owner must qualify the actual app/runner permission inventory, including Local Network for NovaFeatureCheck and required Codex target-app access, then report readiness to Coordinator. Keep affected work dependent on the existing onboarding task; this prepared report is not admission.")
        self.save()
        print("\nSetup report: " + str(self.record / "report.json"))
        for item in self.report["manual_steps"]:
            print("Manual: " + item)
        return 78 if self.report["status"] == "pending" else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--phase", choices=PHASES, action="append", help="Run selected phases and their prerequisites; default: all")
    args = parser.parse_args()
    config = json.loads(args.config.expanduser().read_text())
    required = {"schema", "browser_path", "storage", "provisioning", "coordinator"}
    if (not isinstance(config, dict) or config.get("schema") != 1
            or not required <= set(config) or set(config) - required - {"credential_sync", "permissions"}):
        parser.error("Use setup/new-mac.example.json; credentials never belong in this file")
    fields = {"storage": {"volume", "volume_uuid", "artifact_root"},
              "provisioning": {"ssh_host", "bundle_path", "sha256"},
              "coordinator": {"worker", "cluster_host", "old_worker_stopped", "trust_internal_ca"}}
    if not isinstance(config["browser_path"], str) or not config["browser_path"]:
        parser.error("browser_path must be a nonempty path")
    for section, keys in fields.items():
        if not isinstance(config[section], dict) or set(config[section]) != keys:
            parser.error("Unexpected or missing fields in " + section)
    for section in ("storage", "provisioning"):
        if not all(isinstance(value, str) and value for value in config[section].values()):
            parser.error(section + " values must be nonempty strings")
    coordination = config["coordinator"]
    if (coordination["worker"] not in (None, "oliver-mac", "oliver-mini", "oliverhu-mac-studio", "oliverhu-mac-studio-vm1", "oliverhu-mac-studio-vm2", "oliverhu-mini-vm1", "oliverhu-mini-vm2")
            or not isinstance(coordination["cluster_host"], str)
            or type(coordination["old_worker_stopped"]) is not bool
            or type(coordination["trust_internal_ca"]) is not bool):
        parser.error("Malformed coordinator configuration")
    if "credential_sync" in config:
        try:
            credential_sync_setup.validate(config["credential_sync"])
        except (ValueError, OSError):
            parser.error("Invalid credential_sync configuration; verify paths, peer identity and fingerprint")
    try:
        selected_permissions = permission_plan(config)
    except ValueError as error:
        parser.error(str(error))
    phases = set(args.phase or PHASES)
    while True:
        expanded = phases | {dependency for phase in phases for dependency in DEPENDENCIES.get(phase, ())}
        if expanded == phases:
            break
        phases = expanded
    phases = [phase for phase in PHASES if phase in phases]
    if not args.apply:
        print(json.dumps({"mode": "plan", "phases": phases, "formulas": FORMULAS,
                          "browser": config["browser_path"], "runner_families": selected_permissions["runner_families"] if "runners" in phases else [],
                          "permission_setup": {"peekaboo": selected_permissions["peekaboo"] if "permissions" in phases else [],
                                               "runner_families": selected_permissions["runner_families"] if "runners" in phases else []},
                          "changes_performed": False,
                          "credential_sync": {"enabled": config.get("credential_sync", {}).get("enabled", False),
                                              "peer_enrollment": "explicit local review on peer", "automatic_transfer": False},
                          "manual": ["Xcode/Apple account sign-in", "Existing signing private-key import",
                                     "SSH/GitHub authentication and verified host keys", "macOS and Codex consent"]}, indent=2))
        return 0
    if sys.platform != "darwin" or platform.machine() != "arm64" or os.getuid() == 0:
        parser.error("Run as the signed-in user on an Apple Silicon Mac, not with sudo")
    os.umask(0o077)
    return Setup(config, phases).run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit("Interrupted. Review the last private report and any runner cleanup before resuming; no automatic replay.")
