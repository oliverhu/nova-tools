#!/usr/bin/env python3
"""Explicitly export Nova profiles, never certificates/private keys or accounts."""
import argparse
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

sys.dont_write_bytecode = True
from mac_provisioning import MAX_BUNDLE, SUFFIXES, TEAM, private_directory, profile_info, sha, write_new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New .tar.gz outside a source checkout")
    parser.add_argument("--apply", action="store_true", help="Read the selected local profiles and create the private bundle")
    args = parser.parse_args()
    if not args.apply:
        print("Plan: export unexpired Nova-only profiles from the two Xcode profile directories and browser/.build/provisioning.")
        print("No profile or keychain data read. Add --apply to create the bundle.")
        return
    if sys.platform != "darwin" or os.getuid() == 0:
        parser.error("Run as the signed-in Mac user, not root")
    os.umask(0o077)
    output = args.output.expanduser().absolute()
    if not output.name.endswith(".tar.gz"):
        parser.error("Use a .tar.gz output filename")
    parent = private_directory(output.parent)
    if any((folder / ".git").exists() for folder in (parent, *parent.parents)):
        parser.error("Keep provisioning bundles outside every source checkout")
    roots = [Path.home() / "Library/MobileDevice/Provisioning Profiles",
             Path.home() / "Library/Developer/Xcode/UserData/Provisioning Profiles",
             args.browser.expanduser() / ".build/provisioning"]
    profiles = {}
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        for path in sorted(root.iterdir()):
            if path.suffix not in SUFFIXES:
                continue
            try:
                info, _ = profile_info(path)
            except (ValueError, OSError, subprocess.CalledProcessError):
                continue
            name = info["uuid"] + path.suffix
            raw = path.read_bytes()
            if name in profiles and profiles[name][1] != raw:
                raise ValueError("Same profile UUID has differing bytes; resolve the source profiles first")
            profiles[name] = ({"file": name, **info}, raw)
    if not profiles or len(profiles) > 100:
        raise ValueError("Need 1–100 current Nova profiles. Download them in Xcode Settings > Accounts first.")
    manifest = {"schema": 1, "team": TEAM, "profiles": [pair[0] for pair in profiles.values()]}
    payloads = {"manifest.json": json.dumps(manifest, indent=2).encode(),
                **{name: pair[1] for name, pair in profiles.items()}}
    if sum(map(len, payloads.values())) > MAX_BUNDLE:
        raise ValueError("Profile bundle exceeds 64 MiB")
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, raw in payloads.items():
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                member.mode = 0o600
                archive.addfile(member, io.BytesIO(raw))
    data = stream.getvalue()
    write_new(output, data)
    print(json.dumps({"bundle": str(output), "sha256": sha(data), "profiles": len(profiles),
                      "private_keys_exported": False}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
