"""Authenticated app sessions and durable service handoffs.

Enrollment is disabled by default. Worker credentials authenticate machines;
separate session tokens fence chats on the same machine. Qualification records
observed access, never grants OS permissions or settles another execution.
"""
import hashlib
import json
import math
import re
import secrets

SCHEMA = '''
CREATE TABLE IF NOT EXISTS app_worker_policy (
 worker TEXT PRIMARY KEY, revision INTEGER NOT NULL, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_worker_sessions (
 worker TEXT PRIMARY KEY, epoch INTEGER NOT NULL, session_id TEXT NOT NULL,
 token_hash TEXT NOT NULL, registration TEXT NOT NULL, adopted TEXT,
 qualification TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS app_task_sessions (
 task_id TEXT NOT NULL, generation INTEGER NOT NULL, worker TEXT NOT NULL,
 epoch INTEGER NOT NULL, session_id TEXT NOT NULL, PRIMARY KEY(task_id,generation));
CREATE TABLE IF NOT EXISTS app_inbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
 sender TEXT NOT NULL, recipient TEXT NOT NULL, document TEXT NOT NULL,
 created REAL NOT NULL, UNIQUE(sender,request_id));
'''


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def need(condition, message, conflict=False):
    from .store import Conflict, Invalid
    if not condition:
        raise (Conflict if conflict else Invalid)(message)


def text(value, limit=240):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def hexid(value, length=64):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % length, value) is not None


def no_execution(db, worker):
    need(not db.execute("SELECT 1 FROM tasks WHERE owner=? AND status IN ('running','held')", (worker,)).fetchone(),
         'Preserve active worker execution', True)
    from .event_dispatch import active as active_dispatch
    need(not active_dispatch(db, worker),
         'Settle or audit-abandon the original dispatch admission first', True)
    from .resource_scope import active
    need(not active(db, worker), 'Preserve active resource-scoped execution', True)


def configure(store, actor, worker, body):
    from .workers import WORKERS
    need(actor == 'admin' and worker in WORKERS, 'Administrator and enrolled worker required')
    need(set(body) == {'expected_revision', 'enabled', 'source_commit', 'inventory_sha256', 'evidence'}
         and type(body['expected_revision']) is int and body['expected_revision'] >= 0
         and type(body['enabled']) is bool and hexid(body['source_commit'], 40)
         and hexid(body['inventory_sha256']) and text(body['evidence'], 4000),
         'Exact approved instruction revision, inventory and evidence required')
    with store.transaction() as db:
        old = db.execute('SELECT * FROM app_worker_policy WHERE worker=?', (worker,)).fetchone()
        revision = old['revision'] if old else 0
        # Identical retry after a lost response is safe; changed contents conflict.
        if old and old['document'] == encoded(body) and revision == body['expected_revision'] + 1:
            return {'worker': worker, 'revision': revision, 'duplicate': True}
        need(revision == body['expected_revision'], 'Worker policy revision changed', True)
        no_execution(db, worker)
        if body['enabled']:
            dispatch = db.execute('SELECT enabled FROM dispatch_workers WHERE worker=?', (worker,)).fetchone()
            need(not dispatch or not dispatch['enabled'], 'Disable settled CLI dispatch before app enrollment', True)
        revision += 1
        db.execute('INSERT INTO app_worker_policy VALUES(?,?,?) ON CONFLICT(worker) DO UPDATE SET '
                   'revision=excluded.revision,document=excluded.document', (worker, revision, encoded(body)))
        store.event(db, actor, 'app-policy-updated', None, encoded({'worker': worker, 'revision': revision, 'policy': body}))
        return {'worker': worker, 'revision': revision, 'duplicate': False}


def policy(db, worker):
    row = db.execute('SELECT * FROM app_worker_policy WHERE worker=?', (worker,)).fetchone()
    return {'revision': row['revision'], **json.loads(row['document'])} if row else None


def authenticate(db, worker, binding):
    need(isinstance(binding, dict) and set(binding) == {'session_id', 'epoch', 'token'}
         and text(binding['session_id']) and type(binding['epoch']) is int and text(binding['token'], 256),
         'Exact app session binding required', True)
    row = db.execute('SELECT * FROM app_worker_sessions WHERE worker=?', (worker,)).fetchone()
    need(row and row['session_id'] == binding['session_id'] and row['epoch'] == binding['epoch']
         and secrets.compare_digest(row['token_hash'], hashlib.sha256(binding['token'].encode()).hexdigest()),
         'Stale or foreign app session', True)
    return row


def register(store, worker, body):
    from .workers import WORKERS
    need(worker in WORKERS, 'Enrolled worker credential required')
    keys = {'session_id', 'expected_epoch', 'host_id', 'account_sha256', 'session_token'}
    need(set(body) == keys and text(body['session_id']) and text(body['host_id'])
         and type(body['expected_epoch']) is int and body['expected_epoch'] >= 0
         and (body['account_sha256'] is None or hexid(body['account_sha256'])) and isinstance(body['session_token'], str)
         and 32 <= len(body['session_token']) <= 256,
         'Distinct session, host, account digest, epoch CAS and private random token required')
    public = {k: v for k, v in body.items() if k != 'session_token'}
    digest = hashlib.sha256(body['session_token'].encode()).hexdigest()
    with store.transaction() as db:
        current_policy = policy(db, worker)
        need(current_policy and current_policy['enabled'], 'App worker policy is not enabled', True)
        old = db.execute('SELECT * FROM app_worker_sessions WHERE worker=?', (worker,)).fetchone()
        if old and old['session_id'] == body['session_id']:
            need(old['registration'] == encoded(public) and secrets.compare_digest(old['token_hash'], digest),
                 'Conflicting session registration retry', True)
            return {'worker': worker, 'session_id': old['session_id'], 'epoch': old['epoch'], 'duplicate': True}
        need((old['epoch'] if old else 0) == body['expected_epoch'], 'Session epoch changed', True)
        no_execution(db, worker)
        epoch = body['expected_epoch'] + 1
        db.execute('INSERT INTO app_worker_sessions VALUES(?,?,?,?,?,NULL,NULL,?) '
                   'ON CONFLICT(worker) DO UPDATE SET epoch=excluded.epoch,session_id=excluded.session_id,'
                   'token_hash=excluded.token_hash,registration=excluded.registration,adopted=NULL,'
                   'qualification=NULL,created=excluded.created',
                   (worker, epoch, body['session_id'], digest, encoded(public), store.clock()))
        store.event(db, worker, 'app-session-registered', None, encoded({'epoch': epoch, **public}))
        return {'worker': worker, 'session_id': body['session_id'], 'epoch': epoch, 'duplicate': False}


def adopt(store, worker, body):
    need(set(body) == {'binding', 'policy_revision', 'source_commit', 'inventory_sha256', 'instructions_read'}
         and body['instructions_read'] is True, 'Verified installed inventory and actual instruction readback required')
    with store.transaction() as db:
        session = authenticate(db, worker, body['binding']); current = policy(db, worker)
        need(current and current['enabled'] and body['policy_revision'] == current['revision']
             and body['source_commit'] == current['source_commit']
             and body['inventory_sha256'] == current['inventory_sha256'], 'Approved instruction policy changed', True)
        value = {k: v for k, v in body.items() if k != 'binding'}
        db.execute('UPDATE app_worker_sessions SET adopted=? WHERE worker=?', (encoded(value), worker))
        store.event(db, worker, 'app-instructions-adopted', None,
                    encoded({'session_id': session['session_id'], 'epoch': session['epoch'], **value}))
        return {'adopted': True, 'policy_revision': current['revision']}


def qualify(store, actor, worker, body):
    need(actor == 'admin', 'Administrator qualification required')
    need(set(body) == {'session_id', 'epoch', 'policy_revision', 'capabilities', 'valid_until', 'evidence_sha256'}
         and text(body['session_id']) and type(body['epoch']) is int
         and type(body['policy_revision']) is int and type(body['capabilities']) is list
         and 0 < len(body['capabilities']) <= 64
         and all(text(x, 100) for x in body['capabilities'])
         and len(set(body['capabilities'])) == len(body['capabilities'])
         and type(body['valid_until']) in (int, float) and math.isfinite(body['valid_until'])
         and store.clock() < body['valid_until'] <= store.clock() + 86400
         and hexid(body['evidence_sha256']), 'Exact session, bounded capability qualification and evidence required')
    with store.transaction() as db:
        no_execution(db, worker)
        row = db.execute('SELECT * FROM app_worker_sessions WHERE worker=?', (worker,)).fetchone()
        current = policy(db, worker)
        need(row and row['session_id'] == body['session_id'] and row['epoch'] == body['epoch']
             and current and current['enabled'] and current['revision'] == body['policy_revision'],
             'Session or policy changed', True)
        db.execute('UPDATE app_worker_sessions SET qualification=? WHERE worker=?', (encoded(body), worker))
        store.event(db, actor, 'app-session-qualified', None, encoded({'worker': worker, **body}))
        return {'qualified': True, 'session_id': row['session_id'], 'epoch': row['epoch']}


def readiness_reason(store, db, worker, task=None):
    current = policy(db, worker)
    if not current: return ''
    if not current['enabled']: return 'App worker is disabled'
    row = db.execute('SELECT * FROM app_worker_sessions WHERE worker=?', (worker,)).fetchone()
    if not row: return 'App session is not enrolled'
    adopted = json.loads(row['adopted']) if row['adopted'] else {}
    qualification = json.loads(row['qualification']) if row['qualification'] else {}
    if (adopted.get('policy_revision') != current['revision']
            or adopted.get('source_commit') != current['source_commit']
            or adopted.get('inventory_sha256') != current['inventory_sha256']):
        return 'Current instruction adoption required'
    if (qualification.get('policy_revision') != current['revision']
            or qualification.get('valid_until', 0) <= store.clock()):
        return 'Current app readiness qualification required'
    required = {'development'}
    if task is not None:
        required.update(json.loads(task['requirements']).get('capabilities', []))
    if not required <= set(qualification['capabilities']):
        return 'App session lacks required qualified capability'
    return ''


def ready(store, db, worker, binding, task=None):
    current = policy(db, worker)
    if not current:
        need(binding is None, 'App session is not enrolled', True)
        return None
    row = authenticate(db, worker, binding)
    reason = readiness_reason(store, db, worker, task)
    need(not reason, reason, True)
    return row


def claimed(db, task, generation, worker, session):
    if session:
        db.execute('INSERT INTO app_task_sessions VALUES(?,?,?,?,?)',
                   (task['id'], generation, worker, session['epoch'], session['session_id']))


def guard(store, db, worker, task, binding, cleanup=False):
    record = db.execute('SELECT * FROM app_task_sessions WHERE task_id=? AND generation=?',
                        (task['id'], task['generation'])).fetchone()
    if not record:
        # Legacy owned work can settle; app enrollment cannot replace active work.
        need(binding is None, 'Claim does not belong to an app session', True)
        return
    session = authenticate(db, worker, binding)
    need(record['worker'] == worker and record['epoch'] == session['epoch']
         and record['session_id'] == session['session_id'], 'Claim belongs to another app session', True)
    if not cleanup:
        ready(store, db, worker, binding, task)


def status(store, worker):
    from .workers import WORKERS
    need(worker in WORKERS, 'Enrolled worker required')
    with store.connection() as db:
        row = db.execute('SELECT * FROM app_worker_sessions WHERE worker=?', (worker,)).fetchone()
        return {'worker': worker, 'policy': policy(db, worker), 'readiness_reason': readiness_reason(store, db, worker), 'session': None if not row else {
            'session_id': row['session_id'], 'epoch': row['epoch'], 'registration': json.loads(row['registration']),
            'adopted': json.loads(row['adopted']) if row['adopted'] else None,
            'qualification': json.loads(row['qualification']) if row['qualification'] else None}}


def post_message(store, actor, body):
    from .workers import WORKERS
    need(actor in ('admin', *WORKERS), 'Authenticated inbox sender required')
    binding = body.get('app_binding')
    body = {k: v for k, v in body.items() if k != 'app_binding'}
    need(set(body) == {'request_id', 'recipient', 'task_id', 'generation', 'kind', 'summary', 'artifact_refs'}
         and hexid(body['request_id'], 32) and body['recipient'] in ('coordinator', *WORKERS)
         and hexid(body['task_id'], 16) and type(body['generation']) is int
         and body['kind'] in ('handoff', 'progress', 'blocker', 'decision', 'instruction')
         and text(body['summary'], 8000) and type(body['artifact_refs']) is list
         and len(body['artifact_refs']) <= 20 and all(text(x, 1000) for x in body['artifact_refs']),
         'Bounded task/generation handoff with stable request ID required')
    with store.transaction() as db:
        if actor != 'admin' and policy(db, actor):
            authenticate(db, actor, binding)
        old = db.execute('SELECT * FROM app_inbox WHERE sender=? AND request_id=?', (actor, body['request_id'])).fetchone()
        if old:
            need(old['document'] == encoded(body), 'Conflicting message retry', True)
            return {'id': old['id'], 'duplicate': True}
        task = db.execute('SELECT * FROM tasks WHERE id=?', (body['task_id'],)).fetchone()
        need(task and task['generation'] == body['generation'], 'Task generation changed', True)
        need(actor == 'admin' or actor in (task['owner'], task['last_owner']), 'Only the task actor may report its outcome', True)
        db.execute('INSERT INTO app_inbox(request_id,sender,recipient,document,created) VALUES(?,?,?,?,?)',
                   (body['request_id'], actor, body['recipient'], encoded(body), store.clock()))
        ident = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        store.event(db, actor, 'service-message-posted', task['id'], encoded({'id': ident, 'recipient': body['recipient'], 'kind': body['kind']}))
        return {'id': ident, 'duplicate': False}


def inbox(store, actor, body):
    from .workers import WORKERS
    need(actor in ('admin', *WORKERS) and set(body) == {'after', 'limit'}
         and type(body['after']) is int and body['after'] >= 0
         and type(body['limit']) is int and 1 <= body['limit'] <= 100, 'Authenticated bounded inbox cursor required')
    recipient = 'coordinator' if actor == 'admin' else actor
    with store.connection() as db:
        rows = db.execute('SELECT * FROM app_inbox WHERE recipient=? AND id>? ORDER BY id LIMIT ?',
                          (recipient, body['after'], body['limit'])).fetchall()
        return {'messages': [{'id': r['id'], 'sender': r['sender'], 'created': r['created'], **json.loads(r['document'])} for r in rows],
                'next_cursor': rows[-1]['id'] if rows else body['after']}
