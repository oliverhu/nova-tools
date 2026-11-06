"""Bounded CLI metadata for the existing resource pulse; never starts a worker."""
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat


RUN_STATES = frozenset(('none', 'starting', 'running', 'completed', 'needs_attention',
                        'needs_reconciliation', 'settled', 'continued', 'unknown'))
ISSUES = frozenset(('local_status_unavailable', 'invalid_local_state', 'run_state_unknown'))
FIELDS = frozenset(('available', 'busy', 'run_state', 'task_id', 'task_generation',
                    'completion_verified', 'activity_at', 'started_at', 'ended_at', 'issue'))


def validate_cli_status(value):
    """Reject accidental raw state/report uploads, including private extra keys."""
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError('Invalid CLI status metadata')
    for field in ('available', 'busy', 'completion_verified'):
        if type(value.get(field)) is not bool:
            raise ValueError('Invalid CLI status metadata')
    if value.get('run_state') not in RUN_STATES:
        raise ValueError('Invalid CLI status metadata')
    task = value.get('task_id')
    if task is not None and (not isinstance(task, str) or not re.fullmatch('[0-9a-f]{16}', task)):
        raise ValueError('Invalid CLI status metadata')
    generation = value.get('task_generation')
    if generation is not None and (type(generation) is not int or not 0 <= generation <= 1_000_000):
        raise ValueError('Invalid CLI status metadata')
    for field in ('activity_at', 'started_at', 'ended_at'):
        number = value.get(field)
        if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 1e12):
            raise ValueError('Invalid CLI status metadata')
    if value.get('issue') is not None and value['issue'] not in ISSUES:
        raise ValueError('Invalid CLI status metadata')
    return {key: value.get(key) for key in FIELDS}


def _unavailable():
    return validate_cli_status(dict(available=False, busy=False, run_state='unknown',
                                    task_id=None, completion_verified=False, issue='local_status_unavailable'))


def _private_json(path):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | os.O_NONBLOCK
    fd = os.open(path, flags)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_size > 262144 or info.st_nlink != 1):
            raise ValueError('Invalid local state')
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError('Invalid local state')
    return value, info.st_mtime


def collect_cli_status(worker, root=None):
    """Read own run metadata and nonblocking lock state without changing files.

    Progress/result text comes from the queue, not private CLI events or stderr.
    No credentials, claims, prompts, account/model data, PIDs or paths are sent.
    """
    root = Path(root) if root is not None else Path.home()/'.local/state/nova-cli-dispatch'
    try:
        if root.is_symlink() or not root.is_dir():
            return _unavailable()
        root = root.resolve()
        info = root.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            return _unavailable()
        # Match cli_dispatch's existing lock; do not create or retain it.
        fd = os.open(root/'worker.lock', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return _unavailable()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                busy = False
            except BlockingIOError:
                busy = True
        finally:
            os.close(fd)
        records = []
        for directory in root.iterdir():
            if not re.fullmatch('[0-9a-f]{32}', directory.name):
                continue
            if directory.is_symlink() or not directory.is_dir():
                return _unavailable()
            state = directory/'state.json'
            if not state.exists():
                return _unavailable()
            record, modified = _private_json(state)
            if record.get('worker') != worker:
                return _unavailable()
            created = record.get('created_at')
            if type(created) not in (int, float) or not math.isfinite(created):
                return _unavailable()
            records.append((created, record, modified, directory))
            if len(records) > 1000:
                return _unavailable()
        if not records:
            return validate_cli_status(dict(available=True, busy=busy, run_state='unknown' if busy else 'none',
                                            task_id=None, completion_verified=False))
        _, latest, modified, directory = max(records, key=lambda row: row[0])
        run_state = latest.get('status')
        issue = None
        if run_state not in RUN_STATES:
            run_state, issue = 'unknown', 'run_state_unknown'
        elif not busy and run_state in ('starting', 'running'):
            run_state = 'needs_reconciliation'
        generation = None
        try:
            readback, _ = _private_json(directory/'task-readback.json')
            if readback.get('id') == latest.get('task'):
                generation = readback.get('generation')
        except (OSError, ValueError):
            pass
        return validate_cli_status(dict(available=True, busy=busy, run_state=run_state,
            task_id=latest.get('task'), task_generation=generation,
            completion_verified=latest.get('completion_verified') is True,
            activity_at=modified, started_at=latest.get('started_at'), ended_at=latest.get('ended_at'), issue=issue))
    except (OSError, ValueError, TypeError, RecursionError):
        return _unavailable()
