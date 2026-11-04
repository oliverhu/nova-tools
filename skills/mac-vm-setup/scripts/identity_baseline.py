#!/usr/bin/env python3
"""Versioned, nonsecret host identity baseline; no credential export or account UI."""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import socket
import ssl
import subprocess

PROFILE_DIRS = ("Library/Developer/Xcode/UserData/Provisioning Profiles",
                "Library/MobileDevice/Provisioning Profiles")
KINDS = ("Apple Development", "Apple Distribution", "Developer ID Application")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run(args, timeout=15, input_data=None):
    env = dict(os.environ, SSH_ASKPASS_REQUIRE="never", GIT_TERMINAL_PROMPT="0")
    for name in ("SSH_ASKPASS", "SSH_AUTH_SOCK", "GIT_SSH", "GIT_SSH_COMMAND", "GH_DEBUG"):
        env.pop(name, None)
    process = subprocess.Popen([str(x) for x in args], stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env, start_new_session=True)
    try:
        stdout, stderr = process.communicate(input=input_data, timeout=timeout)
        return process.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return 124, b"", b""


def canonical(path):
    path = Path(path).expanduser().absolute()
    if path != path.resolve():
        raise ValueError("Symlink path refused: " + str(path))
    return path


def read_file(path, limit=4 * 1024 * 1024):
    path = canonical(path)
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > limit:
        raise ValueError("Expected bounded single-link regular file: " + str(path))
    return path.read_bytes()


def private_write(path, data):
    path = canonical(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
        raise ValueError("Output parent must be current-user-owned and 0700")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def public_fingerprint(path):
    fields = read_file(path, 16384).split()
    if len(fields) < 2 or fields[0] != b"ssh-ed25519":
        raise ValueError("Expected explicit Ed25519 public key file")
    raw = base64.b64decode(fields[1], validate=True)
    if len(raw) != 51 or raw[:19] != b"\0\0\0\x0bssh-ed25519\0\0\0\x20":
        raise ValueError("Malformed Ed25519 public key")
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def profile(path, team):
    path = canonical(path)
    raw = read_file(path)
    code, output, _ = run(["/usr/bin/security", "cms", "-D", "-i", path])
    if code:
        raise ValueError("Profile CMS decode failed: " + path.name)
    value = plistlib.loads(output)
    ent = value.get("Entitlements", {})
    app = ent.get("application-identifier", ent.get("com.apple.application-identifier", ""))
    expiry = value.get("ExpirationDate")
    if not isinstance(expiry, dt.datetime):
        raise ValueError("Profile expiry missing")
    expiry = expiry.replace(tzinfo=dt.timezone.utc) if expiry.tzinfo is None else expiry
    uid = value.get("UUID", "")
    if (team not in value.get("TeamIdentifier", []) or not app.startswith(team + ".")
            or "*" in app or expiry <= dt.datetime.now(dt.timezone.utc)
            or not re.fullmatch(r"[0-9a-fA-F-]{36}", uid)
            or path.name != uid + path.suffix):
        raise ValueError("Profile team/app/expiry/filename mismatch: " + path.name)
    platforms = value.get("Platform", [])
    development = ent.get("get-task-allow", ent.get("com.apple.security.get-task-allow", False))
    if "OSX" in platforms:
        role = "mac_distribution" if value.get("ProvisionsAllDevices") else "mac_development"
    elif "iOS" in platforms:
        role = "ios_development" if development else "ios_distribution"
    else:
        raise ValueError("Unsupported provisioning platform")
    return {"file": path.name, "uuid": uid, "sha256": digest(raw), "team": team,
            "application_identifier": app, "platforms": platforms, "role": role,
            "expires_at": expiry.isoformat(),
            "certificate_fingerprints": sorted(hashlib.sha1(x).hexdigest().upper()
                                                for x in value.get("DeveloperCertificates", []))}


def available_identities(team=None):
    code, output, _ = run(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"])
    if code:
        raise ValueError("Signing identity metadata unavailable")
    result = {}
    for fingerprint, name in re.findall(r'\b([0-9A-F]{40})\s+"([^\"]+)"', output.decode()):
        for kind in KINDS:
            if name.startswith(kind + ":"):
                result[fingerprint] = kind
    if team is None:
        return result
    scoped = {}
    for kind in KINDS:
        code, certificates, _ = run(["/usr/bin/security", "find-certificate", "-a", "-c", kind + ":", "-p"])
        if code:
            continue
        for certificate in re.findall(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", certificates, re.DOTALL):
            fingerprint = hashlib.sha1(ssl.PEM_cert_to_DER_cert(certificate.decode())).hexdigest().upper()
            if fingerprint not in result:
                continue
            code, subject, _ = run(["/usr/bin/openssl", "x509", "-noout", "-subject", "-nameopt", "RFC2253"], input_data=certificate)
            if code == 0 and re.search(r"(?:^|[, ])OU=" + re.escape(team) + r"(?:,|\s*$)", subject.decode()):
                scoped[fingerprint] = result[fingerprint]
    return scoped


def revision(baseline):
    body = {k: v for k, v in baseline.items() if k != "revision"}
    return digest(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())


def validate(baseline):
    if baseline.get("schema") != 1 or baseline.get("revision") != revision(baseline):
        raise ValueError("Baseline schema/revision mismatch")
    gh = baseline["github"]
    if (gh.get("policy") != "shared_host_key" or not re.fullmatch(r"[A-Za-z0-9-]+", gh["account"])
            or not re.fullmatch(r"git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", gh["repository"])
            or not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", gh["public_fingerprint"])):
        raise ValueError("Unsupported GitHub baseline")
    if not re.fullmatch(r"[A-Z0-9]{10}", baseline["apple_team"]):
        raise ValueError("Invalid Apple team")
    if not baseline["signing_identities"] or any(
            not re.fullmatch(r"[0-9A-F]{40}", x["fingerprint"]) or x["kind"] not in KINDS
            for x in baseline["signing_identities"]):
        raise ValueError("Invalid signing identity baseline")
    names = set()
    for p in baseline["profiles"]:
        if (not re.fullmatch(r"[0-9a-fA-F-]{36}\.(provisionprofile|mobileprovision)", p["file"])
                or p["file"] in names or not re.fullmatch(r"[0-9a-f]{64}", p["sha256"])
                or p["team"] != baseline["apple_team"]
                or p["role"] not in ("mac_development", "mac_distribution", "ios_development", "ios_distribution")
                or any(not re.fullmatch(r"[0-9A-F]{40}", x) for x in p["certificate_fingerprints"])):
            raise ValueError("Unsafe baseline profile entry")
        names.add(p["file"])
    return baseline


def capture(args):
    directory = canonical(args.profiles)
    profiles = [profile(p, args.team) for p in sorted(directory.iterdir())
                if p.suffix in (".provisionprofile", ".mobileprovision")]
    if not profiles:
        raise ValueError("No named provisioning profiles")
    identities = available_identities(args.team)
    selected = [{"fingerprint": f, "kind": identities[f]} for f in sorted(identities)]
    if not selected:
        raise ValueError("No host signing identities match the selected Apple team")
    baseline = {"schema": 1, "source_host": socket.gethostname(), "created_at": now(),
                "github": {"policy": "shared_host_key", "account": args.account,
                           "repository": args.repository, "public_fingerprint": public_fingerprint(args.public_key)},
                "apple_team": args.team, "signing_identities": selected, "profiles": profiles,
                "notarization_profile": args.notary_profile,
                "source_profile_identity_gaps": [p["file"] for p in profiles
                    if not set(p["certificate_fingerprints"]) & identities.keys()],
                "private_material_included": False}
    baseline["revision"] = revision(baseline)
    return validate(baseline)


def profile_destinations(home):
    return [home / x for x in PROFILE_DIRS]


def apply_profiles(baseline, source, home):
    """Preflight the complete selection; never replace differing destination bytes."""
    jobs = []
    for expected in baseline["profiles"]:
        p = canonical(source) / expected["file"]
        if profile(p, baseline["apple_team"]) != expected:
            raise ValueError("Source profile differs from baseline: " + p.name)
        data = read_file(p)
        for directory in profile_destinations(home):
            target = canonical(directory / p.name)
            for ancestor in [directory, *directory.parents]:
                if ancestor.exists() and (ancestor.stat().st_mode & 0o022):
                    raise ValueError("Writable profile ancestor refused")
            if target.exists():
                if target.stat().st_uid != os.getuid() or read_file(target) != data:
                    raise ValueError("Different existing profile preserved: " + str(target))
            jobs.append((target, data))
    changed = []
    for target, data in jobs:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.exists():
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            changed.append(str(target))
        else:
            if read_file(target) != data:
                raise ValueError("Concurrent profile change; preserved")
            target.chmod(0o600)
        if read_file(target) != data:
            raise ValueError("Profile readback failed")
    return {"baseline_revision": baseline["revision"], "installed_paths": changed,
            "verified_paths": [str(p) for p, _ in jobs], "credentials_or_accounts_changed": False}


def check(baseline, public_key, home, network=False, checkout=None):
    checks = {}
    def record(name, passed, **details):
        checks[name] = {"status": "passed" if passed else "failed", **details}
    expected = baseline["github"]
    try:
        observed = public_fingerprint(public_key)
        record("github_public_identity", observed == expected["public_fingerprint"], fingerprint=observed)
    except (OSError, ValueError):
        record("github_public_identity", False, reason="Public key unavailable or invalid")
    code, output, _ = run(["/usr/bin/ssh", "-G", "git@github.com"])
    config = {}
    for line in output.decode().splitlines():
        key, _, value = line.partition(" ")
        config.setdefault(key, []).append(value)
    identity = str(Path(public_key).expanduser().absolute()).removesuffix(".pub")
    selected = [str(Path(x).expanduser().absolute()) for x in config.get("identityfile", [])]
    route_ok = (code == 0 and selected == [identity] and config.get("hostname") == ["github.com"]
                and config.get("user") == ["git"] and config.get("port") == ["22"]
                and config.get("identitiesonly") == ["yes"]
                and config.get("stricthostkeychecking") in (["true"], ["yes"])
                and config.get("proxycommand", ["none"]) == ["none"]
                and config.get("proxyjump", ["none"]) == ["none"])
    record("github_selected_route", route_ok, identity_path=identity)
    available = {}
    try:
        available = available_identities()
        missing = [x for x in baseline["signing_identities"] if x["fingerprint"] not in available]
        record("signing_identity_presence", not missing, missing=missing, private_key_use_tested=False)
    except (OSError, ValueError):
        record("signing_identity_presence", False, reason="Identity metadata unavailable")
    role_readiness = {}
    for expected_profile in baseline["profiles"]:
        installed = False
        for index, directory in enumerate(profile_destinations(home)):
            p = directory / expected_profile["file"]
            if not p.exists() and not p.is_symlink():
                checks["profile:" + str(index) + ":" + p.name] = {
                    "status": "not_present", "path": str(p), "role": expected_profile["role"]}
                continue
            try:
                actual = profile(p, baseline["apple_team"])
                ok = actual == expected_profile and p.stat().st_uid == os.getuid() and not p.stat().st_mode & 0o077
                record("profile:" + str(index) + ":" + p.name, ok, path=str(p), role=expected_profile["role"])
                installed = installed or ok
            except (OSError, ValueError, plistlib.InvalidFileException):
                record("profile:" + str(index) + ":" + p.name, False, path=str(p), reason="Missing, expired, invalid, or differing profile")
        record("profile_installed:" + expected_profile["file"], installed,
               requirement="At least one supported Xcode profile cache")
        role = expected_profile["role"]
        role_readiness[role] = role_readiness.get(role, False) or bool(installed and set(expected_profile["certificate_fingerprints"]) & available.keys())
    for role in ("mac_development", "mac_distribution", "ios_development", "ios_distribution"):
        record("profile_identity_coverage:" + role, role_readiness.get(role, False))
    if network and checks["github_public_identity"]["status"] == "passed" and route_ok:
        ssh = ["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes",
               "-o", "StrictHostKeyChecking=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none",
               "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1"]
        code, a, b = run(ssh + ["-T", "git@github.com"])
        greeting = ("Hi " + expected["account"] + "! You've successfully authenticated").encode()
        auth = code == 1 and greeting in a + b
        record("github_authentication", auth, exit=code, account=expected["account"])
        if auth:
            # Exact baseline repository via the already checked route; no checkout rewrite or fetch.
            import shlex
            command = ["/usr/bin/git", "-c", "core.sshCommand=" + shlex.join(ssh),
                       "ls-remote", expected["repository"], "refs/heads/main"]
            if checkout:
                command[1:1] = ["-C", str(checkout)]
            url_command = ["/usr/bin/git"] + (["-C", str(checkout)] if checkout else []) + ["ls-remote", "--get-url", expected["repository"]]
            url_code, url, _ = run(url_command)
            if url_code != 0 or url.decode().strip() != expected["repository"]:
                record("github_repository_read", False, reason="Repository URL rewrite or resolution differs; no read attempted")
                code, a = 78, b""
            else:
                code, a, _ = run(command, 20)
            fields = a.decode().strip().split()
            ok = code == 0 and len(fields) == 2 and re.fullmatch(r"[0-9a-f]{40}", fields[0]) and fields[1] == "refs/heads/main"
            if "github_repository_read" not in checks:
                record("github_repository_read", bool(ok), exit=code, observed_main=fields[0] if ok else None)
    for name in ("github_authentication", "github_repository_read", "normal_push", "signing_use", "xcode_account", "notarization"):
        checks.setdefault(name, {"status": "not_exercised"})
    return {"schema": 1, "baseline_revision": baseline["revision"], "hostname": socket.gethostname(),
            "checked_at": now(), "checks": checks,
            "identity_parity": all(x["status"] != "failed" for x in checks.values()),
            "release_readiness": "not_established_by_asset_inventory",
            "processes_stopped": True, "credentials_or_settings_changed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("capture", help="Create a private nonsecret baseline from named host assets")
    create.add_argument("--public-key", required=True)
    create.add_argument("--profiles", required=True)
    create.add_argument("--team", required=True)
    create.add_argument("--account", required=True)
    create.add_argument("--repository", required=True)
    create.add_argument("--notary-profile", required=True)
    create.add_argument("--output", required=True)
    for name in ("check", "apply-profiles"):
        command = sub.add_parser(name)
        command.add_argument("--baseline", required=True)
        command.add_argument("--output", required=True)
        if name == "check":
            command.add_argument("--public-key", required=True)
            command.add_argument("--network", action="store_true", help="One bounded account auth and repository read")
            command.add_argument("--checkout", type=Path)
        else:
            command.add_argument("--source", required=True)
            command.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "capture":
            result = capture(args)
        else:
            baseline = validate(json.loads(read_file(args.baseline)))
            if args.command == "check":
                result = check(baseline, args.public_key, Path.home(), args.network, args.checkout)
            elif args.apply:
                result = apply_profiles(baseline, args.source, Path.home())
            else:
                result = {"mode": "plan", "baseline_revision": baseline["revision"],
                          "profile_files": [p["file"] for p in baseline["profiles"]],
                          "destinations": [str(x) for x in profile_destinations(Path.home())]}
        private_write(args.output, (json.dumps(result, indent=2) + "\n").encode())
        print(json.dumps({"output": str(Path(args.output).expanduser()),
                          "sha256": digest(Path(args.output).expanduser().read_bytes()),
                          "identity_parity": result.get("identity_parity"),
                          "revision": result.get("revision", result.get("baseline_revision"))}))
        return 78 if result.get("identity_parity") is False else 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Never surface subprocess output (which can contain account/credential material).
        print(json.dumps({"status": "stopped", "error_type": type(error).__name__,
                          "reason": str(error) if isinstance(error, ValueError) else "Invalid input or unavailable local dependency"}))
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
