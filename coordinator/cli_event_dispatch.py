"""Event transport inside the existing capacity agent; no new polling job.

The normal CLI wake still owns account checks, local worker.lock and exact task
validation. This module supplies the common durable preclaim fence and publishes
the supervisor's typed settlement only after that lock is released.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time

from .cli_results import read_original


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def enabled(config):
    return isinstance(config.get('event_dispatch'), dict) and config['event_dispatch'].get('enabled') is True


def readiness(config):
    binding = config['event_dispatch']
    path = Path(binding['readiness_manifest'])
    raw = read_original(path)[0]
    if digest(raw) != binding['readiness_revision']:
        raise ValueError('Event readiness manifest changed')
    manifest = json.loads(raw)
    keys = ('worker', 'hostname', 'account_email_sha256', 'model', 'reasoning')
    if (manifest.get('schema') != 1 or config.get('admission') != 'enabled'
            or socket.gethostname() != config.get('hostname')
            or any(manifest.get(k) != config.get(k) for k in keys)
            or not isinstance(manifest.get('files'), dict) or not manifest['files']):
        raise ValueError('Event readiness differs from qualified actor/source')
    for name, expected in manifest['files'].items():
        path = Path(name)
        if path.is_symlink() or path.resolve() != path.absolute() or not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError('Qualified event-dispatch source/instructions changed')
    return binding['readiness_revision']


def admission_inventory(root, worker=None):
    """Reuse the existing terminal-state gate; require the latest actual proof."""
    paths = sorted(root.glob('*/state.json'))
    runs = [(p, json.loads(read_original(p)[0])) for p in paths]
    if worker is not None and any(r.get('worker') != worker for _, r in runs):
        raise ValueError('Foreign local run inventory')
    if any(r.get('status') not in ('completed', 'continued', 'settled') for _, r in runs):
        raise ValueError('Prior local run requires reconciliation')
    runs.sort(key=lambda pair: pair[1]['created_at'])
    proof = None
    if runs:
        directory, latest = runs[-1][0].parent, runs[-1][1]
        for name in ('run-result.json', 'dispatch-settlement.json'):
            path = directory/name
            if path.exists():
                raw = read_original(path)[0]
                value = json.loads(raw)
                if name == 'run-result.json':
                    if (value.get('availability') != 'available' or value.get('execution_stopped') is not True
                            or value.get('run_id') != latest['run_id'] or value.get('task') != latest['task']
                            or value.get('worker') != latest['worker']):
                        raise ValueError('Latest typed result does not establish availability')
                elif (value.get('execution_stopped') is not True or value.get('input_settled') is not True
                      or value.get('unknown_operations', []) != [] or value.get('unsettled_operations', []) != []):
                    raise ValueError('Latest audited local settlement is incomplete')
                proof = {'name': name, 'sha256': digest(raw)}
                break
        if proof is None:
            raise ValueError('Latest local run needs its existing typed result or audited settlement')
    inventory = {'states': {p.parent.name: digest(read_original(p)[0]) for p, _ in runs}, 'latest_proof': proof}
    return (runs[-1][1]['run_id'] if runs else None), digest(json.dumps(inventory, sort_keys=True).encode())


def report_locked(client, config, root, available, issue=None):
    from .cli_dispatch import save
    revision = readiness(config) if available else config['event_dispatch']['readiness_revision']
    latest, proof = admission_inventory(root, config['worker']) if available else (None, digest(b'admission-unavailable'))
    # Administrative sequence writes are serialized independently of runtime
    # ownership, including a refusal reported after the one-shot wake returned.
    with (root/'event-report-sequence.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root/'event-report-sequence.json'
        sequence = json.loads(read_original(path)[0])['sequence'] + 1 if path.exists() else 1
        save(path, {'sequence': sequence})
        return client.request('/api/worker/dispatch/report', {'readiness_revision': revision, 'sequence': sequence,
            'availability': 'available' if available else 'unknown' if available is None else 'busy',
            'latest_run_id': latest, 'settlement_sha256': proof, 'issue': issue})


def reserve_run(client, config, root, run_dir, task, record):
    """Caller holds worker.lock and has passed original account/task checks."""
    if not enabled(config):
        return
    from .cli_dispatch import save
    # Report the prior inventory before publishing this new starting intent.
    report_locked(client, config, root, True)
    revision = readiness(config)
    authorization = {'wake_id': run_dir.name, 'run_id': run_dir.name, 'token': secrets.token_urlsafe(32)}
    save(run_dir/'wake-authorization.json', authorization)
    record.update(wake_id=run_dir.name, dispatch_readiness_revision=revision)
    save(run_dir/'state.json', record)
    response = client.request('/api/worker/dispatch/reserve', {'wake_id': run_dir.name, 'run_id': run_dir.name,
        'task_id': task['id'], 'queued_generation': task['generation'], 'readiness_revision': revision,
        'token_hash': digest(authorization['token'].encode())})
    save(run_dir/'wake-reservation-response.json', response)
    if response.get('duplicate') is not False or response.get('status') != 'reserved':
        raise ValueError('Ambiguous or previously delivered wake; do not launch another model')


def claim_binding(root, run_id):
    """Only the actual CLI thread's existing own_run binding selects this file."""
    if run_id is None:
        return None
    path = root/run_id/'wake-authorization.json'
    if not path.exists():
        return None
    return json.loads(read_original(path)[0])


def supervisor_settled(client, config, run_dir):
    if not enabled(config) or not (run_dir/'wake-authorization.json').exists():
        return
    from .cli_dispatch import save
    state = json.loads(read_original(run_dir/'state.json')[0])
    result_raw = read_original(run_dir/'run-result.json')[0]
    result = json.loads(result_raw)
    if (state.get('status') not in ('completed', 'settled') or state.get('execution_stopped') is not True
            or result.get('availability') != 'available' or result.get('execution_stopped') is not True
            or state.get('unreceived_message_ids', []) != []
            or result.get('run_id') != run_dir.name or result.get('worker') != config['worker']):
        raise ValueError('Exact normally reduced supervisor result required; retain reservation')
    hashes = result.get('evidence_sha256')
    expected = {'finish-response.json', 'worker-report.json', 'task-readback.json'}
    if result.get('outcome') == 'waiting':
        expected.add('wait-receipt.json')
    if (not isinstance(hashes, dict) or set(hashes) != expected
            or any(digest(read_original(run_dir/name)[0]) != value for name, value in hashes.items())):
        raise ValueError('Original supervisor evidence changed; retain reservation')
    # Assert the caller really released the old supervisor lock. This brief
    # check acquires no runtime or UI resource, and the service wake still fences
    # any new model until the following exact settlement is acknowledged.
    with (run_dir.parent/'worker.lock').open('r') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Supervisor worker lock is still held') from None
    authorization = json.loads(read_original(run_dir/'wake-authorization.json')[0])
    report = json.loads(read_original(run_dir/'worker-report.json')[0])
    classification = report.get('failure_classification')
    if classification not in (None, 'harness', 'product'):
        raise ValueError('Unknown explicit failure classification')
    body = {**authorization, 'generation': result['generation'], 'readiness_revision': readiness(config),
            'result_sha256': digest(result_raw), 'outcome': result['outcome'], 'execution_stopped': True,
            'input_settled': True, 'local_lock_released': True, 'unknown_operations': [], 'unsettled_operations': [],
            'unreceived_message_ids': [], 'failure_classification': classification}
    response = client.request('/api/worker/dispatch/settle', body)
    save(run_dir/'event-settlement-response.json', response)


def listen(client, config, root, stop):
    """One subscriber in the existing reporter; blocked waits use no model."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root/'event-listener.lock').open('a+') as listener:
        try:
            fcntl.flock(listener, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        while not stop.is_set():
            try:
                with (root/'worker.lock').open('a+') as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        available = False
                    else:
                        available = True
                    if available:
                        # Active service reservations require supervisor ACK;
                        # a duplicate availability report must not clear them.
                        try:
                            report_locked(client, config, root, True)
                        except Exception:
                            available = False
                candidate = client.request('/api/worker/dispatch/next', {'timeout': 10})
                if candidate.get('task_id') and available:
                    # Existing one-shot entrypoint owns all identity/preflight,
                    # local lock and service reservation checks. No alternate
                    # actor, direct model invocation or prompt substitution.
                    from .cli_dispatch import environment
                    process = subprocess.run([sys.executable, '-m', 'coordinator.cli_dispatch', 'wake',
                        '--task', candidate['task_id'], '--queued-generation', str(candidate['queued_generation']),
                        '--event-triggered'],
                        cwd=str(Path(__file__).resolve().parents[1]), env=environment(),
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                    if process.returncode:
                        # No replay after ambiguous delivery. The original
                        # worker/run journal and service reservation fence it.
                        from .cli_dispatch import save
                        save(root/'event-listener-state.json', {'state': 'needs_attention',
                            'issue': 'wake_refused_or_unknown', 'task_id': candidate['task_id'],
                            'queued_generation': candidate['queued_generation'], 'at': time.time()})
                        # Existing Dispatcher recovery inspects/corrects this
                        # exact boundary; do not retry an actual refusal here.
                        try:
                            report_locked(client, config, root, None, issue='wake_refused_or_unknown')
                        finally:
                            # A failed status upload must not replay the wake.
                            return
                elif candidate.get('task_id'):
                    stop.wait(1)
            except Exception:
                # Reconnect transport only; never reset a run or reservation.
                stop.wait(10)


def start_listener(client):
    from .cli_dispatch import CONFIG, ROOT
    from .workers import MINI_VM_WORKERS
    if not CONFIG.exists():
        return None
    config = json.loads(CONFIG.read_text())
    if (not enabled(config) or client.config.get('worker') != config.get('worker')
            or config.get('worker') not in MINI_VM_WORKERS):
        return None
    stop = threading.Event()
    thread = threading.Thread(target=listen, args=(client, config, ROOT, stop), daemon=True,
                              name='nova-event-dispatch')
    thread.start()
    return stop, thread
