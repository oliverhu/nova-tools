"""Opt-in exact-task eligibility; held resources never become generic capacity.

Service policy enrollment is administrator-only. Worker reservations require the
same policy, full held/target history CAS and a fresh matching local fence. The
normal wake, ACK, availability and task acceptance contracts stay unchanged.
"""
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
import secrets

from .cli_results import TASK_FIELDS
from .workers import MINI_VM_WORKERS

SCHEMA = """
CREATE TABLE IF NOT EXISTS resource_scope_policies(
 worker TEXT PRIMARY KEY, revision TEXT NOT NULL, document TEXT NOT NULL,
 enabled INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS resource_scope_runs(
 id TEXT PRIMARY KEY, worker TEXT NOT NULL, task_id TEXT NOT NULL,
 queued_generation INTEGER NOT NULL, run_id TEXT NOT NULL UNIQUE,
 policy_revision TEXT NOT NULL, task_sha256 TEXT NOT NULL, token_hash TEXT NOT NULL,
 local_fence_sha256 TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
 expires REAL NOT NULL, claim_generation INTEGER, result_sha256 TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_resource_scope_worker ON resource_scope_runs(worker)
 WHERE status IN ('reserved','claimed');
CREATE UNIQUE INDEX IF NOT EXISTS one_resource_scope_task ON resource_scope_runs(task_id)
 WHERE status IN ('reserved','claimed');
"""
SAFE_OPS = {
 'source': {'source-read','source-edit','git-local','git-transfer','source-audit'},
 'inert': {'source-read','inert-python','source-audit'},
 'media': {'media-read','image-inspect','video-inspect'},
}
SAFE_CAPS = {'cli-shell','git-source','cli-media','cli-media-video'}
OWNED_ROLES = {'host','driver','executor','controller','native-main','server'}
SCOPE_TASK_FIELDS = (*TASK_FIELDS, 'title', 'description', 'task_type', 'compute', 'priority',
                     'request_key', 'submitted_by', 'outcome_corrections', 'attempt_corrections',
                     'attempt_allowances', 'attempt_allowance_amendments')
UNKNOWN_KEYS = {'unknown_operations','unsettled_operations','unresolved_observations'}


def need(ok, reason):
    if not ok: raise ValueError(reason)

def finite(v):return type(v) in (int,float) and math.isfinite(v)
def exact(v,keys):return type(v) is dict and set(v)==set(keys.split())
def hexid(v,n):return type(v) is str and re.fullmatch('[0-9a-f]{%d}'%n,v) is not None
def encoded(v):return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(v):return hashlib.sha256(encoded(v).encode()).hexdigest()
def reference(v):return exact(v,'path sha256') and type(v['path']) is str and v['path'].startswith('/') and hexid(v['sha256'],64)


def path(v):
    need(type(v) is str and v.startswith('/') and not v.startswith('//') and str(PurePosixPath(v))==v
         and '..' not in PurePosixPath(v).parts and not any(ord(c)<32 for c in v), 'Exact canonical resource path required')
    return PurePosixPath(v)


def resource(v, held=False):
    need(type(v) is dict and v.get('kind') in ('path','media','capability','git','process'), 'Unknown resource kind')
    kind=v['kind']
    if kind=='path':
        need(exact(v,'kind value access') and v['access'] in ('read','write'), 'Exact path/access resource required');path(v['value'])
    elif kind=='media':need(exact(v,'kind sha256 path access') and hexid(v['sha256'],64) and v['access']=='read','Exact read-only media content resource required');path(v['path'])
    elif kind=='capability':need(exact(v,'kind value') and type(v['value']) is str and re.fullmatch('[a-z][a-z0-9-]{0,79}',v['value']),'Exact capability resource required')
    elif kind=='git':need(exact(v,'kind repository') and v['repository'] in ('oliverhu/nova-browser','oliverhu/nova-tools'),'Only exact reviewed source Git remotes allowed')
    else:
        need(held and exact(v,'kind pid uid birth') and type(v['pid']) is int and v['pid']>0 and type(v['uid']) is int
             and type(v['birth']) is list and len(v['birth'])==2 and all(type(x) is int for x in v['birth']), 'Process resources are held only, never source/inert/media operations')


def intersects(a,b):
    if a['kind']=='media' and b['kind']=='path':return intersects({'kind':'path','value':a['path'],'access':'read'},b)
    if b['kind']=='media' and a['kind']=='path':return intersects(a,{'kind':'path','value':b['path'],'access':'read'})
    if a['kind']!=b['kind']:return False
    if a['kind']=='path':
        left,right=path(a['value']),path(b['value'])
        return left==right or left in right.parents or right in left.parents
    if a['kind']=='media':return a['sha256']==b['sha256'] or a['path']==b['path']
    return a==b  # Read sharing is deliberately not inferred for a held resource.


def unknowns(value,pointer=''):
    rows=[]
    if type(value) is dict:
        need(not any(value.get(k) is True for k in ('human_stop','stop_requested','global_stop')), 'Global human/operation stop prevents scoped work')
        for key,item in value.items():
            child=pointer+'/'+key.replace('~','~0').replace('/','~1')
            if key in UNKNOWN_KEYS:
                need(type(item) is list,'Explicit unknown inventory required')
                rows += [{'pointer':child+'/'+str(i),'value_sha256':digest(row)} for i,row in enumerate(item)]
                unknowns(item,child)  # Also reject a nested global stop inside an opaque unknown.
            else:rows+=unknowns(item,child)
    elif type(value) is list:
        for i,item in enumerate(value):rows+=unknowns(item,pointer+'/'+str(i))
    return rows


def identity(v,uid):
    return exact(v,'pid uid birth') and type(v['pid']) is int and v['pid']>0 and type(v['uid']) is int and v['uid']==uid and type(v['birth']) is list and len(v['birth'])==2 and all(type(x) is int for x in v['birth']) and v['birth'][0]>0 and 0<=v['birth'][1]<1000000


def validate_owned(proofs,uid,held):
    need(type(proofs) is list and len(proofs)==len(OWNED_ROLES),'All six actual owned roles need separate settlement proof')
    roles=set()
    for p in proofs:
        need(exact(p,'role run_id task generation identity waiter facts provenance evidence_sha256 recorded_at observer_source_sha256 not_before_epoch not_after_epoch') and p['role'] in OWNED_ROLES and p['role'] not in roles
             and identity(p['identity'],uid) and identity(p['waiter'],uid) and hexid(p['evidence_sha256'],64)
             and hexid(p['observer_source_sha256'],64) and finite(p['recorded_at']) and p['recorded_at']>0,'Exact owned generations/provenance required')
        need(p['run_id']==held['run_id'] and p['task']==held['task'] and type(p['generation']) is int and p['generation']==held['generation']
             and finite(p['not_before_epoch']) and finite(p['not_after_epoch'])
             and 0<p['not_before_epoch']<=p['recorded_at']<=p['not_after_epoch'], 'Owned proof run/generation/epoch differs')
        roles.add(p['role']);facts=p['facts']
        if p['role']=='server':
            need(exact(facts,'thread_joined listener_closed connection_refused endpoint nonce')
                 and all(facts[k] is True for k in ('thread_joined','listener_closed','connection_refused'))
                 and type(facts['endpoint']) is str and re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}',facts['endpoint'])
                 and 0<int(facts['endpoint'].rsplit(':',1)[-1])<=65535 and hexid(facts['nonce'],32),'Positive exact owned server closure required')
        else:
            need(exact(facts,'reaped normal_exit returncode wait_target_pid') and facts['reaped'] is True and facts['normal_exit'] is True
                 and type(facts['returncode']) is int and 0<=facts['returncode']<=255
                 and type(facts['wait_target_pid']) is int and facts['wait_target_pid']==p['identity']['pid'],'Positive exact owned wait/reap required')
        provenance=p['provenance']
        need(type(provenance) is dict and set(provenance)==set(facts)|{'role','run_id','task','generation','identity','waiter','recorded_at','observer_source_sha256'}
             and all(exact(row,'reference pointer') and reference(row['reference']) and type(row['pointer']) is str and row['pointer'].startswith('/') for row in provenance.values()),'Every owned fact needs bound raw provenance')
        need(p['evidence_sha256']==provenance['recorded_at']['reference']['sha256'],'Owned evidence must name its actual raw receipt')
        if p['role']!='server':
            need(all(provenance[k]['reference']==provenance['wait_target_pid']['reference'] for k in facts),'Target/wait/reap facts must share one actual raw wait receipt')


def validate_policy(policy):
    need(exact(policy,'schema worker uid held resources owned_settlement proof_sources local_fence_sha256 allowed_workspaces allowed_kinds allowed_capabilities expires_at review_source')
         and type(policy['schema']) is int and policy['schema']==1 and policy['worker'] in MINI_VM_WORKERS
         and type(policy['uid']) is int and policy['uid']>=0, 'Exact opt-in resource policy required')
    need(type(policy['held']) is list and len(policy['held'])==1 and type(policy['resources']) is list and 0<len(policy['resources'])<=32,'Bounded held inventory required')
    for r in policy['resources']:resource(r,held=True)
    need(type(policy['proof_sources']) is list and 0<len(policy['proof_sources'])<=32 and all(reference(r) for r in policy['proof_sources'])
         and hexid(policy['local_fence_sha256'],64),'Exact local proof/fence sources required')
    validate_owned(policy['owned_settlement'],policy['uid'],policy['held'][0])
    for proof in policy['owned_settlement']:
        need(all(row['reference'] in policy['proof_sources'] for row in proof['provenance'].values()),'Owned provenance outside reviewed proof sources')
    seen=set()
    for held in policy['held']:
        need(exact(held,'run_id task generation api_sha256 originals unknown_bindings approval_sha256 approval_resources approval_scope_known')
             and hexid(held['run_id'],32) and hexid(held['task'],16) and held['task'] not in seen
             and type(held['generation']) is int and held['generation']>0 and hexid(held['api_sha256'],64),'Exact held run/owner/history binding required')
        seen.add(held['task'])
        need(type(held['originals']) is dict and {'state.json','worker-report.json','task-readback.json'}<=set(held['originals'])
             and set(held['originals'])<={'state.json','worker-report.json','task-readback.json','finish-response.json','wait-receipt.json','stopped-cleanup.json'}
             and all(hexid(v,64) for v in held['originals'].values()),'Immutable original inventory required')
        need(type(held['unknown_bindings']) is list and held['unknown_bindings'],'Unknowns must remain explicit')
        for row in held['unknown_bindings']:
            need(exact(row,'source pointer value_sha256 resource_indexes effects_known') and type(row['source']) is str
                 and type(row['pointer']) is str and row['pointer'].startswith('/') and hexid(row['value_sha256'],64)
                 and type(row['resource_indexes']) is list and row['resource_indexes'] and len(set(row['resource_indexes']))==len(row['resource_indexes'])
                 and all(type(i) is int and 0<=i<len(policy['resources']) for i in row['resource_indexes'])
                 and row['effects_known'] is True,'Unknown/unbounded effects cannot be treated as disjoint')
        need(held['approval_scope_known'] is True and hexid(held['approval_sha256'],64)
             and type(held['approval_resources']) is list and held['approval_resources']
             and all(type(i) is int and 0<=i<len(policy['resources']) for i in held['approval_resources']),'Original approval hold needs bounded preserved scope, not inferred clearance')
    need(type(policy['allowed_workspaces']) is list and 0<len(policy['allowed_workspaces'])<=8,'Explicit isolated workspaces required')
    for workspace in policy['allowed_workspaces']:
        path(workspace)
        need(not any(intersects({'kind':'path','value':workspace,'access':'write'},r) for r in policy['resources']),'Workspace intersects a held resource')
    need(type(policy['allowed_kinds']) is list and policy['allowed_kinds'] and set(policy['allowed_kinds'])<=set(SAFE_OPS)
         and type(policy['allowed_capabilities']) is list and policy['allowed_capabilities'] and set(policy['allowed_capabilities'])<=SAFE_CAPS,'Only known source/inert/media capabilities may enroll')
    need(finite(policy['expires_at']) and exact(policy['review_source'],'thread_id message_id')
         and all(type(v) is str and v.strip() for v in policy['review_source'].values()),'Actual scoped review provenance/expiry required')


def declaration(task,policy):
    need(task.get('status')=='queued' and task.get('owner') is None and task.get('task_type')!='publish' and task.get('compute')=='standard'
         and task.get('id') not in {h['task'] for h in policy['held']}, 'Held/native/publish/retry task cannot borrow a disjoint scope')
    requirements=task.get('requirements',{});scope=requirements.get('resource_scope')
    need(requirements.get('worker')==policy['worker'] and exact(scope,'schema kind operations resources workspace required_capabilities effects_declared acceptance_within_scope')
         and type(scope['schema']) is int and scope['schema']==1 and scope['kind'] in policy['allowed_kinds'],'Explicit constrained task declaration required')
    need(type(scope['operations']) is list and scope['operations'] and len(set(scope['operations']))==len(scope['operations'])
         and set(scope['operations'])<=SAFE_OPS[scope['kind']] and scope['effects_declared'] is True and scope['acceptance_within_scope'] is True,'Unknown/native operations or incomplete acceptance cannot use scoped eligibility')
    need(type(scope['required_capabilities']) is list and scope['required_capabilities'] and set(scope['required_capabilities'])<=set(policy['allowed_capabilities'])
         and set(requirements.get('capabilities',[]))==set(scope['required_capabilities']) and 'cli-shell' in scope['required_capabilities'],'Exact known resource and capability requirements required')
    if scope['kind']=='media':need('cli-media' in scope['required_capabilities'],'Actual media readiness capability required')
    if 'video-inspect' in scope['operations']:need('cli-media-video' in scope['required_capabilities'],'Actual temporal video readiness required')
    if 'git-transfer' in scope['operations']:need('git-source' in scope['required_capabilities'],'Exact source Git capability required')
    workspace=path(scope['workspace'])
    need(any(workspace==path(root) or path(root) in workspace.parents for root in policy['allowed_workspaces']),'Workspace outside reviewed isolated source roots')
    need(type(scope['resources']) is list and 0<len(scope['resources'])<=16,'Explicit bounded task resource footprint required')
    writable=False
    for r in scope['resources']:
        resource(r)
        need(not any(intersects(r,held) for held in policy['resources']),'Task intersects a held resource')
        if r['kind']=='path' and r['access']=='write':
            value=path(r['value']);need(value==workspace or workspace in value.parents,'Writes must remain in isolated workspace');writable=True
        if r['kind']=='capability':need(r['value'] in scope['required_capabilities'],'Undeclared capability resource')
    need(writable,'Isolated report/output workspace must be explicit')
    need({r['value'] for r in scope['resources'] if r['kind']=='capability'}==set(scope['required_capabilities']),'Every required capability must be resource-declared')
    if scope['kind']=='media':need(any(r['kind']=='media' for r in scope['resources']),'Exact read-only media content/path required')
    if 'git-transfer' in scope['operations']:need(any(r['kind']=='git' for r in scope['resources']),'Exact source Git remote resource required')
    return scope


def task_snapshot(store,db,ident):
    row=db.execute('SELECT * FROM tasks WHERE id=?',(ident,)).fetchone();need(row is not None,'Unknown scope task')
    task=store.public_task(row);task['approval']=store.approval_state(db,row)
    task['receipts']=[{**dict(r),'receipt':json.loads(r['receipt'])} for r in db.execute('SELECT * FROM receipts WHERE task_id=? ORDER BY at',(ident,))]
    task['events']=[dict(r) for r in db.execute('SELECT * FROM events WHERE task_id=? ORDER BY id',(ident,))]
    for table in ('outcome_corrections','attempt_corrections','attempt_allowances','attempt_allowance_amendments'):
        task[table]=[store._correction_readback(r) for r in db.execute('SELECT * FROM '+table+' WHERE task_id=? ORDER BY at,rowid',(ident,))]
    return task


def task_fingerprint(task):return digest({k:task.get(k) for k in SCOPE_TASK_FIELDS})


def verify_held(store,db,policy):
    for held in policy['held']:
        task=task_snapshot(store,db,held['task'])
        need(task_fingerprint(task)==held['api_sha256'] and task['generation']>=held['generation']
             and task.get('owner') in (None,policy['worker']) and task.get('last_owner') in (None,policy['worker']), 'Held owner/generation/full history changed')
        actual=unknowns(task)
        rows=[{'pointer':r['pointer'],'value_sha256':r['value_sha256']} for r in held['unknown_bindings'] if r['source']=='fresh-api']
        need(sorted(rows,key=encoded)==sorted(actual,key=encoded),'Every current API unknown must remain mapped to held resources')
        need(digest(task.get('approval'))==held['approval_sha256'],'Original approval hold changed')


def configure(store,actor,worker,body):
    from .store import Invalid,Conflict
    if actor!='admin' or worker not in MINI_VM_WORKERS:raise Invalid('Administrator and exact Mini worker required')
    try:
        need(exact(body,'policy expected_revision') and (body['expected_revision'] is None or hexid(body['expected_revision'],64)),'Exact eligibility policy CAS required')
        policy=body['policy'];validate_policy(policy);need(policy['worker']==worker and store.clock()<policy['expires_at']<=store.clock()+86400,'Bounded current policy expiry required')
        with store.transaction() as db:
            previous=db.execute('SELECT * FROM resource_scope_policies WHERE worker=?',(worker,)).fetchone()
            need((previous['revision'] if previous else None)==body['expected_revision'],'Eligibility policy revision changed')
            need(not db.execute("SELECT id FROM resource_scope_runs WHERE worker=? AND status IN ('reserved','claimed')",(worker,)).fetchone(),'Preserve active scoped ownership')
            enrolled=db.execute('SELECT * FROM dispatch_workers WHERE worker=?',(worker,)).fetchone();need(enrolled and enrolled['enabled'],'Existing qualified CLI enrollment required')
            need(enrolled['availability']!='available','Generic availability contradicts a held resource policy')
            for held in policy['held']:
                wake=db.execute("SELECT * FROM dispatch_wakes WHERE worker=? AND task_id=? AND run_id=? AND status='claimed'",(worker,held['task'],held['run_id'])).fetchone()
                need(wake and wake['claim_generation']==held['generation'],'Exact retained claimed wake must remain held')
            verify_held(store,db,policy);revision=digest(policy)
            db.execute('INSERT INTO resource_scope_policies VALUES(?,?,?,?,?) ON CONFLICT(worker) DO UPDATE SET revision=excluded.revision,document=excluded.document,enabled=1,created=excluded.created',(worker,revision,encoded(policy),1,store.clock()))
            store.event(db,'admin','resource-scope-policy',None,encoded({'worker':worker,'revision':revision,'held_tasks':[h['task'] for h in policy['held']]}))
            return {'revision':revision,'generic_availability_changed':False,'held_wakes_acked':[]}
    except (ValueError,TypeError,KeyError) as error:raise Conflict(str(error)) from None


def candidate(store,db,worker,task_id,revision):
    entry=db.execute('SELECT * FROM resource_scope_policies WHERE worker=?',(worker,)).fetchone()
    need(entry and entry['enabled'] and entry['revision']==revision,'Resource policy missing or changed')
    policy=json.loads(entry['document']);need(store.clock()<policy['expires_at'],'Resource policy expired')
    validate_policy(policy);verify_held(store,db,policy)
    own=db.execute('SELECT * FROM workers WHERE id=?',(worker,)).fetchone()
    need(own and not own['paused'] and own['seen']>store.clock()-90,'Fresh unpaused own capacity required')
    need(not db.execute("SELECT id FROM tasks WHERE owner=? AND status IN ('running','held')",(worker,)).fetchone(),'An active owned service task prevents another runtime')
    need(not db.execute("SELECT id FROM resource_scope_runs WHERE worker=? AND status IN ('reserved','claimed')",(worker,)).fetchone(),'Scoped runtime already reserved or unknown')
    need(not db.execute("SELECT id FROM dispatch_wakes WHERE task_id=? AND status!='settled'",(task_id,)).fetchone(),'Task has an unsettled normal wake; cross-worker handoff is not clearance')
    task=task_snapshot(store,db,task_id);scope=declaration(task,policy)
    row=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    need(store.fits(row,own) and store.assignment_allowed(row,worker) and store.scheduling(db,row)['ready'],'Task capacity/dependencies/assignment not eligible')
    return policy,task,scope


def reserve(store,worker,body):
    from .store import Invalid,Conflict
    if worker not in MINI_VM_WORKERS:raise Invalid('Exact Mini worker required')
    try:
        need(exact(body,'id run_id task_id queued_generation policy_revision task_sha256 token_hash local_fence_sha256')
             and all(hexid(body[k],32) for k in ('id','run_id')) and hexid(body['task_id'],16)
             and all(hexid(body[k],64) for k in ('policy_revision','task_sha256','token_hash','local_fence_sha256'))
             and type(body['queued_generation']) is int,'Exact scoped reservation binding required')
        with store.transaction() as db:
            need(not db.execute('SELECT id FROM resource_scope_runs WHERE id=? OR run_id=?',(body['id'],body['run_id'])).fetchone(),'Scoped reservation is one-shot; inspect existing state')
            policy,task,scope=candidate(store,db,worker,body['task_id'],body['policy_revision'])
            need(task['generation']==body['queued_generation'] and task_fingerprint(task)==body['task_sha256']
                 and policy['local_fence_sha256']==body['local_fence_sha256'],'Local/API/resource fence changed')
            db.execute("INSERT INTO resource_scope_runs(id,worker,task_id,queued_generation,run_id,policy_revision,task_sha256,token_hash,local_fence_sha256,status,created,expires) VALUES(?,?,?,?,?,?,?,?,?,'reserved',?,?)",(body['id'],worker,task['id'],task['generation'],body['run_id'],body['policy_revision'],body['task_sha256'],body['token_hash'],body['local_fence_sha256'],store.clock(),min(store.clock()+90,policy['expires_at'])))
            store.event(db,worker,'resource-scope-reserved',None,encoded({'id':body['id'],'task':task['id'],'run':body['run_id']}))
            return {'id':body['id'],'status':'reserved','scope':scope,'generic_available':False,'wakeACKAuthorized':False,'resourceReleaseAuthorized':False}
    except (ValueError,TypeError,KeyError) as error:raise Conflict(str(error)) from None


def claim_allowed(store,db,row,worker,binding):
    if not (exact(binding,'lane id run_id token') and binding['lane']=='resource-scoped' and hexid(binding['id'],32) and hexid(binding['run_id'],32) and type(binding['token']) is str and 32<=len(binding['token'])<=128):return False
    entry=db.execute('SELECT * FROM resource_scope_runs WHERE id=?',(binding['id'],)).fetchone()
    if not entry or entry['worker']!=worker or entry['task_id']!=row['id'] or entry['run_id']!=binding['run_id'] or entry['status']!='reserved' or entry['expires']<=store.clock() or entry['queued_generation']!=row['generation']:return False
    if not secrets.compare_digest(entry['token_hash'],hashlib.sha256(binding['token'].encode()).hexdigest()):return False
    policy_row=db.execute('SELECT * FROM resource_scope_policies WHERE worker=?',(worker,)).fetchone()
    try:
        need(policy_row and policy_row['enabled'] and policy_row['revision']==entry['policy_revision'],'Policy changed')
        policy=json.loads(policy_row['document']);need(store.clock()<policy['expires_at'],'Policy expired');validate_policy(policy);verify_held(store,db,policy)
        task=task_snapshot(store,db,row['id']);declaration(task,policy)
        need(task_fingerprint(task)==entry['task_sha256'],'Target API/history changed')
        need(not db.execute("SELECT id FROM dispatch_wakes WHERE task_id=? AND status!='settled'",(row['id'],)).fetchone(),'Normal wake collision')
        return True
    except (ValueError,TypeError,KeyError):return False


def claimed(store,db,row,worker,generation):
    db.execute("UPDATE resource_scope_runs SET status='claimed',claim_generation=? WHERE worker=? AND task_id=? AND status='reserved'",(generation,worker,row['id']))


def complete(store,worker,body):
    from .store import Conflict
    try:
        need(exact(body,'id run_id token generation result_sha256 own_scope_stopped unknown_operations unsettled_operations unreceived_message_ids local_lock_released')
             and type(body['generation']) is int and body['own_scope_stopped'] is True and body['local_lock_released'] is True
             and all(body[k]==[] for k in ('unknown_operations','unsettled_operations','unreceived_message_ids')) and hexid(body['result_sha256'],64),'Actual own scope settlement required')
        with store.transaction() as db:
            entry=db.execute('SELECT * FROM resource_scope_runs WHERE id=?',(body['id'],)).fetchone()
            need(entry and entry['worker']==worker and entry['run_id']==body['run_id'] and entry['claim_generation']==body['generation']
                 and entry['status']=='claimed' and type(body['token']) is str and secrets.compare_digest(entry['token_hash'],hashlib.sha256(body['token'].encode()).hexdigest()),'Exact claimed scope owner/token required')
            task=task_snapshot(store,db,entry['task_id'])
            need(task['generation']==body['generation'] and task['status']=='completed'
                 and task.get('result',{}).get('execution_stopped') is True
                 and task.get('result',{}).get('acceptance_complete') is True,'Actual settled owning task API receipt required')
            from .cli_results import clean
            clean([task['result'],task['result'].get('cleanup',{})])
            db.execute("UPDATE resource_scope_runs SET status='completed',result_sha256=? WHERE id=?",(body['result_sha256'],entry['id']))
            store.event(db,worker,'resource-scope-completed',None,encoded({'id':entry['id'],'task':entry['task_id'],'result_sha256':body['result_sha256']}))
            return {'scope_completed':True,'generic_available':False,'held_wakes_acked':[],'resourceReleaseAuthorized':False}
    except (ValueError,TypeError,KeyError) as error:raise Conflict(str(error)) from None


def projection(store,db,worker):
    entry=db.execute('SELECT * FROM resource_scope_policies WHERE worker=?',(worker,)).fetchone()
    if not entry:return None
    policy=json.loads(entry['document']);eligible=[];issue=None
    if entry['enabled'] and store.clock()<policy['expires_at']:
        try:
            verify_held(store,db,policy)
            for row in db.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY priority DESC,created,id LIMIT 128"):
                try:candidate(store,db,worker,row['id'],entry['revision']);eligible.append(row['id'])
                except (ValueError,TypeError,KeyError):continue
        except (ValueError,TypeError,KeyError) as error:issue=str(error)
    else:issue='disabled-or-expired'
    return {'enabled':bool(entry['enabled']),'revision':entry['revision'],'expires_at':policy['expires_at'],
            'eligible_task_ids':eligible,'generic_available':False,'wakeACKAuthorized':False,'resourceReleaseAuthorized':False,'issue':issue}


def active(db,worker):
    return db.execute("SELECT id FROM resource_scope_runs WHERE worker=? AND status IN ('reserved','claimed')",(worker,)).fetchone() is not None
