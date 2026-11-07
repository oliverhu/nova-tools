#!/usr/bin/env python3
"""Install worker skills from an approved Git commit at an idle/safe boundary.

This updates instruction files only: no credentials, claims, runtime code,
permissions, schedules or app processes. The caller obtains the approved commit
from the coordination service and separately acknowledges actual instruction use.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid

SOURCES = {'nova-coordination': 'skill', 'mac-vm-setup': 'skills/mac-vm-setup'}


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE)


def inventory(root):
    if root.is_symlink():
        raise ValueError('Managed skill must not be a symlink')
    if not root.exists():
        return None
    if not root.is_dir():
        raise ValueError('Managed skill must be a directory')
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Managed skill contains a symlink')
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_dir():
            raise ValueError('Managed skill contains an unsupported file type')
    return result


def stage(repo, revision, destination):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('An exact approved 40-character Git commit is required')
    actual = git(repo, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    if actual != revision:
        raise ValueError('Approved revision is not the selected commit')
    entries = git(repo, 'ls-tree', '-r', '-z', revision, '--', *SOURCES.values()).split(b'\0')
    count = size = 0
    for entry in filter(None, entries):
        metadata, rawpath = entry.split(b'\t', 1)
        mode, kind, oid = metadata.decode().split()
        path = rawpath.decode('utf-8')
        if mode not in ('100644', '100755') or kind != 'blob':
            raise ValueError('Skill source must contain only regular Git files')
        for name, prefix in SOURCES.items():
            if path.startswith(prefix + '/'):
                relative = Path(path[len(prefix) + 1:])
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('Unsafe source path')
                data = git(repo, 'cat-file', 'blob', oid)
                count += 1; size += len(data)
                if count > 1000 or size > 10 * 1024 * 1024:
                    raise ValueError('Instruction bundle exceeds size limit')
                target = destination / name / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(0o755 if mode == '100755' else 0o644)
                break
    for name in SOURCES:
        if not (destination / name / 'SKILL.md').is_file():
            raise ValueError('Approved revision is missing a required skill')
    return {name: inventory(destination / name) for name in SOURCES}


def synchronize(repo, revision, skills, state):
    skills, state = Path(skills).absolute(), Path(state).absolute()
    if state == skills or skills in state.parents:
        raise ValueError('Sync state and backups must remain outside skill discovery')
    # Refuse symlinked destinations rather than writing through an unexpected root.
    for root in (skills, state):
        if any(p.is_symlink() for p in (root, *root.parents)):
            raise ValueError('Destination ancestors must not be symlinks')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lockpath = state / 'sync.lock'
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix='.nova-sync-', dir=skills) as temporary:
            stage_root = Path(temporary)
            expected = stage(repo, revision, stage_root)
            existing = {name: inventory(skills / name) for name in SOURCES}
            changed = [name for name in SOURCES if existing[name] != expected[name]]
            backup = state / 'backups' / uuid.uuid4().hex
            moved, installed = [], []
            try:
                for name in changed:
                    target = skills / name
                    if target.exists():
                        backup.mkdir(parents=True, exist_ok=True, mode=0o700)
                        # copy first: state and skills may live on different volumes.
                        shutil.copytree(target, backup / name)
                        if inventory(backup / name) != existing[name]:
                            raise RuntimeError('Backup verification failed')
                        os.replace(target, stage_root / ('old-' + name))
                        moved.append(name)
                    os.replace(stage_root / name, target)
                    installed.append(name)
                observed = {name: inventory(skills / name) for name in SOURCES}
                if observed != expected:
                    raise RuntimeError('Installed instruction verification failed')
                receipt = {'schema_version': 1, 'source_commit': revision,
                           'installed_hashes': observed, 'changed_skills': changed,
                           'backup_path': str(backup) if moved else None,
                           'instruction_readback_required': True,
                           'worker_readiness_established': False}
                receiptpath = state / 'instruction-installation.json'
                pending = state / ('.receipt-' + uuid.uuid4().hex)
                with os.fdopen(os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
                    json.dump(receipt, output, indent=2); output.write('\n')
                    output.flush(); os.fsync(output.fileno())
                os.replace(pending, receiptpath)
                return receipt
            except BaseException:
                for name in reversed(installed):
                    shutil.rmtree(skills / name)
                for name in reversed(moved):
                    os.replace(stage_root / ('old-' + name), skills / name)
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--revision', required=True, help='Exact commit approved by coordination policy')
    parser.add_argument('--skills-dir', type=Path, default=Path.home() / '.codex/skills')
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/state/nova-coordination/app-worker')
    args = parser.parse_args()
    print(json.dumps(synchronize(args.repository, args.revision, args.skills_dir, args.state_dir), indent=2))


if __name__ == '__main__':
    main()
