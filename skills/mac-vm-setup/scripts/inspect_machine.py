#!/usr/bin/env python3
"""Read selected macOS setup facts; no provisioning, secrets or readiness claims."""
import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import plistlib
import pwd
import subprocess
import sys


def capture(args, timeout=15):
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "error": type(exc).__name__}, None
    if proc.returncode:
        return {"status": "failed", "exit_code": proc.returncode}, None
    return {"status": "observed"}, proc.stdout


def text_fact(args):
    result, output = capture(args)
    if output is not None:
        result["value"] = output.decode("utf-8", errors="replace").strip()
    return result


def inspect_volume(path):
    requested = Path(path).expanduser().absolute()
    if not requested.exists():
        return {"requested": str(requested), "status": "missing"}
    result, output = capture(["/usr/sbin/diskutil", "info", "-plist", str(requested)])
    result["requested"] = str(requested)
    if output is None:
        return result
    try:
        info = plistlib.loads(output)
    except (ValueError, plistlib.InvalidFileException):
        result["status"] = "invalid_response"
        return result
    keys = (
        "MountPoint", "VolumeUUID", "FilesystemType", "FilesystemName",
        "WritableVolume", "WritableMedia", "Internal", "SolidState",
        "DeviceIdentifier", "ParentWholeDisk", "TotalSize", "FreeSpace",
        "APFSContainerReference", "APFSContainerSize", "APFSContainerFree",
    )
    result["diskutil"] = {key: info[key] for key in keys if key in info}
    mount = info.get("MountPoint")
    result["canonical_path"] = str(requested.resolve())
    result["exact_canonical_mount"] = bool(
        mount and str(requested) == str(requested.resolve()) == mount
    )
    try:
        stat = os.statvfs(requested)
        result["filesystem_available_bytes"] = stat.f_bavail * stat.f_frsize
    except OSError:
        result["filesystem_available_bytes"] = None
    result["expected_uuid_checked"] = False
    result["note"] = "Observation only; compare identity to the selected machine plan."
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--volume", action="append", default=[],
                        help="Observe an exact mount; repeat for multiple volumes")
    parser.add_argument("--toolchain", action="store_true",
                        help="Also query Xcode version and macOS/iOS SDK versions")
    args = parser.parse_args()
    if platform.system() != "Darwin":
        print(json.dumps({"status": "unsupported", "reason": "Requires macOS"}))
        return 2
    user = pwd.getpwuid(os.getuid())
    report = {
        "schema": 1,
        "observed_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "scope": "Read-only local inventory; no permission or functional qualification",
        "machine": {
            "hostname": platform.node(),
            "architecture": platform.machine(),
            "uid": os.getuid(),
            "account": user.pw_name,
            "account_home": user.pw_dir,
            "environment_home": str(Path.home()),
            "macos": text_fact(["/usr/bin/sw_vers", "-productVersion"]),
            "memory_bytes": text_fact(["/usr/sbin/sysctl", "-n", "hw.memsize"]),
            "logical_cpus": text_fact(["/usr/sbin/sysctl", "-n", "hw.logicalcpu"]),
            "developer_directory": text_fact(["/usr/bin/xcode-select", "-p"]),
        },
        "volumes": [inspect_volume(path) for path in args.volume],
        "not_checked": ["TCC grants", "Codex tools", "SSH/Git", "signing",
                        "simulator functional boot", "queue", "VM boot", "reboot"],
        "ready_for_work": None,
    }
    if args.toolchain:
        report["toolchain"] = {
            "xcode": text_fact(["/usr/bin/xcodebuild", "-version"]),
            "macos_sdk": text_fact(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-version"]),
            "ios_sdk": text_fact(["/usr/bin/xcrun", "--sdk", "iphoneos", "--show-sdk-version"]),
        }
    print(json.dumps(report, indent=2))
    # Exit zero means inventory ran; individual observations may be unavailable.
    return 0


if __name__ == "__main__":
    sys.exit(main())
