#!/usr/bin/env python3
"""Prepare/apply one guest's exact CLI module update; no services or credentials.

Plan creates immutable before/after bytes. Apply and rollback compare every
current hash first. The owner checks readiness and coordinates a safe boundary.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coordinator.cli_dispatch import save

FILES = ('cli_results.py', 'cli_messages.py', 'publication_ancestry.py', 'client.py', 'cli_dispatch.py')
EVENT_FILES = ('cli_event_dispatch.py', 'cli_dispatch.py', 'client.py')
EVENT_FILES_V2 = ('cli_results.py', *EVENT_FILES)
RECOVERY_FILES_V1 = ('cli_recovery.py', 'cli_results.py', 'cli_dispatch.py', 'client.py')


def profile_files(profile):
    if profile == 'scoped-recovery-v1': return RECOVERY_FILES_V1
    if profile == 'results': return FILES
    if profile == 'event-dispatch': return EVENT_FILES
    if profile == 'event-dispatch-v2': return EVENT_FILES_V2
    raise ValueError('Unknown fixed rollout profile')


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def read(path):
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError('Canonical nonsymlink rollout paths required')
    if not path.exists(): return None
    if not path.is_file() or path.stat().st_uid != os.getuid():
        raise ValueError('Owned regular rollout files required')
    return path.read_bytes()


def plan(source, destination, directory, worker, profile='results'):
    selected = profile_files(profile)
    if directory.absolute() != directory.resolve():
        raise ValueError('Canonical rollout directory required')
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    files = {}
    for name in selected:
        before, after = read(destination/name), read(source/name)
        if after is None: raise ValueError('Missing source module')
        compile(after, str(source/name), 'exec')
        for kind, raw in (('before', before), ('after', after)):
            if raw is not None:
                path = directory/(name+'.'+kind)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream: stream.write(raw)
        files[name] = {'before': digest(before), 'after': digest(after)}
    manifest = {'schema': 1, 'hostname': socket.gethostname(), 'worker': worker,
                'source': str(source), 'destination': str(destination), 'files': files}
    if profile != 'results': manifest['profile'] = profile
    save(directory/'manifest.json', manifest)
    return manifest


def apply(directory, worker, rollback=False):
    manifest = json.loads(read(directory/'manifest.json'))
    selected = profile_files(manifest.get('profile', 'results'))
    if (manifest.get('schema') != 1 or manifest.get('hostname') != socket.gethostname()
            or manifest.get('worker') != worker or set(manifest.get('files', {})) != set(selected)):
        raise ValueError('Exact guest/worker/module rollout binding required')
    destination = Path(manifest['destination'])
    before, after = ('after', 'before') if rollback else ('before', 'after')
    payloads = {}
    for name in selected:
        expected = manifest['files'][name]
        if digest(read(destination/name)) not in (expected[before], expected[after]):
            raise ValueError('Installed module differs; preserve and inspect before rollout')
        raw = read(directory/(name+'.'+after))
        if digest(raw) != expected[after]: raise ValueError('Rollout bytes changed')
        payloads[name] = raw
    # Dependencies first, entry point last. A partial update stays recoverable;
    # its exact affected bytes and hashes are preserved in the plan directory.
    for name in selected:
        current = digest(read(destination/name))
        if current == manifest['files'][name][after]:
            continue
        if current != manifest['files'][name][before]:
            raise ValueError('Installed module changed during rollout')
        raw = payloads[name]
        target = destination/name
        if raw is None:
            target.unlink()
        else:
            temporary = target.with_name(name+'.nova-rollout-'+str(os.getpid()))
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, target)
    if any(digest(read(destination/n)) != manifest['files'][n][after] for n in selected):
        raise ValueError('Rollout verification failed')
    receipt = {'schema': 1, 'worker': worker, 'hostname': socket.gethostname(),
               'operation': 'rollback' if rollback else 'apply', 'verified': True,
               'files': {n: manifest['files'][n][after] for n in selected},
               'services_restarted': False, 'credentials_changed': False}
    save(directory/(receipt['operation']+'-receipt.json'), receipt)
    return receipt


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('plan', 'apply', 'rollback'))
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--profile', choices=('results', 'event-dispatch', 'event-dispatch-v2', 'scoped-recovery-v1'), default='results',
                        help='Fixed module set for plan; apply/rollback use the pinned manifest')
    args = parser.parse_args()
    config = json.loads((Path.home()/'.config/nova-coordinator/cli-dispatch.json').read_text())
    if config['hostname'] != socket.gethostname(): raise ValueError('Wrong guest')
    if args.operation == 'plan':
        result = plan(Path(__file__).resolve().parents[1]/'coordinator',
                      Path.home()/'.local/share/nova-coordinator/coordinator',
                      args.directory, config['worker'], profile=args.profile)
    else:
        result = apply(args.directory, config['worker'], args.operation == 'rollback')
    print(json.dumps(result, indent=2))


if __name__ == '__main__': main()
