"""Explicit, opt-in task admission and immutable human decision revisions.

Readiness is a scoped attestation, never permission or cleanup. Revocation fences
future effects, retaining the actual claim and all original outcomes. Every read
and transition here uses the caller's existing SQLite transaction.
"""
import hashlib
import json
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS task_decisions (
 decision_key TEXT NOT NULL, revision INTEGER NOT NULL, request_id TEXT UNIQUE NOT NULL,
 document TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(decision_key,revision));
CREATE TABLE IF NOT EXISTS task_readiness (
 task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, document TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS task_readiness_updates (
 request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
 document TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS task_execution_admission (
 task_id TEXT NOT NULL, generation INTEGER NOT NULL, worker TEXT NOT NULL,
 readiness_revision INTEGER NOT NULL, phase TEXT NOT NULL DEFAULT 'claimed',
 PRIMARY KEY(task_id,generation));
"""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def need(condition, message, conflict=False):
    from .store import Conflict, Invalid
    if not condition:
        raise (Conflict if conflict else Invalid)(message)


def hexid(value, length=64):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % length, value) is not None


def text(value, limit=2000):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def validate_declaration(value):
    need(isinstance(value, dict) and set(value) == {'checks', 'decisions'}
         and type(value['checks']) is list and 0 < len(value['checks']) <= 32
         and all(text(name, 160) for name in value['checks'])
         and len(set(value['checks'])) == len(value['checks'])
         and isinstance(value['decisions'], dict) and len(value['decisions']) <= 32
         and all(text(key, 160) and hexid(scope) for key, scope in value['decisions'].items()),
         'Readiness declaration requires unique capability names and scoped decision keys')


def context(row):
    # Generation is checked separately: an attested queued generation is consumed
    # by exactly its next claim. Wait/routing/step changes invalidate this digest.
    value = {key: row[key] for key in ('id', 'step', 'requirements', 'dependencies', 'steps', 'task_type')}
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def decision(store, actor, body):
    need(actor == 'admin', 'Decision revisions require administrator identity')
    need(set(body) == {'request_id', 'decision_key', 'expected_revision', 'state', 'source', 'scope_sha256', 'evidence'}
         and hexid(body['request_id'], 32) and text(body['decision_key'], 160)
         and type(body['expected_revision']) is int and body['expected_revision'] >= 0
         and body['state'] in ('approved', 'denied', 'superseded')
         and isinstance(body['source'], dict) and set(body['source']) == {'thread_id', 'message_id', 'verified'}
         and all(text(body['source'][key], 240) for key in ('thread_id', 'message_id'))
         and body['source']['verified'] is True and hexid(body['scope_sha256']) and text(body['evidence']),
         'Exact verified primary human source, scoped decision and revision CAS required')
    need(len(encoded(body)) <= 10000, 'Decision audit exceeds bounded event size')
    with store.transaction() as db:
        prior = db.execute('SELECT * FROM task_decisions WHERE request_id=?', (body['request_id'],)).fetchone()
        if prior:
            need(prior['document'] == encoded(body), 'Conflicting decision retry', True)
            return {'revision': prior['revision'], 'duplicate': True}
        latest = db.execute('SELECT * FROM task_decisions WHERE decision_key=? ORDER BY revision DESC LIMIT 1',
                            (body['decision_key'],)).fetchone()
        revision = latest['revision'] if latest else 0
        need(revision == body['expected_revision'], 'Decision revision changed', True)
        # A source message may cover several decision keys, but cannot later be
        # reinterpreted as another revision of the same decision.
        for record in db.execute('SELECT document FROM task_decisions WHERE decision_key=?', (body['decision_key'],)):
            source = json.loads(record['document'])['source']
            need(source != body['source'], 'Human source already recorded for this decision', True)
        revision += 1
        db.execute('INSERT INTO task_decisions VALUES(?,?,?,?,?)',
                   (body['decision_key'], revision, body['request_id'], encoded(body), store.clock()))
        store.event(db, actor, 'decision-revised', None, encoded({'revision': revision, 'request': body}))
        return {'revision': revision, 'duplicate': False}


def update(store, actor, ident, body):
    from .workers import WORKERS
    need(actor == 'admin', 'Task readiness requires administrator identity')
    keys = {'request_id', 'generation', 'expected_revision', 'context_sha256', 'worker', 'state',
            'valid_until', 'checks', 'decisions', 'evidence'}
    need(set(body) == keys and hexid(body['request_id'], 32)
         and type(body['generation']) is int and body['generation'] >= 0
         and type(body['expected_revision']) is int and body['expected_revision'] >= 0
         and hexid(body['context_sha256']) and body['worker'] in WORKERS
         and body['state'] in ('ready', 'waiting') and type(body['valid_until']) in (int, float)
         and store.clock() < body['valid_until'] <= store.clock() + 86400
         and type(body['checks']) is list and 0 < len(body['checks']) <= 32
         and type(body['decisions']) is list and len(body['decisions']) <= 32 and text(body['evidence']),
         'Exact task context, worker, finite readiness and structured checks required')
    names = set()
    for check in body['checks']:
        need(isinstance(check, dict) and set(check) == {'name', 'state', 'evidence_sha256'}
             and text(check['name'], 160) and check['name'] not in names
             and check['state'] in ('qualified', 'missing', 'not_required') and hexid(check['evidence_sha256']),
             'Unique scoped capability checks with actual evidence hashes required')
        names.add(check['name'])
    decisions = set()
    for binding in body['decisions']:
        need(isinstance(binding, dict) and set(binding) == {'decision_key', 'revision', 'scope_sha256'}
             and text(binding['decision_key'], 160) and binding['decision_key'] not in decisions
             and type(binding['revision']) is int and binding['revision'] > 0 and hexid(binding['scope_sha256']),
             'Unique exact decision revision and scope required')
        decisions.add(binding['decision_key'])
    need(body['state'] != 'ready' or all(c['state'] != 'missing' for c in body['checks']),
         'Missing checks cannot establish readiness')
    need(len(encoded(body)) <= 10000, 'Readiness audit exceeds bounded event size')
    with store.transaction() as db:
        prior = db.execute('SELECT * FROM task_readiness_updates WHERE request_id=?', (body['request_id'],)).fetchone()
        if prior:
            need(prior['task_id'] == ident and prior['document'] == encoded(body), 'Conflicting readiness retry', True)
            return {'revision': prior['revision'], 'duplicate': True}
        row = db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()
        need(row and row['generation'] == body['generation'] and context(row) == body['context_sha256'],
             'Task generation or constraints changed', True)
        need(row['status'] in ('queued', 'blocked', 'running', 'pending_external_approval', 'held', 'failed'),
             'Terminal task cannot gain readiness', True)
        need(row['status'] != 'running' or body['state'] == 'waiting',
             'Active claim may only have readiness revoked; settle before requalification', True)
        declaration = json.loads(row['requirements']).get('readiness')
        if declaration is not None:
            need(set(declaration['checks']) <= names
                 and all(any(b['decision_key'] == key and b['scope_sha256'] == scope for b in body['decisions'])
                         for key, scope in declaration['decisions'].items()),
                 'Readiness must cover every declared capability and decision scope')
        current = db.execute('SELECT revision FROM task_readiness WHERE task_id=?', (ident,)).fetchone()
        revision = current['revision'] if current else 0
        need(revision == body['expected_revision'], 'Readiness revision changed', True)
        if body['state'] == 'ready':
            need(not decision_reason(db, body), 'Decision is missing, stale, denied or superseded', True)
        revision += 1
        db.execute('INSERT INTO task_readiness VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET '
                   'revision=excluded.revision,document=excluded.document,at=excluded.at',
                   (ident, revision, encoded(body), store.clock()))
        db.execute('INSERT INTO task_readiness_updates VALUES(?,?,?,?,?)',
                   (body['request_id'], ident, revision, encoded(body), store.clock()))
        store.event(db, actor, 'task-readiness-revised', ident, encoded({'revision': revision, 'request': body}))
        return {'revision': revision, 'duplicate': False}


def decision_reason(db, document):
    for binding in document['decisions']:
        latest = db.execute('SELECT * FROM task_decisions WHERE decision_key=? ORDER BY revision DESC LIMIT 1',
                            (binding['decision_key'],)).fetchone()
        if not latest:
            return 'Required decision is missing'
        value = json.loads(latest['document'])
        if (latest['revision'] != binding['revision'] or value['state'] != 'approved'
                or value['scope_sha256'] != binding['scope_sha256']):
            return 'Required decision is stale, denied or superseded'
    return ''


def view(store, db, row, worker=None, execution=False):
    record = db.execute('SELECT * FROM task_readiness WHERE task_id=?', (row['id'],)).fetchone()
    if not record:
        required = 'readiness' in json.loads(row['requirements'])
        return {'managed': required, 'revision': 0,
                'reason': 'Explicit task readiness not yet recorded' if required else ''}
    document = json.loads(record['document'])
    reason = ''
    generation = row['generation'] - int(execution)
    if document['state'] != 'ready':
        reason = 'Task readiness explicitly waiting'
    elif document['generation'] != generation or document['context_sha256'] != context(row):
        reason = 'Task readiness generation or constraints changed'
    elif store.clock() >= document['valid_until']:
        reason = 'Task readiness expired'
    elif worker is not None and document['worker'] != worker:
        reason = 'Task readiness is qualified for another worker'
    else:
        reason = decision_reason(db, document)
    if execution:
        claim = db.execute('SELECT * FROM task_execution_admission WHERE task_id=? AND generation=?',
                           (row['id'], row['generation'])).fetchone()
        if not claim or claim['readiness_revision'] != record['revision'] or claim['worker'] != worker:
            reason = reason or 'Claim readiness revision changed; stop effects and settle'
    return {'managed': True, 'revision': record['revision'], 'reason': reason, 'worker': document['worker'],
            'state': document['state'], 'valid_until': document['valid_until'],
            'context_sha256': context(row), 'decisions': document['decisions'], 'checks': document['checks']}


def claimed(store, db, row, worker, generation):
    state = view(store, db, row, worker)
    need(not state['reason'], state['reason'], True)
    db.execute('INSERT INTO task_execution_admission VALUES(?,?,?,?,?)',
               (row['id'], generation, worker, state['revision'], 'claimed'))


def guard(store, db, row, worker, phase=None):
    # Explicit cleanup is a prospective owner transition only. It does not alter
    # old outcomes, release a wake, declare stopped execution or clear unknowns.
    need(phase in (None, 'implementing', 'testing', 'cleanup'), 'Unknown explicit execution phase')
    claim = db.execute('SELECT * FROM task_execution_admission WHERE task_id=? AND generation=?',
                       (row['id'], row['generation'])).fetchone()
    if phase != 'cleanup' and not (phase is None and claim and claim['phase'] == 'cleanup'):
        state = view(store, db, row, worker, execution=True)
        need(not state['reason'], state['reason'], True)
    if phase == 'cleanup' and not claim:
        # A pre-upgrade owning claim has no admission row. Record only the
        # actual prospective cleanup phase; no readiness/acceptance is inferred.
        db.execute('INSERT INTO task_execution_admission VALUES(?,?,?,?,?)',
                   (row['id'], row['generation'], worker, 0, 'cleanup'))
    if phase is not None:
        db.execute('UPDATE task_execution_admission SET phase=? WHERE task_id=? AND generation=?',
                   (phase, row['id'], row['generation']))


def invalidate_worker(store, db, actor, worker, evidence):
    """Hook for the separately authorized reset owner's *same* transaction.

    The reset owner must validate its human decision, epoch CAS and archive first.
    This only withdraws qualification, including for never-claimed queued tasks;
    it never fences a token, clears unknowns, or represents reset/cleanup success.
    """
    import uuid
    from .workers import WORKERS
    need(actor == 'admin' and worker in WORKERS and text(evidence),
         'Authorized reset owner and bounded invalidation evidence required')
    changed = []
    for record in db.execute("SELECT r.* FROM task_readiness r JOIN tasks t ON t.id=r.task_id "
                             "WHERE t.status NOT IN ('completed','cancelled')").fetchall():
        document = json.loads(record['document'])
        if document['worker'] != worker or document['state'] != 'ready':
            continue
        revision = record['revision'] + 1
        document = {**document, 'request_id': uuid.uuid4().hex, 'expected_revision': record['revision'],
                    'state': 'waiting', 'evidence': evidence}
        db.execute('UPDATE task_readiness SET revision=?,document=?,at=? WHERE task_id=?',
                   (revision, encoded(document), store.clock(), record['task_id']))
        db.execute('INSERT INTO task_readiness_updates VALUES(?,?,?,?,?)',
                   (document['request_id'], record['task_id'], revision, encoded(document), store.clock()))
        store.event(db, actor, 'worker-readiness-invalidated', record['task_id'], encoded({
            'worker': worker, 'revision': revision, 'previous_revision': record['revision'], 'evidence': evidence}))
        changed.append(record['task_id'])
    return changed
