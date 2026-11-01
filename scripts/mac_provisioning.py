"""Shared, import-safe validation for private Nova provisioning bundles."""
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tarfile

TEAM = "2243E5YVZ9"
MAX_PROFILE = 4 * 1024 * 1024
MAX_BUNDLE = 64 * 1024 * 1024
SUFFIXES = (".provisionprofile", ".mobileprovision")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def private_directory(path, *, allow_readable=False):
    path = Path(path).expanduser().absolute()
    if path != path.resolve():
        raise ValueError("Private output must not pass through a symlink: " + str(path))
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise ValueError("Private directory belongs to another user")
    if path.stat().st_mode & (0o022 if allow_readable else 0o077):
        raise ValueError("Directory permissions are too broad: " + str(path))
    return path


def write_new(path, data, *, restrict_existing=False):
    """Preserve existing files; an identical result is safe to resume."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Refusing a symlink destination")
    if path.exists():
        if not path.is_file() or path.stat().st_uid != os.getuid() or path.read_bytes() != data:
            raise ValueError("Existing file differs; preserved: " + str(path))
        if path.stat().st_mode & 0o077:
            if restrict_existing:
                path.chmod(0o600)
            else:
                raise ValueError("Existing private file is too broadly readable: " + str(path))
        return
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)


def profile_info(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_PROFILE:
        raise ValueError("Profile must be a bounded regular file")
    result = subprocess.run(["/usr/bin/security", "cms", "-D", "-i", str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    profile = plistlib.loads(result.stdout)
    if not isinstance(profile, dict):
        raise ValueError("Profile CMS content is not a dictionary")
    entitlements = profile.get("Entitlements", {})
    if not isinstance(entitlements, dict):
        raise ValueError("Malformed profile entitlements")
    identifier = entitlements.get("application-identifier", entitlements.get("com.apple.application-identifier", ""))
    if not isinstance(identifier, str):
        raise ValueError("Malformed application identifier")
    app = identifier.split(".", 1)[-1]
    expiration = profile.get("ExpirationDate")
    if not isinstance(expiration, dt.datetime):
        raise ValueError("Profile has no expiration")
    expiration = expiration.replace(tzinfo=dt.timezone.utc) if expiration.tzinfo is None else expiration
    uuid = profile.get("UUID", "")
    platforms = profile.get("Platform", [])
    teams = profile.get("TeamIdentifier", [])
    if (not isinstance(teams, list) or TEAM not in teams or not app.startswith("app.novabrowser.")
            or "*" in app or expiration <= dt.datetime.now(dt.timezone.utc)
            or not isinstance(uuid, str) or not re.fullmatch(r"[0-9A-Fa-f-]{36}", uuid)
            or not isinstance(platforms, list) or not platforms
            or any(not isinstance(item, str) for item in platforms)):
        raise ValueError("Profile is expired or outside the Nova team/application scope")
    info = {"uuid": uuid, "team": TEAM, "application": app,
            "platforms": platforms, "expires": expiration.isoformat(),
            "sha256": sha(path.read_bytes())}
    return info, profile


def unpack_bundle(data, destination):
    """Do not extract archive paths or trust manifest metadata without CMS checks."""
    if len(data) > MAX_BUNDLE:
        raise ValueError("Provisioning bundle exceeds 64 MiB")
    destination = private_directory(destination)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if (not 2 <= len(members) <= 101 or len(names) != len(set(names))
                or sum(member.size for member in members) > MAX_BUNDLE
                or any(not member.isfile() or member.size > MAX_PROFILE or member.size < 0 for member in members)
                or "manifest.json" not in names):
            raise ValueError("Unsafe or oversized provisioning archive")
        manifest = json.loads(archive.extractfile("manifest.json").read())
        if not isinstance(manifest, dict):
            raise ValueError("Malformed provisioning manifest")
        entries = manifest.get("profiles")
        if manifest.get("schema") != 1 or manifest.get("team") != TEAM or not isinstance(entries, list):
            raise ValueError("Unsupported provisioning manifest")
        if any(not isinstance(entry, dict) or not isinstance(entry.get("file"), str) for entry in entries):
            raise ValueError("Malformed provisioning manifest entry")
        expected = [entry["file"] for entry in entries]
        if len(expected) != len(set(expected)) or set(names) != set(expected) | {"manifest.json"}:
            raise ValueError("Unexpected or duplicate bundle entries")
        validated = []
        for entry in entries:
            name = entry["file"]
            if not re.fullmatch(r"[0-9A-Fa-f-]{36}\.(?:mobileprovision|provisionprofile)", name):
                raise ValueError("Only UUID-named provisioning profiles are allowed")
            raw = archive.extractfile(name).read()
            if sha(raw) != entry.get("sha256"):
                raise ValueError("Profile checksum mismatch")
            target = destination / name
            write_new(target, raw)
            actual, decoded = profile_info(target)
            if any(entry.get(key) != value for key, value in actual.items()):
                raise ValueError("Profile CMS metadata differs from manifest")
            if name != actual["uuid"] + Path(name).suffix:
                raise ValueError("Profile UUID differs from its filename")
            validated.append((target, actual, decoded))
    return validated
