"""Opt-in, audited publication coalescing. No publication or Git execution here.

Authenticated observers attest ancestry from their actual Git checkout. Missing,
stale or divergent ancestry is a scheduling barrier, never a licence to cancel.
"""
import hashlib
import json
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS publication_policies (
 repository TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
 proof TEXT NOT NULL DEFAULT '{}', inherited TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS publication_policy_audits (
 id TEXT PRIMARY KEY, repository TEXT NOT NULL, actor TEXT NOT NULL,
 request TEXT NOT NULL, before_state TEXT NOT NULL, after_state TEXT NOT NULL, at REAL NOT NULL);
"""


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def target(row):
    return json.loads(row['result']).get('release_target', {})


def rows_for(db, repository):
    return [r for r in db.execute("SELECT * FROM tasks WHERE task_type='publish' AND status NOT IN ('completed','cancelled') ORDER BY created,id")
            if target(r).get('repository') == repository]


def history(store, db, row):
    return {'task': store._correction_state(row),
            'receipts': [dict(r) for r in db.execute('SELECT * FROM receipts WHERE task_id=? ORDER BY generation,step', (row['id'],))],
            'events': [dict(r) for r in db.execute('SELECT * FROM events WHERE task_id=? ORDER BY id', (row['id'],))],
            'locks': [dict(r) for r in db.execute('SELECT * FROM locks WHERE task_id=?', (row['id'],))]}


def pristine(store, db, row):
    h = history(store, db, row)
    return (row['status'] == 'queued' and row['owner'] is None and row['last_owner'] is None
            and row['generation'] == row['step'] == row['fix_attempts_used'] == row['total_fix_attempts'] == 0
            and row['claim_hash'] is None and row['lease_until'] is None
            and not json.loads(row['attempt_allowance']) and not json.loads(row['attempt_exception'])
            and set(json.loads(row['result'])) == {'release_target'}
            and not h['receipts'] and not h['locks']
            and sum(e['kind'] == 'submitted' for e in h['events']) == 1
            and all(e['kind'] in ('submitted', 'publication-constraints-carried') for e in h['events']) and row['compute'] == 'standard')


def inherited_requirements(rows, inherited):
    """Carry every original prerequisite and restrictive requirement forward."""
    req = dict(inherited.get('requirements', {})); deps = set(inherited.get('dependencies', []))
    for row in rows:
        deps.update(json.loads(row['dependencies']))
        for key, value in json.loads(row['requirements']).items():
            if key in ('cpus', 'memory_gb', 'disk_gb'):
                req[key] = max(req.get(key, 0), value)
            elif key == 'capabilities':
                req[key] = sorted(set(req.get(key, [])) | set(value))
            elif key == 'worker' and key not in req:
                req[key] = value
            elif req.get(key) != value:
                raise ValueError('Conflicting or unsupported publication requirements')
    return {'requirements': req, 'dependencies': sorted(deps)}


def observe(store, db, repo, body):
    proof = body.get('publication_ancestry')
    if proof is None:
        return
    from .store import Invalid
    if (not isinstance(proof, dict) or set(proof) != {'head', 'published_head', 'ancestors'}
            or proof['head'] != repo['merged_head'] or proof['published_head'] != repo['published_head']
            or not isinstance(proof['ancestors'], list) or len(proof['ancestors']) > 512
            or any(not isinstance(v, str) or not re.fullmatch('[0-9a-f]{40}', v) for v in proof['ancestors'])
            or len(set(proof['ancestors'])) != len(proof['ancestors'])):
        raise Invalid('Exact current publication ancestry observation required')
    db.execute("INSERT OR IGNORE INTO publication_policies(repository) VALUES(?)", (repo['name'],))
    db.execute('UPDATE publication_policies SET proof=? WHERE repository=?',
               (encode({**proof, 'observed_at': repo['observed_at']}), repo['name']))


def calculate(store, db, repo):
    policy = db.execute('SELECT * FROM publication_policies WHERE repository=?', (repo['name'],)).fetchone()
    rows = rows_for(db, repo['name'])
    proof = json.loads(policy['proof']) if policy else {}
    fresh = (proof.get('head') == repo['merged_head'] and proof.get('published_head') == repo['published_head']
             and proof.get('observed_at', 0) >= store.clock() - 600 and repo['observed_at'] >= store.clock() - 600)
    ancestors = set(proof.get('ancestors', [])) if fresh else set()
    ancestors.add(repo['merged_head'])
    no_change = repo['merged_head'] == repo['published_head']
    barriers = []
    if not no_change and (not fresh or repo['published_head'] not in ancestors):
        barriers.append('Current published-to-desired ancestry is unverified')
    for row in rows:
        if not pristine(store, db, row):
            barriers.append('Retained claim/effect/approval history: ' + row['id'])
        elif target(row).get('branch') != repo['branch'] or target(row).get('commit') not in ancestors:
            barriers.append('Unverified target ancestry: ' + row['id'])
    try:
        inherited = json.loads(policy['inherited']) if policy else {}
        # The combined Nova publisher is qualified on Studio; guest development
        # capacity alone does not establish Xcode upload/signing readiness. This
        # restriction is applied only after explicit policy activation.
        if repo['name'] == 'oliverhu/nova-browser':
            inherited = inherited_requirements([{'requirements':encode({'worker':'oliverhu-mac-studio'}), 'dependencies':'[]'}], inherited)
        carried = inherited_requirements(rows, inherited)
    except (ValueError, TypeError):
        carried = {}; barriers.append('Conflicting publication requirements')
    survivor = next((r['id'] for r in rows if target(r).get('commit') == repo['merged_head']), None) if not no_change else None
    referenced = set()
    for dependent in db.execute("SELECT id,dependencies FROM tasks WHERE status NOT IN ('completed','cancelled')"):
        referenced.update(json.loads(dependent['dependencies']))
    if survivor and survivor in carried.get('dependencies', []):
        barriers.append('Inherited dependencies would make the latest publication depend on itself')
    for row in rows:
        if row['id'] != survivor and row['id'] in referenced:
            barriers.append('Original publication is still a required prerequisite: ' + row['id'])
    return {'repository': repo['name'], 'head': repo['merged_head'], 'published_head': repo['published_head'],
            'no_change': no_change, 'enabled': bool(policy and policy['enabled']), 'survivor': survivor,
            'barriers': barriers, 'inherited': carried,
            'supersede': [r['id'] for r in rows if r['id'] != survivor and carried
                          and r['id'] not in referenced and pristine(store, db, r) and target(r).get('branch') == repo['branch']
                          and target(r).get('commit') in ancestors
                          and (no_change or fresh and repo['published_head'] in ancestors)]}


def snapshot(store, db, repo):
    return {'repository': dict(repo), 'policy': dict(db.execute('SELECT * FROM publication_policies WHERE repository=?', (repo['name'],)).fetchone() or {}),
            'tasks': [history(store, db, r) for r in rows_for(db, repo['name'])]}


def plan(store, actor, body):
    from .store import Conflict, Invalid, bounded
    if actor != 'admin':
        raise Conflict('Publication policy planning requires administrator identity')
    name = bounded(body.get('repository'), 'repository', 200)
    with store.transaction() as db:
        repo = db.execute('SELECT * FROM repositories WHERE name=?', (name,)).fetchone()
        if not repo:
            raise Invalid('Unknown repository')
        state = snapshot(store, db, repo)
        reviewable = [r for r in rows_for(db, name) if reviewed_handoff(store, db, r)]
        return {'fingerprint': digest(state), 'proposal': calculate(store, db, repo),
                'history_fingerprints': {h['task']['id']: digest(h) for h in state['tasks']},
                'reviewable_handoffs': [{'task':r['id'], 'owner_receipt_hashes':[digest(json.loads(x['receipt'])) for x in history(store,db,r)['receipts']]} for r in reviewable]}


def apply_plan(store, actor, body):
    from .store import Conflict, Invalid, bounded
    if actor != 'admin':
        raise Conflict('Publication policy application requires administrator identity')
    if not isinstance(body, dict) or set(body) != {'repository', 'fingerprint', 'execution_stopped', 'external_state_checked', 'evidence'}:
        raise Invalid('Exact publication policy fields required')
    if body['execution_stopped'] is not True or body['external_state_checked'] is not True:
        raise Invalid('Positive stopped/checked policy evidence required')
    evidence = bounded(body['evidence'], 'evidence', 16000)
    bounded(body['repository'], 'repository', 200)
    if not isinstance(body['fingerprint'], str) or not re.fullmatch('[0-9a-f]{64}', body['fingerprint']):
        raise Invalid('Exact publication policy fingerprint required')
    with store.transaction() as db:
        previous = db.execute('SELECT id FROM publication_policy_audits WHERE repository=? AND actor=? AND request=?',
                              (body['repository'], actor, encode(body))).fetchone()
        if previous:
            return {'audit_id': previous['id'], 'enabled': True, 'replayed': True}
        repo = db.execute('SELECT * FROM repositories WHERE name=?', (body['repository'],)).fetchone()
        if not repo or digest(snapshot(store, db, repo)) != body['fingerprint']:
            raise Conflict('Publication policy state changed; replan before applying')
        before = snapshot(store, db, repo)
        proposal = calculate(store, db, repo)
        # Activation is explicit and retains barriers. It cannot settle a held
        # claim, remove an approval or turn a legacy receipt into no-effects proof.
        db.execute('INSERT OR IGNORE INTO publication_policies(repository) VALUES(?)', (repo['name'],))
        db.execute('UPDATE publication_policies SET enabled=1 WHERE repository=?', (repo['name'],))
        audit_id = digest({'request': body, 'before': before})
        store.event(db, actor, 'publication-policy-enabled', detail=encode({'repository': repo['name'], 'audit_id': audit_id, 'evidence': evidence}))
        after = snapshot(store, db, repo)
        db.execute('INSERT INTO publication_policy_audits VALUES(?,?,?,?,?,?,?)',
                   (audit_id, repo['name'], actor, encode(body), encode(before), encode(after), store.clock()))
        return {'audit_id': audit_id, 'proposal': calculate(store, db, repo), 'enabled': True}


def prepare_tick(store, db, repo):
    proposal = calculate(store, db, repo)
    if not proposal['enabled']:
        return None
    before = snapshot(store, db, repo)
    for ident in proposal['supersede']:
        # Pristine predicate and ancestry were checked in this same write
        # transaction. Results, targets, receipts, dependencies/counters stay intact.
        db.execute("UPDATE tasks SET status='cancelled',updated=?,progress=? WHERE id=?",
                   (store.clock(), 'Superseded by desired target ' + repo['merged_head'], ident))
        store.event(db, 'hourly-scheduler', 'publication-superseded', ident,
                    encode({'repository': repo['name'], 'desired_commit': repo['merged_head'], 'no_change': proposal['no_change'], 'prior_fingerprint': digest(before)}))
    if proposal['inherited']:
        db.execute('UPDATE publication_policies SET inherited=? WHERE repository=?', (encode(proposal['inherited']), repo['name']))
    def audit_tick():
        after = snapshot(store, db, repo)
        if after != before:
            audit_id = digest({'before': before, 'after': after, 'proposal': proposal})
            db.execute('INSERT OR IGNORE INTO publication_policy_audits VALUES(?,?,?,?,?,?,?)',
                       (audit_id, repo['name'], 'hourly-scheduler', encode(proposal), encode(before), encode(after), store.clock()))
    if proposal['barriers']:
        db.execute('UPDATE repositories SET last_error=? WHERE name=?', ('; '.join(proposal['barriers'])[:6000], repo['name']))
        audit_tick()
        return {'defer': True}
    if proposal['survivor']:
        row = db.execute('SELECT * FROM tasks WHERE id=?', (proposal['survivor'],)).fetchone()
        merged = inherited_requirements([row], proposal['inherited'])
        if json.loads(row['requirements']) != merged['requirements'] or json.loads(row['dependencies']) != merged['dependencies']:
            db.execute('UPDATE tasks SET requirements=?,dependencies=?,updated=? WHERE id=?',
                       (encode(merged['requirements']), encode(merged['dependencies']), store.clock(), row['id']))
            store.event(db, 'hourly-scheduler', 'publication-constraints-carried', row['id'], encode(merged))
    audit_tick()
    return {'defer': False, **proposal['inherited']}


def explicit_upload(row, store, db):
    """An explicit upload-only authorization is a distinct immutable scope."""
    value = json.loads(row['result'])
    h = history(store, db, row)
    authorization = value.get('release_authorization')
    return (row['status'] == 'queued' and row['generation'] == row['step'] == 0 and row['owner'] is None
            and row['last_owner'] is None and row['claim_hash'] is None and row['lease_until'] is None
            and row['fix_attempts_used'] == row['total_fix_attempts'] == 0
            and row['request_key'].startswith('ios-upload-only:')
            and set(value) == {'release_target','release_scope','release_authorization'}
            and value['release_scope'] == 'ios_upload_only' and isinstance(authorization,dict)
            and set(authorization) == {'thread_id','message_id'}
            and all(isinstance(v,str) and 0 < len(v) <= 200 for v in authorization.values())
            and not h['receipts'] and not h['locks'] and len(h['events']) == 1 and h['events'][0]['kind'] == 'submitted')


def admission(store, db, row):
    repo_name = target(row).get('repository') if row['task_type'] == 'publish' else None
    if not repo_name:
        return ''
    enabled = db.execute('SELECT enabled FROM publication_policies WHERE repository=?', (repo_name,)).fetchone()
    if not enabled or not enabled['enabled']:
        return ''
    repo = db.execute('SELECT * FROM repositories WHERE name=?', (repo_name,)).fetchone()
    if not repo:
        return ''
    if explicit_upload(row, store, db):
        pending = rows_for(db, repo_name)
        uploads = [r for r in pending if explicit_upload(r, store, db)]
        unsafe = [r for r in pending if not pristine(store,db,r) and not explicit_upload(r,store,db)]
        # Preserve original explicit upload authority and dependencies, while a
        # running/unknown/held publication remains a barrier to new execution.
        if not unsafe and uploads and uploads[0]['id'] == row['id']:
            return ''
        return 'Publication policy: earlier explicit upload or unresolved publication must settle'
    p = calculate(store, db, repo)
    if not p['enabled']:
        return ''
    if p['barriers']:
        return 'Publication policy: ' + '; '.join(p['barriers'])
    if p['no_change'] or p['survivor'] != row['id']:
        return 'Publication policy: obsolete target awaits audited supersession'
    # Before the hourly coalescing transaction carries constraints, refuse a
    # claim that could discard an older task's prerequisite or hard requirement.
    current = {'requirements': json.loads(row['requirements']), 'dependencies': sorted(json.loads(row['dependencies']))}
    if current != p['inherited']:
        return 'Publication policy: prior constraints await durable carry-forward'
    return ''


def reviewed_handoff(store, db, row):
    """Structural eligibility only; an admin must review each owner's no-effects statement."""
    h = history(store, db, row)
    def decision(value):
        if isinstance(value, dict):
            return any(k == 'approval_request' or k == 'human_stop' and v is True or decision(v) for k,v in value.items())
        return isinstance(value, list) and any(decision(v) for v in value)
    if (row['status'] != 'queued' or row['owner'] is not None or row['generation'] < 1
            or row['step'] != 0 or row['claim_hash'] is not None or row['lease_until'] is not None
            or row['fix_attempts_used'] != 0 or row['total_fix_attempts'] != 0
            or json.loads(row['attempt_allowance']) or json.loads(row['attempt_exception'])
            or set(json.loads(row['result'])) != {'release_target'} or h['locks'] or row['compute'] != 'standard'
            or len(h['receipts']) != row['generation']
            or {r['generation'] for r in h['receipts']} != set(range(1,row['generation']+1))
            or sum(e['kind']=='claimed' for e in h['events']) != row['generation']
            or any(e['kind'] not in ('submitted','claimed','host-reassigned','waiting','publication-constraints-carried') for e in h['events'])):
        return False
    for r in h['receipts']:
        envelope = json.loads(r['receipt']); receipt = envelope.get('receipt', {})
        if (envelope.get('outcome') != 'waiting' or r['step'] != 0 or not isinstance(receipt, dict)
                or receipt.get('execution_stopped') is not True or receipt.get('external_state_checked') is not True
                or set(receipt) - {'execution_stopped','external_state_checked','summary'}
                or not isinstance(receipt.get('summary'), str) or decision(receipt)):
            return False
    return True


def retire_reviewed_handoff(store, actor, body):
    from .store import Conflict, Invalid, bounded
    if actor != 'admin':
        raise Conflict('Historical no-effects review requires administrator identity')
    fields = {'repository','task','fingerprint','owner_receipt_hashes','release_actions_unstarted',
              'execution_stopped','external_state_checked','evidence'}
    if not isinstance(body,dict) or set(body) != fields or any(body[k] is not True for k in
            ('release_actions_unstarted','execution_stopped','external_state_checked')):
        raise Invalid('Exact positive historical no-effects review required')
    evidence = bounded(body['evidence'],'evidence',16000)
    bounded(body['repository'],'repository',200)
    if (not isinstance(body['task'],str) or not re.fullmatch('[0-9a-f]{16}',body['task'])
            or not isinstance(body['fingerprint'],str) or not re.fullmatch('[0-9a-f]{64}',body['fingerprint'])
            or not isinstance(body['owner_receipt_hashes'],list) or len(body['owner_receipt_hashes']) > 512
            or any(not isinstance(x,str) or not re.fullmatch('[0-9a-f]{64}',x) for x in body['owner_receipt_hashes'])):
        raise Invalid('Exact task and owner receipt fingerprints required')
    with store.transaction() as db:
        replay=db.execute('SELECT id FROM publication_policy_audits WHERE actor=? AND request=?',(actor,encode(body))).fetchone()
        if replay:return {'audit_id':replay['id'],'replayed':True}
        repo=db.execute('SELECT * FROM repositories WHERE name=?',(body['repository'],)).fetchone()
        if not repo or digest(snapshot(store,db,repo)) != body['fingerprint']:
            raise Conflict('Publication review state changed; replan')
        row=db.execute('SELECT * FROM tasks WHERE id=?',(body['task'],)).fetchone()
        proposal=calculate(store,db,repo)
        current=db.execute('SELECT * FROM publication_policies WHERE repository=?',(repo['name'],)).fetchone()
        proof=json.loads(current['proof']) if current else {}
        if (not proposal['enabled'] or not row or target(row).get('repository') != repo['name']
                or not reviewed_handoff(store,db,row) or not proposal['inherited']
                or target(row).get('branch') != repo['branch'] or target(row).get('commit') == repo['merged_head']
                or target(row).get('commit') not in proof.get('ancestors',[])
                or proof.get('head') != repo['merged_head'] or proof.get('published_head') != repo['published_head']
                or proof.get('observed_at',0) < store.clock()-600
                or repo['published_head'] not in set(proof.get('ancestors',[])) | {repo['merged_head']}):
            raise Conflict('Only positively reviewed obsolete unstarted handoffs may be retired')
        if any(row['id'] in json.loads(x['dependencies']) for x in db.execute(
                "SELECT dependencies FROM tasks WHERE status NOT IN ('completed','cancelled')")):
            raise Conflict('An original dependent still requires this exact publication')
        prior=history(store,db,row)
        hashes=[digest(json.loads(r['receipt'])) for r in prior['receipts']]
        if body['owner_receipt_hashes'] != hashes:
            raise Conflict('Every original owner receipt must match this review')
        before=snapshot(store,db,repo)
        db.execute('UPDATE publication_policies SET inherited=? WHERE repository=?',(encode(proposal['inherited']),repo['name']))
        db.execute("UPDATE tasks SET status='cancelled',updated=?,progress=? WHERE id=?",
                   (store.clock(),'Audited obsolete unstarted handoff: '+evidence,row['id']))
        ident=digest({'request':body,'before':before})
        store.event(db,actor,'publication-unstarted-reviewed',row['id'],encode({'audit_id':ident,'desired_commit':repo['merged_head'],'owner_receipt_hashes':hashes,'evidence':evidence}))
        db.execute('INSERT INTO publication_policy_audits VALUES(?,?,?,?,?,?,?)',
                   (ident,repo['name'],actor,encode(body),encode(before),encode(snapshot(store,db,repo)),store.clock()))
        return {'audit_id':ident,'task':store.public_task(db.execute('SELECT * FROM tasks WHERE id=?',(row['id'],)).fetchone())}
