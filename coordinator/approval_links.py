"""Administrator-verified cross-chat question linkage, separate from ordinary approvals."""
import hashlib
import json
import re

SCHEMA = '''CREATE TABLE IF NOT EXISTS approval_links (
 id TEXT PRIMARY KEY, actor TEXT NOT NULL, request TEXT NOT NULL,
 before_state TEXT NOT NULL, after_state TEXT NOT NULL, at REAL NOT NULL);'''


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def apply(store, actor, body):
    from .store import Conflict, Invalid, bounded
    if actor != 'admin':
        raise Conflict('Cross-chat approval linkage requires administrator identity')
    fields = {'task', 'generation', 'worker', 'original_question', 'source', 'question_text',
              'original_question_text', 'original_question_item_id', 'response', 'scope', 'scope_evidence', 'result_sha256',
              'decision_verified', 'link_verified', 'execution_stopped', 'external_state_checked'}
    if not isinstance(body, dict) or set(body) != fields or any(body[k] is not True for k in
            ('decision_verified', 'link_verified', 'execution_stopped', 'external_state_checked')):
        raise Invalid('Exact positively verified question-link evidence required')
    request = dict(body)
    for k in ('question_text', 'original_question_text', 'original_question_item_id', 'response', 'scope', 'scope_evidence', 'worker'):
        request[k] = bounded(body[k], k, 2000)
    for k, keys in [('original_question', {'thread_id', 'question_id'}),
                    ('source', {'thread_id', 'question_id', 'message_id'})]:
        if not isinstance(body[k], dict) or set(body[k]) != keys:
            raise Invalid('Exact original question and actual response identities required')
        request[k] = {s: bounded(body[k][s], k+'.'+s, 500) for s in keys}
    if (not re.fullmatch('[0-9a-f]{16}', str(body['task']))
            or type(body['generation']) is not int or body['generation'] < 1
            or not re.fullmatch('[0-9a-f]{64}', str(body['result_sha256']))
            or request['original_question']['thread_id'] == request['source']['thread_id']):
        raise Invalid('Exact task/generation/result and distinct cross-chat source required')
    identity = {k: request[k] for k in ('task', 'generation', 'original_question', 'source')}
    ident = hashlib.sha256(encode(identity).encode()).hexdigest()
    with store.transaction() as db:
        prior = db.execute('SELECT * FROM approval_links WHERE id=?', (ident,)).fetchone()
        if prior:
            if prior['request'] != encode(request):
                raise Conflict('Immutable question link already has different scope/evidence')
            return {'link_id': ident, 'replayed': True, 'task': store.public_task(db.execute('SELECT * FROM tasks WHERE id=?', (request['task'],)).fetchone())}
        row = db.execute('SELECT * FROM tasks WHERE id=?', (request['task'],)).fetchone()
        if not row or row['status'] != 'pending_external_approval' or row['generation'] != request['generation'] or row['owner'] != request['worker']:
            raise Conflict('Exact original pending task/generation/owner required')
        result = json.loads(row['result'])
        if (hashlib.sha256(encode(result).encode()).hexdigest() != request['result_sha256']
                or result.get('approval_request') != request['original_question']
                or result.get('execution_stopped') is not True
                or result.get('external_state_checked') is not True
                or result.get('acceptance_complete') is not False
                or result.get('human_stop') is True
                or result.get('unknown_operations', []) != [] or result.get('unsettled_operations', []) != []):
            raise Conflict('Original question/result, settled execution and holds must match')
        if row['fix_attempts_used'] >= store.attempt_limit(row) and row['reassignment_count'] >= 6:
            raise Conflict('Retry limits exhausted')
        before = store.public_task(row)
        store.invalidate_attempt_exception(db, row)
        db.execute("UPDATE tasks SET status='queued',last_owner=owner,owner=NULL,claim_hash=NULL,lease_until=NULL,retry_after=NULL,updated=?,progress=? WHERE id=?",
                   (store.clock(), 'Cross-chat approval linked; execution/qualification still required. '+request['scope_evidence'], row['id']))
        source_id = hashlib.sha256(encode(request['source']).encode()).hexdigest()
        store.event(db, actor, 'approval-applied', row['id'], encode({'approval_id': source_id,
            'generation': row['generation'], 'source': request['source'], 'response': request['response'],
            'scope': request['scope'], 'scope_evidence': request['scope_evidence'],
            'question_link': {'link_id': ident, 'original_question': request['original_question'],
                              'task': row['id'], 'generation': row['generation'], 'result_sha256': request['result_sha256']}}))
        after = store.public_task(db.execute('SELECT * FROM tasks WHERE id=?', (row['id'],)).fetchone())
        db.execute('INSERT INTO approval_links VALUES(?,?,?,?,?,?)',
                   (ident, actor, encode(request), encode(before), encode(after), store.clock()))
        return {'link_id': ident, 'replayed': False, 'task': after}


def detail(store, actor, ident):
    from .store import Conflict, Invalid
    if actor != 'admin':
        raise Conflict('Question-link provenance requires administrator identity')
    with store.connection() as db:
        row = db.execute('SELECT * FROM approval_links WHERE id=?', (ident,)).fetchone()
        if not row:
            raise Invalid('Unknown question link')
        return {**dict(row), **{k: json.loads(row[k]) for k in ('request', 'before_state', 'after_state')}}
