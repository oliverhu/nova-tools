#!/usr/bin/python3
"""Read-only SSH access to Nova intake artifacts; never executes a shell."""
import hashlib
import json
import os
import shlex
import shutil
import stat
import sys

ROOT = "/Users/oliverhu/Documents/ChatGPT/exports/nova-task-intake"


def open_artifact(relative):
    parts = relative.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("Use an artifact path relative to the intake directory")
    directory = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError("Only regular artifact files can be read")
        return os.fdopen(descriptor, "rb")
    finally:
        os.close(directory)


def main():
    arguments = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    if arguments == ["ping"]:
        print(json.dumps({"service": "nova-artifact-read", "version": 1,
                          "access": "read-only"}))
        return
    if len(arguments) != 2 or arguments[0] not in ("stat", "fetch"):
        raise ValueError("Allowed commands: ping, stat RELATIVE_PATH, fetch RELATIVE_PATH")
    operation, relative = arguments
    with open_artifact(relative) as artifact:
        if operation == "fetch":
            shutil.copyfileobj(artifact, sys.stdout.buffer, 1024 * 1024)
        else:
            digest = hashlib.sha256()
            count = 0
            for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                digest.update(chunk)
                count += len(chunk)
            print(json.dumps({"path": relative, "bytes": count,
                              "sha256": digest.hexdigest()}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print("Artifact request refused: " + str(error), file=sys.stderr)
        sys.exit(1)
