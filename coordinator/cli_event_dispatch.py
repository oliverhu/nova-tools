"""Event transport inside the existing capacity agent; no new polling job.

The normal CLI wake still owns account checks, local worker.lock and exact task
validation. This module supplies the common durable preclaim fence and publishes
the supervisor's typed settlement only after that lock is released.
"""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time

from .cli_results import read_original

PRECLAIM_COMMON = {'state.json', 'worker-report.json', 'task-readback.json', 'tool-discovery.json',
                   'wake-reservation-response.json', 'model-spawn-intent.json', 'events.jsonl'}
PRECLAIM_PROFILES = {'explicit-cleanup': PRECLAIM_COMMON | {'stopped-cleanup.json', 'readiness-check.json'},
                     'noninvocation-marker': PRECLAIM_COMMON | {'finish-response.json', 'readiness.json'},
                     'stopped-readiness-refusal': (PRECLAIM_COMMON - {'tool-discovery.json'}) |
                         {'stopped-cleanup.json', 'readiness-preflight.json', 'actor-binding.json', 'instruction-readback.json'}}


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
    if config.get('session_epoch'):keys += ('session_epoch',)
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


def validate_preclaim_event(config, prior, documents, task, note):
    """Compatibility for a truthful readiness stop, retaining every original.

    This is deliberately narrower than normal result reconciliation: no task
    was ever claimed, no finish exists, and one audited capability refinement
    must be the only change since the worker's retained service readback.
    """
    report, old = (documents[n] for n in ('worker-report.json', 'task-readback.json'))
    profile = note['evidence_profile']
    cleanup = documents['stopped-cleanup.json'] if profile in ('explicit-cleanup', 'stopped-readiness-refusal') else report.get('cleanup', {})
    run_id, worker = prior.get('run_id'), config['worker']
    if (not isinstance(run_id, str) or len(run_id) != 32 or any(c not in '0123456789abcdef' for c in run_id)
            or prior.get('worker') != worker or prior.get('task') != task.get('id')
            or prior.get('status') != 'needs_attention' or prior.get('completion_verified') is not False
            or type(prior.get('cli_exit_code')) is not int or prior['cli_exit_code'] != 0
            or prior.get('unreceived_message_ids') != []
            or any(type(prior.get(k)) is not int or prior[k] <= 0 for k in ('supervisor_pid', 'cli_pid'))
            or any(type(prior.get(k)) not in (int, float) or not math.isfinite(prior[k]) or prior[k] <= 0
                   for k in ('created_at', 'started_at', 'ended_at'))
            or not prior['created_at'] <= prior['started_at'] <= prior['ended_at']
            or type(prior.get('queued_generation')) is not int or prior['queued_generation'] != 0
            or type(task.get('generation')) is not int or task['generation'] != 0 or task.get('step') != 0
            or task.get('status') != 'queued' or task.get('owner') is not None or task.get('last_owner') is not None
            or task.get('requirements', {}).get('worker') != worker
            or task.get('result') != {} or task.get('receipts') != [] or task.get('approval') is not None
            or any(type(task.get(k)) is not int or task[k] != 0
                   for k in ('fix_attempts_used', 'total_fix_attempts', 'attempt_base', 'reassignment_count'))):
        raise ValueError('Ended exact-worker run and unchanged never-owned generation-zero task required')
    for key in ('id', 'title', 'description', 'steps', 'step', 'generation', 'status', 'owner', 'last_owner',
                'dependencies', 'compute', 'priority', 'request_key', 'result', 'receipts', 'approval',
                'fix_attempts_used', 'total_fix_attempts', 'attempt_base', 'reassignment_count'):
        if key not in old or old[key] != task.get(key):
            raise ValueError('Task acceptance, history or constraints changed beyond capability refinement')
    original_events, events = old.get('events', []), task.get('events', [])
    if (not isinstance(original_events, list) or [e.get('kind') for e in original_events] != ['submitted', 'wake-reserved']
            or events[:-1] != original_events or len(events) != 3
            or events[-1].get('kind') != 'task-capabilities-required' or events[-1].get('actor') != 'admin'
            or events[-1].get('id') != note.get('capability_event_id')
            or original_events[-1].get('actor') != worker):
        raise ValueError('Only the exact reservation and one audited capability disposition may follow submission')
    reserved = json.loads(original_events[-1]['detail']); disposition = json.loads(events[-1]['detail'])
    if (reserved.get('wake_id') != run_id or reserved.get('run_id') != run_id
            or reserved.get('task_id') != task['id'] or reserved.get('queued_generation') != 0
            or reserved.get('readiness_revision') != config['event_dispatch']['readiness_revision']
            or disposition.get('request', {}).get('generation') != 0
            or disposition['request'].get('expected_requirements') != old.get('requirements')
            or disposition.get('requirements') != task['requirements']):
        raise ValueError('Reservation or capability disposition does not bind the original task')
    if (report.get('run_id') != run_id or report.get('task') != task['id'] or report.get('worker') != worker
            or type(report.get('generation')) is not int or report['generation'] != 0
            or report.get('outcome') != ('blocked' if profile == 'stopped-readiness-refusal' else 'waiting')
            or report.get('acceptance_complete') is not False
            or report.get('cleanup') != cleanup
            or not isinstance(cleanup.get('evidence'), list) or not cleanup['evidence']
            or not all(isinstance(e, str) and e.strip() for e in cleanup['evidence'])):
        raise ValueError('Original positive no-claim report/cleanup with incomplete acceptance required')
    if profile == 'explicit-cleanup':
        if (type(report.get('queued_generation')) is not int or report['queued_generation'] != 0
                or not isinstance(report.get('summary'), str) or not report['summary'].strip()
                or report.get('claim_attempted') is not False or report.get('service_receipt_submitted') is not False
                or report.get('test_started') is not False or report.get('publication') is not False
                or report.get('execution_stopped') is not True or report.get('external_state_checked') is not True):
            raise ValueError('Explicit-cleanup report lacks positive no-claim/no-receipt facts')
    elif profile == 'stopped-readiness-refusal':
        ready = documents['readiness-preflight.json']; actor = documents['actor-binding.json']
        instructions = documents['instruction-readback.json']
        if (report.get('claim_attempted') is not False or report.get('finish_attempted') is not False
                or report.get('queued_generation') != 0 or report.get('fix_attempts_used') != 0
                or not isinstance(report.get('summary'), str) or not report['summary'].strip()
                or any(ready.get(k) != v for k, v in {'task': task['id'], 'generation': 0,
                    'task_status': 'queued', 'prior_receipts': [], 'claim_attempted': False,
                    'claim_generation': None, 'messages_ledger_exists': False, 'message_count': 0}.items())
                or ready.get('readiness_manifest_sha256') != config['event_dispatch']['readiness_revision']
                or ready.get('readiness_manifest') != config['event_dispatch']['readiness_manifest']
                or any(actor.get(k) != v for k, v in {'run_id': run_id, 'task': task['id'],
                    'queued_generation': 0, 'claimed_generation': None, 'claim_attempted': False}.items())
                or any(actor.get(k) != config.get(k) for k in ('worker', 'account_email_sha256', 'model', 'reasoning'))
                or actor.get('thread_ids') != [actor.get('execution_thread_id')]
                or not isinstance(actor.get('execution_thread_id'), str) or not actor['execution_thread_id'].strip()
                or any(instructions.get(k) != v for k, v in {'schema': 1, 'run_id': run_id,
                    'worker': worker, 'hostname': config['hostname'], 'own_actor_adoption': True}.items())):
            raise ValueError('Original stopped preclaim refusal, readiness and exact actor binding required')
    else:
        marker = documents['finish-response.json']; ready = documents['readiness.json']
        if (report.get('claimed') is not False or report.get('service_status') != 'queued'
                or any(marker.get(k) != v for k, v in {'run_id': run_id, 'task': task['id'], 'generation': 0,
                    'worker': worker, 'operation': 'not_invoked', 'service_response': None,
                    'service_finish_submitted': False, 'service_defer_submitted': False,
                    'observed_status': 'queued'}.items())
                or not isinstance(marker.get('reason'), str) or not marker['reason'].strip()
                or ready.get('claim_allowed') is not False or ready.get('tests_started') is not False
                or ready.get('operations_launched') != []):
            raise ValueError('Actual noninvocation marker and no-operation readiness required')
    for value in (prior, report, cleanup, note):
        if (value.get('human_stop') is True or value.get('approval_request')
                or value.get('unknown_operations', []) != [] or value.get('unsettled_operations', []) != []
                or any(value.get(k) is False for k in ('execution_stopped', 'external_state_checked', 'input_settled'))):
            raise ValueError('Unknown operations and real human/approval holds remain binding')
    if (any(cleanup.get(k) is not True for k in ('execution_stopped', 'external_state_checked', 'input_settled'))
            or cleanup.get('unknown_operations') != [] or cleanup.get('unsettled_operations') != []
            or note.get('run_id') != run_id or note.get('task') != task['id'] or note.get('worker') != worker
            or type(note.get('generation')) is not int or note['generation'] != 0
            or any(note.get(k) is not True for k in ('execution_stopped', 'external_state_checked', 'input_settled'))
            or any(note.get(k) != [] for k in ('unknown_operations', 'unsettled_operations'))
            or note.get('claim_attempted') is not False or note.get('finish_attempted') is not False
            or not isinstance(note.get('evidence'), str) or not note['evidence'].strip()):
        raise ValueError('Positive exact-run cleanup and inspected no-claim/no-finish note required')
    response = documents['wake-reservation-response.json']; intent = documents['model-spawn-intent.json']
    if response != {'wake_id': run_id, 'status': 'reserved', 'duplicate': False} or intent != {'run_id': run_id, 'wake_id': run_id}:
        raise ValueError('Original reservation/model intent differs')


def reconcile_preclaim_event(client, config, root, prior, note_file, lock, check_only=False):
    """Called with worker.lock held; release only after durable positive proof.

    Run from a qualified isolated source when installed source is pinned by an
    active reservation. It does not replace installed files or readiness pins.
    A transport ambiguity reuses the immutable request, never a second model.
    """
    from .cli_dispatch import save
    revision = readiness(config)
    directory = root/prior['run_id']
    request_path = directory/'preclaim-event-request.json'
    audit_path = directory/'preclaim-reconciliation.json'
    backup = directory/'preclaim-state-before.json'
    note_raw = read_original(note_file)[0]; note = json.loads(note_raw)
    selected = PRECLAIM_PROFILES.get(note.get('evidence_profile'))
    if selected is None:
        raise ValueError('An exact supported original-evidence profile is required')
    recovery = request_path.exists()
    if recovery:
        body = json.loads(read_original(request_path)[0]); audit = json.loads(read_original(audit_path)[0])
        original_state = read_original(backup)[0]
        expected_state = {**json.loads(original_state), 'prior_status': 'needs_attention', 'status': 'settled',
                          'execution_stopped': True, 'settlement_outcome': 'unclaimed_stopped'}
        if (audit.get('note_sha256') != digest(note_raw) or prior != expected_state
                or read_original(audit_path)[0] != read_original(directory/'dispatch-settlement.json')[0]
                or body.get('result_sha256') != digest(read_original(directory/'dispatch-settlement.json')[0])
                or body.get('readiness_revision') != revision or body.get('evidence_sha256') != note.get('evidence_sha256')):
            raise ValueError('Prior preclaim reconciliation changed; preserve and inspect')
    else:
        original_state = read_original(directory/'state.json')[0]
        if any((directory/n).exists() or (directory/n).is_symlink() for n in
               (('finish-response.json',) if 'finish-response.json' not in selected else ()) +
               ('wait-receipt.json', 'claim-response.json', 'run-result.json',
                'dispatch-settlement.json', 'preclaim-state-before.json', 'preclaim-reconciliation.json')):
            raise ValueError('Preserve existing finish/claim/result/settlement evidence; this route does not apply')
        if json.loads(original_state) != prior:
            raise ValueError('Preclaim state changed')
    raw = {n: original_state if n == 'state.json' else read_original(directory/n)[0] for n in selected}
    hashes = {n: digest(value) for n, value in raw.items()}
    if hashes != note.get('evidence_sha256'):
        raise ValueError('Original preclaim evidence differs from exact reviewed hashes')
    authorization = json.loads(read_original(directory/'wake-authorization.json')[0])
    if (set(authorization) != {'wake_id', 'run_id', 'token'} or authorization['wake_id'] != prior['run_id']
            or authorization['run_id'] != prior['run_id']):
        raise ValueError('Foreign private wake authorization')
    if recovery:
        if any(body.get(k) != v for k, v in authorization.items()):
            raise ValueError('Immutable recovery request differs from private wake binding')
    else:
        documents = {n: json.loads(value) for n, value in raw.items() if n != 'events.jsonl'}
        task = client.request('/api/tasks/'+prior['task'])
        validate_preclaim_event(config, prior, documents, task, note)
        messages = directory/'messages.json'
        if messages.exists():
            entries = json.loads(read_original(messages)[0]).get('messages')
            if not isinstance(entries, list) or any(m.get('status') != 'readback' for m in entries):
                raise ValueError('Unreceived messages prevent preclaim settlement')
        fresh = client.request('/api/tasks/'+prior['task'])
        validate_preclaim_event(config, prior, documents, fresh, note)
        if any(fresh.get(k) != task.get(k) for k in ('requirements', 'events', 'generation', 'owner', 'status', 'receipts')):
            raise ValueError('Service task changed during preclaim validation')
        if read_original(note_file)[0] != note_raw or any(read_original(directory/n)[0] != value for n, value in raw.items()):
            raise ValueError('Original preclaim evidence changed during validation')
        if check_only:
            return {'validated': True, 'run_id': prior['run_id'], 'service_or_admission_changed': False}
        # Originals remain byte-exact. No finish or passing result is invented.
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(original_state); stream.flush(); os.fsync(stream.fileno())
        proof = {'schema': 1, 'operation': 'preclaim_event_stop', 'run_id': prior['run_id'], 'task': prior['task'],
                 'worker': config['worker'], 'generation': 0, 'outcome': 'unclaimed_stopped', 'acceptance_complete': False,
                 'availability': 'available', 'execution_stopped': True, 'external_state_checked': True,
                 'input_settled': True, 'unknown_operations': [], 'unsettled_operations': [],
                 'evidence_sha256': hashes, 'evidence_profile': note['evidence_profile'],
                 'note_sha256': digest(note_raw), 'capability_event_id': note['capability_event_id']}
        save(audit_path, proof)
        save(directory/'dispatch-settlement.json', proof)
        body = {**authorization, 'generation': 0, 'readiness_revision': revision,
                'result_sha256': digest(read_original(directory/'dispatch-settlement.json')[0]),
                'capability_event_id': note['capability_event_id'], 'evidence_sha256': hashes,
                'evidence_profile': note['evidence_profile'],
                'execution_stopped': True, 'external_state_checked': True, 'input_settled': True,
                'local_lock_released': True, 'unknown_operations': [], 'unsettled_operations': [],
                'unreceived_message_ids': [], 'claim_attempted': False, 'finish_attempted': False}
        save(request_path, body)  # Owner-only journal contains the private token.
        save(directory/'state.json', {**prior, 'prior_status': prior['status'], 'status': 'settled',
             'execution_stopped': True, 'settlement_outcome': 'unclaimed_stopped'})
    if check_only:
        return {'validated': True, 'run_id': prior['run_id'], 'service_or_admission_changed': False}
    fcntl.flock(lock, fcntl.LOCK_UN)
    # Independently verify the old lock really is released before acknowledging.
    with (root/'worker.lock').open('r') as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Worker lock was reacquired; retain reservation') from None
    readiness(config)
    response = client.request('/api/worker/dispatch/preclaim-settle', body)
    if response.get('settled') is not True or response.get('outcome') != 'unclaimed_stopped':
        raise ValueError('Preclaim settlement not acknowledged; retain immutable request')
    save(directory/'event-settlement-response.json', response)
    return {'settled': True, 'run_id': prior['run_id'], 'outcome': 'unclaimed_stopped', 'acceptance_complete': False}


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
                        report_locked(client, config, root, False)
                    else:
                        available = True
                    if available:
                        # Active service reservations require supervisor ACK;
                        # a duplicate availability report must not clear them.
                        try:
                            report_locked(client, config, root, True)
                        except (ValueError, OSError):
                            available = False
                            report_locked(client, config, root, None, issue='local_admission_unresolved')
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
