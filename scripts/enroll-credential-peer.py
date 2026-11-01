#!/usr/bin/env python3
"""Review or enroll one public key locally on its intended credential-sync peer."""
import argparse
import json
import os
import sys
from credential_sync import SyncError, read_file
from credential_sync_setup import enroll


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--install-dir", required=True)
    p.add_argument("--public-key-file", required=True)
    p.add_argument("--ssh-dir", default="~/.ssh")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if os.getuid() == 0:
        raise SyncError("Run as the intended SSH user, without sudo")
    raw = read_file(args.public_key_file, 4096, private=False)
    if raw is None:
        raise SyncError("Select the enrolling machine's public key file")
    print(json.dumps(enroll(args.install_dir, raw.decode().strip(), args.ssh_dir, args.apply), indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, SyncError) else "Enrollment failed; no private data logged", file=sys.stderr)
        sys.exit(1)
