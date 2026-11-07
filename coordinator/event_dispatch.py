"""Opt-in Mini wake fencing. An ambiguous delivery never expires into a retry.

The authenticated local supervisor reports availability after its existing result
reducer, not after task finish. No process absence, heartbeat or model text can
release an active wake. Reservations are shared by manual and event delivery.
"""
import hashlib
import json
import re
import secrets
import time

from .workers import MINI_VM_WORKERS

SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_workers (
 worker TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
 readiness_revision TEXT NOT NULL, availability TEXT NOT NULL DEFAULT 'unknown',
 task_filter TEXT NOT NULL DEFAULT 'null',
 observed_at REAL NOT NULL DEFAULT 0, sequence INTEGER NOT NULL DEFAULT 0,
 report TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS dispatch_wakes (
 id TEXT PRIMARY KEY, worker TEXT NOT NULL, task_id TEXT NOT NULL,
 queued_generation INTEGER NOT NULL, run_id TEXT NOT NULL UNIQUE,
 readiness_revision TEXT NOT NULL, token_hash TEXT NOT NULL,
 status TEXT NOT NULL, created_at REAL NOT NULL, claimed_at REAL,
 claim_generation INTEGER, first_test_at REAL, settled_at REAL,
 phase TEXT NOT NULL DEFAULT 'admission', phase_at REAL,
 settlement TEXT NOT NULL DEFAULT '{}');
CREATE UNIQUE INDEX IF NOT EXISTS one_active_worker_wake ON dispatch_wakes(worker)
 WHERE status != 'settled';
CREATE UNIQUE INDEX IF NOT EXISTS one_active_task_wake ON dispatch_wakes(task_id)
 WHERE status != 'settled';
"""


def require(value, message, conflict=False):
    if not value:
        from .store import Conflict, Invalid
        raise (Conflict if conflict else Invalid)(message)


def hexid(value, length):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % length, value) is not None


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def active(db, worker):
    return db.execute("SELECT * FROM dispatch_wakes WHERE worker=? AND status!='settled'", (worker,)).fetchone()


def require_capabilities(store, actor, task_id, body):
    """Refine declared acceptance into scheduler requirements; never grant access.

    Both ordinary claims and event admission already use Store.fits. Additive
    refinement therefore also covers the existing Dispatcher fallback.
    """
    keys = {'request_id', 'generation', 'expected_requirements', 'capabilities',
            'execution_stopped', 'external_state_checked', 'evidence'}
    require(actor == 'admin', 'Administrator required')
    require(set(body) == keys and hexid(body['request_id'], 32)
            and type(body['generation']) is int and body['generation'] >= 0
            and isinstance(body['expected_requirements'], dict)
            and type(body['capabilities']) is list and 0 < len(body['capabilities']) <= 20
            and all(isinstance(c, str) and re.fullmatch('[a-z][a-z0-9-]{0,78}', c) for c in body['capabilities'])
            and len(set(body['capabilities'])) == len(body['capabilities'])
            and body['execution_stopped'] is True and body['external_state_checked'] is True
            and isinstance(body['evidence'], str) and 0 < len(body['evidence'].strip()) <= 2000,
            'Exact generation, original requirements, additive capabilities and checked/stopped evidence required')
    with store.transaction() as db:
        prior = db.execute("SELECT * FROM events WHERE task_id=? AND kind='task-capabilities-required' ORDER BY id",
                           (task_id,)).fetchall()
        for event in prior:
            value = json.loads(event['detail'])
            if value['request']['request_id'] == body['request_id']:
                require(value['request'] == body, 'Conflicting capability refinement request', True)
                return {'event_id': event['id'], 'requirements': value['requirements'], 'duplicate': True}
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        require(row is not None, 'Unknown task')
        require(row['generation'] == body['generation'] and row['status'] in ('queued', 'blocked')
                and json.loads(row['requirements']) == body['expected_requirements']
                and (row['status'] != 'blocked' or json.loads(row['result']).get('execution_stopped') is True)
                and not db.execute("SELECT id FROM dispatch_wakes WHERE task_id=? AND status='claimed'", (task_id,)).fetchone(),
                'Task changed, owns execution or retains an unsettled claimed wake', True)
        requirements = {**body['expected_requirements'], 'capabilities': sorted(
            set(body['expected_requirements'].get('capabilities', [])) | set(body['capabilities']))}
        require(len(requirements['capabilities']) <= 20, 'Too many required capabilities')
        db.execute('UPDATE tasks SET requirements=?,updated=? WHERE id=?',
                   (encoded(requirements), store.clock(), task_id))
        store.event(db, actor, 'task-capabilities-required', task_id,
                    encoded({'request': body, 'requirements': requirements}))
        return {'event_id': db.execute('SELECT last_insert_rowid()').fetchone()[0],
                'requirements': requirements, 'duplicate': False}


def configure(store, actor, worker, body):
    require(actor == 'admin' and worker in MINI_VM_WORKERS, 'Administrator and enrolled Mini required')
    require(set(body) in ({'enabled', 'readiness_revision', 'evidence'},
                         {'enabled', 'readiness_revision', 'evidence', 'task_ids'})
            and type(body['enabled']) is bool and hexid(body['readiness_revision'], 64)
            and isinstance(body['evidence'], str) and 0 < len(body['evidence']) <= 2000,
            'Explicit enabled state, qualified readiness revision and bounded evidence required')
    task_ids = body.get('task_ids')
    require(task_ids is None or (type(task_ids) is list and len(task_ids) <= 32
            and all(hexid(t, 16) for t in task_ids) and len(set(task_ids)) == len(task_ids)),
            'Pilot task filter must contain unique exact task IDs')
    with store.transaction() as db:
        from .resource_scope import active as scoped_active
        require(not scoped_active(db,worker), 'Preserve active resource-scoped ownership', True)
        require(not active(db, worker), 'Settle the active wake before changing dispatch configuration', True)
        require(not db.execute("SELECT id FROM tasks WHERE owner=? AND status IN ('running','held')", (worker,)).fetchone(),
                'Preserve active ownership during enrollment', True)
        require(task_ids is None or all(db.execute('SELECT id FROM tasks WHERE id=?', (t,)).fetchone() for t in task_ids),
                'Pilot task filter contains an unknown task')
        db.execute("INSERT INTO dispatch_workers(worker,enabled,readiness_revision,task_filter) VALUES(?,?,?,?) "
                   "ON CONFLICT(worker) DO UPDATE SET enabled=excluded.enabled,readiness_revision=excluded.readiness_revision,"
                   "task_filter=excluded.task_filter,availability='unknown',observed_at=0,sequence=0,report='{}'",
                   (worker, int(body['enabled']), body['readiness_revision'], encoded(task_ids)))
        store.event(db, actor, 'dispatch-configured', None, encoded({'worker': worker, **body}))
        return {'worker': worker, 'enabled': body['enabled'], 'readiness_revision': body['readiness_revision']}


def report(store, worker, body):
    require(worker in MINI_VM_WORKERS, 'Enrolled Mini supervisor required')
    require(set(body) == {'readiness_revision', 'sequence', 'availability', 'latest_run_id', 'settlement_sha256', 'issue'}
            and hexid(body['readiness_revision'], 64) and type(body['sequence']) is int and 0 < body['sequence'] <= 10**12
            and body['availability'] in ('available', 'busy', 'unknown')
            and body['issue'] in (None, 'wake_refused_or_unknown', 'local_admission_unresolved')
            and (body['availability'] != 'available' or body['issue'] is None)
            and (body['latest_run_id'] is None or hexid(body['latest_run_id'], 32))
            and hexid(body['settlement_sha256'], 64), 'Exact typed local admission report required')
    with store.transaction() as db:
        config = db.execute('SELECT * FROM dispatch_workers WHERE worker=?', (worker,)).fetchone()
        require(config and config['enabled'] and config['readiness_revision'] == body['readiness_revision'],
                'Dispatch is disabled or readiness revision differs', True)
        require(body['sequence'] >= config['sequence'], 'Stale local report sequence', True)
        if body['sequence'] == config['sequence']:
            require(config['report'] == encoded(body), 'Conflicting duplicate local report', True)
            return {'accepted': True, 'duplicate': True}
        wake = active(db, worker)
        from .resource_scope import active as scoped_active
        require(not scoped_active(db,worker) or body['availability']!='available','Resource-scoped ownership cannot become generic available',True)
        require(not wake or body['availability'] != 'available', 'Only exact supervisor settlement can release an active wake', True)
        db.execute('UPDATE dispatch_workers SET availability=?,observed_at=?,sequence=?,report=? WHERE worker=?',
                   (body['availability'], store.clock(), body['sequence'], encoded(body), worker))
        return {'accepted': True, 'duplicate': False}


def candidate(store, db, worker, task_id=None):
    from .resource_scope import active as scoped_active
    if scoped_active(db,worker):return None
    config = db.execute('SELECT * FROM dispatch_workers WHERE worker=?', (worker,)).fetchone()
    if (not config or not config['enabled'] or config['availability'] != 'available'
            or config['observed_at'] <= store.clock() - 90 or active(db, worker)):
        return None
    own = db.execute('SELECT * FROM workers WHERE id=?', (worker,)).fetchone()
    if (not own or own['paused'] or own['seen'] <= store.clock() - 90
            or db.execute("SELECT id FROM tasks WHERE owner=? AND status IN ('running','held')", (worker,)).fetchone()):
        return None
    rows = db.execute("SELECT * FROM tasks WHERE status IN ('queued','blocked') ORDER BY priority DESC,created,id").fetchall()
    for row in rows:
        task_filter = json.loads(config['task_filter'])
        if task_filter is not None and row['id'] not in task_filter:
            continue
        if task_id is not None and row['id'] != task_id:
            continue
        if row['task_type'] == 'publish' or row['compute'] == 'heavy':
            continue
        if db.execute("SELECT id FROM dispatch_wakes WHERE task_id=? AND status!='settled'", (row['id'],)).fetchone():
            continue
        if db.execute("SELECT id FROM resource_scope_runs WHERE task_id=? AND status IN ('reserved','claimed')",(row['id'],)).fetchone():
            continue
        scheduling = store.scheduling(db, row)
        if (store.fits(row, own) and store.assignment_allowed(row, worker)
                and scheduling['ready'] and store.candidate(db, row) == worker):
            return row
    return None


def next_candidate(store, worker, timeout=0):
    require(worker in MINI_VM_WORKERS and type(timeout) in (int, float) and 0 <= timeout <= 10,
            'Enrolled Mini and bounded event wait required')
    # Hold the condition across the check and wait to avoid lost commit signals.
    deadline = time.monotonic() + timeout
    with store.dispatch_changed:
        while True:
            with store.connection() as db:
                row = candidate(store, db, worker)
                if row:
                    return {'task_id': row['id'], 'queued_generation': row['generation']}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {'task_id': None}
            store.dispatch_changed.wait(remaining)


def reserve(store, worker, body):
    keys = {'wake_id', 'run_id', 'task_id', 'queued_generation', 'readiness_revision', 'token_hash'}
    require(worker in MINI_VM_WORKERS and set(body) == keys
            and all(hexid(body[k], n) for k, n in [('wake_id', 32), ('run_id', 32), ('task_id', 16),
                                                  ('readiness_revision', 64), ('token_hash', 64)])
            and type(body['queued_generation']) is int and body['queued_generation'] >= 0,
            'Exact run, task generation, readiness and token binding required')
    with store.transaction() as db:
        existing = db.execute('SELECT * FROM dispatch_wakes WHERE id=?', (body['wake_id'],)).fetchone()
        if existing:
            require(existing['worker'] == worker and all(existing[k] == body[k] for k in keys - {'wake_id'}),
                    'Wake ID reused with another binding', True)
            return {'wake_id': existing['id'], 'status': existing['status'], 'duplicate': True}
        row = candidate(store, db, worker, body['task_id'])
        config = db.execute('SELECT * FROM dispatch_workers WHERE worker=?', (worker,)).fetchone()
        require(row is not None and row['generation'] == body['queued_generation']
                and config['readiness_revision'] == body['readiness_revision'], 'Worker/task is no longer eligible', True)
        db.execute('INSERT INTO dispatch_wakes(id,worker,task_id,queued_generation,run_id,readiness_revision,token_hash,status,created_at) '
                   "VALUES(?,?,?,?,?,?,?,'reserved',?)", (body['wake_id'], worker, row['id'], row['generation'],
                   body['run_id'], body['readiness_revision'], body['token_hash'], store.clock()))
        db.execute("UPDATE dispatch_workers SET availability='busy' WHERE worker=?", (worker,))
        store.event(db, worker, 'wake-reserved', row['id'], encoded({k: v for k, v in body.items() if k != 'token_hash'}))
        return {'wake_id': body['wake_id'], 'status': 'reserved', 'duplicate': False}


def claim_allowed(db, row, worker, binding, store=None):
    if isinstance(binding, dict) and binding.get('lane') == 'resource-scoped':
        from . import resource_scope
        return store is not None and resource_scope.claim_allowed(store, db, row, worker, binding)
    if db.execute("SELECT id FROM resource_scope_runs WHERE task_id=? AND status IN ('reserved','claimed')",(row['id'],)).fetchone():
        return False  # A normal/cross-worker claim cannot bypass a scoped reservation.
    wake = db.execute("SELECT * FROM dispatch_wakes WHERE task_id=? AND status!='settled'", (row['id'],)).fetchone()
    config = db.execute('SELECT * FROM dispatch_workers WHERE worker=?', (worker,)).fetchone()
    if not wake:
        return not (config and config['enabled'])
    return bool(isinstance(binding, dict) and set(binding) == {'wake_id', 'run_id', 'token'}
                and isinstance(binding['token'], str) and 32 <= len(binding['token']) <= 128
                and wake['worker'] == worker and wake['status'] == 'reserved'
                and wake['id'] == binding['wake_id'] and wake['run_id'] == binding['run_id']
                and wake['queued_generation'] == row['generation'] and config and config['enabled']
                and config['readiness_revision'] == wake['readiness_revision']
                and secrets.compare_digest(wake['token_hash'], hashlib.sha256(binding['token'].encode()).hexdigest()))


def claimed(store, db, row, worker, generation):
    db.execute("UPDATE dispatch_wakes SET status='claimed',phase='claimed',phase_at=?,claimed_at=?,claim_generation=? "
               "WHERE worker=? AND task_id=? AND status='reserved'", (store.clock(), store.clock(), generation, worker, row['id']))


def settle(store, worker, body):
    from .resource_scope import active as scoped_active
    keys = {'wake_id', 'run_id', 'generation', 'token', 'result_sha256', 'outcome', 'execution_stopped',
            'input_settled', 'unknown_operations', 'unsettled_operations', 'unreceived_message_ids',
            'local_lock_released', 'readiness_revision', 'failure_classification'}
    require(set(body) == keys and hexid(body['wake_id'], 32) and hexid(body['run_id'], 32)
            and type(body['generation']) is int and isinstance(body['token'], str) and hexid(body['result_sha256'], 64)
            and body['outcome'] in ('completed', 'waiting', 'blocked', 'pending_external_approval')
            and body['failure_classification'] in (None, 'harness', 'product')
            and body['execution_stopped'] is True and body['input_settled'] is True
            and body['local_lock_released'] is True and hexid(body['readiness_revision'], 64)
            and all(body[k] == [] for k in ('unknown_operations', 'unsettled_operations', 'unreceived_message_ids')),
            'Exact positive supervisor result and closed message/operation inventories required')
    with store.transaction() as db:
        require(not scoped_active(db,worker),'Preserve active resource-scoped runtime',True)
        wake = db.execute('SELECT * FROM dispatch_wakes WHERE id=?', (body['wake_id'],)).fetchone()
        require(wake and wake['worker'] == worker and wake['run_id'] == body['run_id']
                and wake['readiness_revision'] == body['readiness_revision']
                and secrets.compare_digest(wake['token_hash'], hashlib.sha256(body['token'].encode()).hexdigest()),
                'Foreign wake settlement', True)
        public = {k: v for k, v in body.items() if k != 'token'}
        if wake['status'] == 'settled':
            require(wake['settlement'] == encoded(public), 'Conflicting settled result', True)
            return {'settled': True, 'duplicate': True}
        row = db.execute('SELECT * FROM tasks WHERE id=?', (wake['task_id'],)).fetchone()
        expected = 'queued' if body['outcome'] == 'waiting' else body['outcome']
        require(wake['status'] == 'claimed' and body['generation'] == wake['claim_generation'] == row['generation']
                and row['status'] == expected and (row['owner'] == worker or row['last_owner'] == worker),
                'Task outcome/generation changed or no exact claim occurred', True)
        db.execute("UPDATE dispatch_wakes SET status='settled',phase='settled',phase_at=?,settled_at=?,settlement=? WHERE id=?",
                   (store.clock(), store.clock(), encoded(public), wake['id']))
        # The ACK is produced after the exact supervisor result and worker-lock
        # release, with readiness rechecked. Task finish never enters this path.
        db.execute("UPDATE dispatch_workers SET availability='available',observed_at=? WHERE worker=?", (store.clock(), worker))
        store.event(db, worker, 'wake-settled', row['id'], encoded(public))
        return {'settled': True, 'duplicate': False}


PRECLAIM_COMMON = {'state.json', 'worker-report.json', 'task-readback.json', 'tool-discovery.json',
                   'wake-reservation-response.json', 'model-spawn-intent.json', 'events.jsonl'}
PRECLAIM_PROFILES = {'explicit-cleanup': PRECLAIM_COMMON | {'stopped-cleanup.json', 'readiness-check.json'},
                     'noninvocation-marker': PRECLAIM_COMMON | {'finish-response.json', 'readiness.json'},
                     'stopped-readiness-refusal': (PRECLAIM_COMMON - {'tool-discovery.json'}) |
                         {'stopped-cleanup.json', 'readiness-preflight.json', 'actor-binding.json', 'instruction-readback.json'}}


def settle_preclaim(store, worker, body):
    """An ended, never-claimed reservation is distinct from an owning finish.

    The supervisor validates the immutable originals locally; this transaction
    independently fences actual service history and the audited disposition.
    Nothing expires, claims a task, fabricates a finish or consumes an attempt.
    """
    keys = {'wake_id', 'run_id', 'token', 'generation', 'readiness_revision', 'result_sha256',
            'capability_event_id', 'evidence_sha256', 'evidence_profile', 'execution_stopped', 'external_state_checked',
            'input_settled', 'local_lock_released', 'unknown_operations', 'unsettled_operations',
            'unreceived_message_ids', 'claim_attempted', 'finish_attempted'}
    require(worker in MINI_VM_WORKERS and set(body) == keys
            and hexid(body['wake_id'], 32) and hexid(body['run_id'], 32)
            and isinstance(body['token'], str) and 32 <= len(body['token']) <= 128
            and type(body['generation']) is int and body['generation'] == 0
            and type(body['capability_event_id']) is int and body['capability_event_id'] > 0
            and hexid(body['readiness_revision'], 64) and hexid(body['result_sha256'], 64)
            and isinstance(body['evidence_profile'], str) and body['evidence_profile'] in PRECLAIM_PROFILES
            and isinstance(body['evidence_sha256'], dict)
            and set(body['evidence_sha256']) == PRECLAIM_PROFILES[body['evidence_profile']]
            and all(hexid(v, 64) for v in body['evidence_sha256'].values())
            and all(body[k] is True for k in ('execution_stopped', 'external_state_checked', 'input_settled', 'local_lock_released'))
            and all(body[k] == [] for k in ('unknown_operations', 'unsettled_operations', 'unreceived_message_ids'))
            and body['claim_attempted'] is False and body['finish_attempted'] is False,
            'Exact positive no-claim supervisor settlement and immutable original evidence required')
    public = {**{k: v for k, v in body.items() if k != 'token'}, 'outcome': 'unclaimed_stopped',
              'acceptance_complete': False}
    with store.transaction() as db:
        wake = db.execute('SELECT * FROM dispatch_wakes WHERE id=?', (body['wake_id'],)).fetchone()
        config = db.execute('SELECT * FROM dispatch_workers WHERE worker=?', (worker,)).fetchone()
        require(wake and wake['worker'] == worker and wake['run_id'] == body['run_id']
                and wake['readiness_revision'] == body['readiness_revision']
                and secrets.compare_digest(wake['token_hash'], hashlib.sha256(body['token'].encode()).hexdigest()),
                'Foreign preclaim settlement', True)
        if wake['status'] == 'settled':
            require(wake['settlement'] == encoded(public), 'Conflicting preclaim settlement', True)
            return {'settled': True, 'duplicate': True, 'outcome': 'unclaimed_stopped'}
        row = db.execute('SELECT * FROM tasks WHERE id=?', (wake['task_id'],)).fetchone()
        require(wake['status'] == 'reserved' and wake['queued_generation'] == 0 and wake['claim_generation'] is None
                and config and config['enabled'] and config['readiness_revision'] == body['readiness_revision']
                and row['generation'] == 0 and row['step'] == 0 and row['status'] == 'queued'
                and row['owner'] is None and row['last_owner'] is None and row['claim_hash'] is None
                and json.loads(row['requirements']).get('worker') == worker and json.loads(row['result']) == {}
                and all(row[k] == 0 for k in ('fix_attempts_used', 'total_fix_attempts', 'attempt_base', 'reassignment_count'))
                and not db.execute('SELECT task_id FROM receipts WHERE task_id=?', (row['id'],)).fetchone()
                and store.approval_state(db, row) is None,
                'Only an exact never-owned generation-zero reservation may settle without a finish', True)
        events = db.execute('SELECT * FROM events WHERE task_id=? ORDER BY id', (row['id'],)).fetchall()
        require([e['kind'] for e in events] == ['submitted', 'wake-reserved', 'task-capabilities-required']
                and events[1]['actor'] == worker and json.loads(events[1]['detail']).get('wake_id') == wake['id']
                and events[2]['actor'] == 'admin' and events[2]['id'] == body['capability_event_id'],
                'Unexpected service history or missing exact audited capability disposition', True)
        disposition = json.loads(events[2]['detail'])
        require(disposition['request']['generation'] == 0
                and disposition['requirements'] == json.loads(row['requirements']),
                'Capability disposition no longer matches this task', True)
        db.execute("UPDATE dispatch_wakes SET status='settled',phase='preclaim-stopped',phase_at=?,settled_at=?,settlement=? WHERE id=?",
                   (store.clock(), store.clock(), encoded(public), wake['id']))
        db.execute("UPDATE dispatch_workers SET availability='available',observed_at=? WHERE worker=?", (store.clock(), worker))
        store.event(db, worker, 'wake-preclaim-settled', row['id'], encoded(public))
        return {'settled': True, 'duplicate': False, 'outcome': 'unclaimed_stopped'}


def projection(store, db, worker):
    config = db.execute('SELECT * FROM dispatch_workers WHERE worker=?', (worker,)).fetchone()
    if not config:
        return None
    wake = db.execute('SELECT * FROM dispatch_wakes WHERE worker=? ORDER BY created_at DESC,id DESC LIMIT 1', (worker,)).fetchone()
    result = {'enabled': bool(config['enabled']), 'availability': config['availability'],
              'task_ids': json.loads(config['task_filter']),
              'updated_at': config['observed_at'], 'stale': config['observed_at'] <= store.clock() - 90,
              'latest_run': None}
    result['issue'] = json.loads(config['report']).get('issue')
    if result['stale']:
        result['availability'] = 'unknown'
    settlements = [json.loads(r[0]) for r in db.execute(
        "SELECT settlement FROM dispatch_wakes WHERE worker=? AND status='settled'", (worker,))]
    classified = [s for s in settlements if s.get('failure_classification') is not None]
    result['metrics'] = {'classified_failure_samples': len(classified),
        'harness_failure_count': sum(s['failure_classification'] == 'harness' for s in classified) if classified else None,
        'manual_approval_count': db.execute("SELECT COUNT(*) FROM events WHERE kind='approval-applied' AND task_id IN "
            "(SELECT task_id FROM dispatch_wakes WHERE worker=?)", (worker,)).fetchone()[0]}
    result['metrics']['manual_intervention_count'] = db.execute(
        "SELECT COUNT(*) FROM events WHERE kind IN ('approval-applied','reconciled','outcome-corrected','attempts-corrected',"
        "'task-capabilities-required','wake-preclaim-settled') "
        "AND task_id IN (SELECT task_id FROM dispatch_wakes WHERE worker=?)", (worker,)).fetchone()[0]
    result['metrics']['manual_intervention_scope'] = 'Audited approvals, outcome/attempt corrections, capability refinement and preclaim reconciliation only'
    result['metrics']['preclaim_stop_count'] = sum(s.get('outcome') == 'unclaimed_stopped' for s in settlements)
    if wake:
        task = db.execute('SELECT created,status,progress FROM tasks WHERE id=?', (wake['task_id'],)).fetchone()
        result['latest_run'] = {k: wake[k] for k in ('run_id', 'task_id', 'queued_generation', 'claim_generation',
                              'status', 'phase', 'phase_at', 'created_at', 'claimed_at', 'first_test_at', 'settled_at')}
        result['latest_run'].update(elapsed_seconds=max(0, (wake['settled_at'] or store.clock()) - wake['created_at']),
            queue_delay_seconds=wake['claimed_at'] - task['created'] if wake['claimed_at'] is not None else None,
            time_to_first_test_seconds=wake['first_test_at'] - wake['claimed_at'] if wake['first_test_at'] is not None else None,
            latest_progress=task['progress'])
        result['latest_run'].update(
            task_status=task['status'],
            task_outcome=json.loads(wake['settlement']).get('outcome') if wake['status'] == 'settled' else None,
            next_action='Waiting for worker to claim this task' if wake['status'] == 'reserved' else
                'Owner continues actual task' if wake['status'] == 'claimed' else 'Select next eligible task',
            blocker='Worker report is stale; execution state unknown' if result['stale'] else result['issue'],
            artifact_link='/api/tasks/' + wake['task_id'])
    return result
