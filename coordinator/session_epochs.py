"""Explicit session abandonment is not cleanup. Old resources stay quarantined."""
import json
from . import resource_scope as scope
from .workers import MINI_VM_WORKERS

SCHEMA='''
CREATE TABLE IF NOT EXISTS epoch_task_admissions(task_id TEXT PRIMARY KEY,worker TEXT NOT NULL,epoch TEXT NOT NULL,semantics_sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS worker_epochs(worker TEXT PRIMARY KEY,epoch TEXT NOT NULL,policy TEXT NOT NULL,archive TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS worker_epoch_resets(id TEXT PRIMARY KEY,worker TEXT NOT NULL,request_sha256 TEXT NOT NULL,document TEXT NOT NULL,response TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS worker_quarantines(task_id TEXT PRIMARY KEY,worker TEXT NOT NULL,epoch TEXT NOT NULL,generation INTEGER NOT NULL,original TEXT NOT NULL,approval TEXT NOT NULL,archive_sha256 TEXT NOT NULL);
'''

def current(db,worker):
    return db.execute('SELECT * FROM worker_epochs WHERE worker=?',(worker,)).fetchone()

def snapshot(store,db,worker):
    own=db.execute('SELECT * FROM workers WHERE id=?',(worker,)).fetchone();scope.need(own is not None,'Unknown worker')
    wakes=[dict(r) for r in db.execute("SELECT * FROM dispatch_wakes WHERE worker=? AND status NOT IN ('settled','quarantined') ORDER BY id",(worker,))]
    scoped=[dict(r) for r in db.execute("SELECT * FROM resource_scope_runs WHERE worker=? AND status IN ('reserved','claimed') ORDER BY id",(worker,))]
    ids={w['task_id'] for w in wakes+scoped}|{r['id'] for r in db.execute("SELECT id FROM tasks WHERE owner=? AND status IN ('running','held')",(worker,))}
    tasks=[scope.task_snapshot(store,db,ident) for ident in sorted(ids)]
    config=db.execute('SELECT * FROM dispatch_workers WHERE worker=?',(worker,)).fetchone()
    epoch=current(db,worker)
    return {'worker':worker,'paused':bool(own['paused']),'epoch':epoch['epoch'] if epoch else None,
            'dispatch':dict(config) if config else None,'wakes':wakes,'scoped':scoped,'tasks':tasks}

def plan(store,worker):
    from .store import Conflict
    try:
        scope.need(worker in MINI_VM_WORKERS,'Exact enrolled Mini required')
        with store.transaction() as db:
            value=snapshot(store,db,worker)
            return {'worker':worker,'expected_epoch':value['epoch'],'fingerprint':scope.digest(value),
                    'paused':value['paused'],'held':[{'task':t['id'],'generation':t['generation'],'api_sha256':scope.task_fingerprint(t)} for t in value['tasks']],
                    'wake_ids':[w['id'] for w in value['wakes']],'scope_ids':[w['id'] for w in value['scoped']],
                    'retained_resources':retained_resources(db,worker),
                    'quarantined_tasks':[r['task_id'] for r in db.execute('SELECT task_id FROM worker_quarantines WHERE worker=? ORDER BY task_id',(worker,))],
                    'unknowns':[{'source':t['id'],**u} for t in [*value['tasks'],*[json.loads(r['original'])['task'] for r in db.execute('SELECT original FROM worker_quarantines WHERE worker=?',(worker,))]] for u in unknown_inventory(t)],
                    'execution_stopped':False,'wakeACKAuthorized':False,'resourceReleaseAuthorized':False}
    except ValueError as error:raise Conflict(str(error)) from None

def validate_policy(policy,worker,held):
    scope.need(scope.exact(policy,'schema worker held resources allowed_workspaces allowed_kinds allowed_capabilities unknown_effects_bounded unknown_bindings') and type(policy['schema']) is int and policy['schema']==1 and policy['worker']==worker,'Exact restricted epoch policy required')
    scope.need(policy['held']==held and type(policy['unknown_effects_bounded']) is bool,'Quarantined task inventory and bounded effects review required')
    scope.need(type(policy['unknown_bindings']) is list,'Explicit retained unknown bindings required')
    for binding in policy['unknown_bindings']:
        scope.need(scope.exact(binding,'source pointer value_sha256 resource_indexes effects_bounded') and type(binding['source']) is str and type(binding['pointer']) is str and scope.hexid(binding['value_sha256'],64) and type(binding['effects_bounded']) is bool and type(binding['resource_indexes']) is list and binding['resource_indexes'] and all(type(i) is int and 0<=i<len(policy['resources']) for i in binding['resource_indexes']),'Unknown/unbounded effects remain ineligible')
    scope.need(type(policy['resources']) is list and 0<len(policy['resources'])<=32,'Explicit held native/resource inventory required')
    for resource in policy['resources']:scope.resource(resource,held=True)
    scope.need(any(r['kind']=='capability' and r['value']=='native-ui' for r in policy['resources']),'Native effects remain held')
    scope.need(type(policy['allowed_workspaces']) is list and 0<len(policy['allowed_workspaces'])<=8,'Explicit disjoint workspaces required')
    for root in policy['allowed_workspaces']:
        scope.path(root);scope.need(not any(scope.intersects({'kind':'path','value':root,'access':'write'},r) for r in policy['resources']),'Workspace intersects held resources')
    scope.need(type(policy['allowed_kinds']) is list and policy['allowed_kinds'] and set(policy['allowed_kinds'])<=set(scope.SAFE_OPS),'Only source/inert/media work allowed')
    scope.need(type(policy['allowed_capabilities']) is list and policy['allowed_capabilities'] and set(policy['allowed_capabilities'])<=scope.SAFE_CAPS|{'cli-media-v1'},'Only known CLI/media/source capabilities allowed')

def apply(store,actor,worker,body):
    from .store import Conflict
    try:
        scope.need(actor=='admin' and worker in MINI_VM_WORKERS,'Administrator and exact Mini required')
        scope.need(scope.exact(body,'id worker expected_epoch fingerprint archive decision policy local_admission') and body['worker']==worker and scope.hexid(body['id'],32) and scope.hexid(body['fingerprint'],64),'Exact reset identity and CAS required')
        scope.need(body['expected_epoch'] is None or scope.hexid(body['expected_epoch'],32),'Exact previous epoch required')
        archive=body['archive'];decision=body['decision'];local=body['local_admission']
        scope.need(scope.exact(archive,'schema kind worker reset_id original_root archive_root manifest verified entry_count total_file_bytes') and type(archive['schema']) is int and archive['schema']==1 and archive['kind']=='worker-session-archive' and archive['worker']==worker and archive['reset_id']==body['id'] and archive['verified'] is True and scope.reference(archive['manifest']) and type(archive['entry_count']) is int and 0<archive['entry_count']<=8192 and type(archive['total_file_bytes']) is int and 0<=archive['total_file_bytes']<=2*1024**3,'Actual immutable archive binding required')
        old,new=scope.path(archive['original_root']),scope.path(archive['archive_root']);scope.need(old!=new and old not in new.parents and new not in old.parents,'Archive must be separate from active root')
        scope.need(scope.exact(decision,'source response scope verified') and scope.exact(decision['source'],'thread_id message_id') and all(type(v) is str and v.strip() for v in decision['source'].values()) and type(decision['response']) is str and 0<len(decision['response'])<=4000 and decision['scope']=='archive-active-session-only' and decision['verified'] is True,'Actual primary human session-abandonment decision required')
        scope.need(scope.exact(local,'worker_lock_held listener_lock_held agent_stopped') and all(v is True for v in local.values()),'Active local worker/listener prevents reset')
        fingerprint=scope.digest(body)
        with store.transaction() as db:
            previous=db.execute('SELECT * FROM worker_epoch_resets WHERE id=?',(body['id'],)).fetchone()
            if previous:
                scope.need(previous['worker']==worker and previous['request_sha256']==fingerprint,'Reset ID reused with another request')
                return {**json.loads(previous['response']),'duplicate':True,'active_epoch':current(db,worker)['epoch']}
            before=snapshot(store,db,worker)
            scope.need(before['paused'] and before['epoch']==body['expected_epoch'] and scope.digest(before)==body['fingerprint'],'Worker/wake/generation/history CAS changed or worker is not paused')
            held=[{'task':t['id']} for t in before['tasks']]
            existing=[{'task':r['task_id']} for r in db.execute('SELECT task_id FROM worker_quarantines WHERE worker=? ORDER BY task_id',(worker,))]
            all_held=sorted(existing+held,key=lambda r:r['task']);scope.need(len({h['task'] for h in all_held})==len(all_held),'Already quarantined task cannot be reset again as active')
            validate_policy(body['policy'],worker,all_held)
            previous_epoch=current(db,worker)
            if previous_epoch:
                prior_resources=retained_resources(db,worker)
                scope.need({scope.encoded(r) for r in prior_resources}<={scope.encoded(r) for r in body['policy']['resources']},'Prior held resources cannot be removed by reset')
            originals=[*before['tasks'],*[json.loads(r['original'])['task'] for r in db.execute('SELECT original FROM worker_quarantines WHERE worker=?',(worker,))]]
            expected=[{'source':t['id'],**u} for t in originals for u in unknown_inventory(t)]
            actual=[{k:r[k] for k in ('source','pointer','value_sha256')} for r in body['policy']['unknown_bindings'] if not r['source'].startswith('archive:')]
            scope.need(sorted(actual,key=scope.encoded)==sorted(expected,key=scope.encoded),'Every API unknown must remain explicitly resource-bound')
            protected=[{'kind':'path','value':str(path),'access':'write'} for path in (old,new)]
            for root in body['policy']['allowed_workspaces']:
                scope.need(not any(scope.intersects({'kind':'path','value':root,'access':'write'},r) for r in protected),'Workspace intersects archived/active records')
            # Positive authorization abandons a session; it never asserts cleanup.
            for task in before['tasks']:
                row=db.execute('SELECT * FROM tasks WHERE id=?',(task['id'],)).fetchone()
                scope.need(row['owner'] in (None,worker) and row['last_owner'] in (None,worker),'Foreign owning task prevents reset')
                db.execute('INSERT INTO worker_quarantines VALUES(?,?,?,?,?,?,?)',(task['id'],worker,body['id'],task['generation'],scope.encoded({'task':task,'row':dict(row)}),scope.encoded(task.get('approval')),archive['manifest']['sha256']))
                if row['status'] not in ('completed','cancelled'):
                    db.execute("UPDATE tasks SET status='abandoned_unverified',owner=NULL,last_owner=COALESCE(owner,last_owner),claim_hash=NULL,lease_until=NULL WHERE id=?",(task['id'],))
                store.event(db,'admin','session-abandoned-unverified',task['id'],scope.encoded({'epoch':body['id'],'archive_sha256':archive['manifest']['sha256'],'original_generation':task['generation'],'cleanup_verified':False,'decision_source':decision['source']}))
            db.execute("UPDATE dispatch_wakes SET status='quarantined' WHERE worker=? AND status NOT IN ('settled','quarantined')",(worker,))
            db.execute("UPDATE resource_scope_runs SET status='quarantined' WHERE worker=? AND status IN ('reserved','claimed')",(worker,))
            db.execute('UPDATE resource_scope_policies SET enabled=0 WHERE worker=?',(worker,))
            db.execute("UPDATE dispatch_workers SET enabled=0,availability='unknown',observed_at=0 WHERE worker=?",(worker,))
            db.execute('INSERT INTO worker_epochs VALUES(?,?,?,?,?) ON CONFLICT(worker) DO UPDATE SET epoch=excluded.epoch,policy=excluded.policy,archive=excluded.archive,created=excluded.created',(worker,body['id'],scope.encoded(body['policy']),scope.encoded(archive),store.clock()))
            response={'recorded':True,'worker':worker,'epoch':body['id'],'active_epoch':body['id'],'quarantined_tasks':[h['task'] for h in all_held],'paused':True,'enrolled':False,'execution_stopped':False,'wakeACKAuthorized':False,'resourceReleaseAuthorized':False,'duplicate':False}
            db.execute('INSERT INTO worker_epoch_resets VALUES(?,?,?,?,?,?)',(body['id'],worker,fingerprint,scope.encoded({'request':body,'before':before}),scope.encoded(response),store.clock()))
            store.event(db,'admin','worker-session-reset',None,scope.encoded({'worker':worker,'epoch':body['id'],'archive_sha256':archive['manifest']['sha256'],'decision_source':decision['source']}))
            return response
    except (ValueError,TypeError,KeyError) as error:raise Conflict(str(error)) from None

def protocol(store,worker,epoch):
    with store.connection() as db:
        row=current(db,worker)
        if row is None:return epoch is None
        return epoch==row['epoch']

def admit(store,db,worker,task):
    """Fresh work uses a reviewed disjoint footprint, not old owned-wait proof."""
    if db.execute('SELECT task_id FROM worker_quarantines WHERE task_id=?',(task['id'],)).fetchone():return False
    entry=current(db,worker)
    if not entry:return True
    try:
        policy=json.loads(entry['policy'])
        if task.get('created',0)<=entry['created']:
            adopted=db.execute('SELECT * FROM epoch_task_admissions WHERE task_id=?',(task['id'],)).fetchone()
            scope.need(adopted and adopted['worker']==worker and adopted['epoch']==entry['epoch'] and adopted['semantics_sha256']==semantics(task),'Historical task has not been explicitly scoped into this epoch')
        if task.get('status')=='blocked':
            from .cli_results import clean
            clean([task.get('result',{})]);task={**task,'status':'queued','owner':None}
        declaration(task,entry)
        return True
    except (ValueError,TypeError,KeyError):return False

def public_quarantine(db,task_id):
    row=db.execute('SELECT * FROM worker_quarantines WHERE task_id=?',(task_id,)).fetchone()
    if not row:return None
    return {'state':'abandoned-unverified','epoch':row['epoch'],'generation':row['generation'],'archive_sha256':row['archive_sha256'],'original_approval':json.loads(row['approval']),'cleanup_verified':False,'resourceReleaseAuthorized':False}


def semantics(task):
    return scope.digest({k:task.get(k) for k in ('id','title','description','steps','requirements','dependencies','task_type','compute')})


def scope_task(store,actor,ident,body):
    from .store import Conflict
    try:
        scope.need(actor=='admin' and scope.exact(body,'worker epoch task_sha256 resource_scope evidence') and type(body['evidence']) is str and 0<len(body['evidence'])<=2000,'Exact administrative task scope required')
        with store.transaction() as db:
            task=scope.task_snapshot(store,db,ident);entry=current(db,body['worker'])
            scope.need(entry and entry['epoch']==body['epoch'] and scope.task_fingerprint(task)==body['task_sha256'],'Task/history/epoch changed')
            scope.need(not db.execute('SELECT task_id FROM worker_quarantines WHERE task_id=?',(ident,)).fetchone(),'Original quarantined task cannot be adopted')
            requirements={**task['requirements'],'resource_scope':body['resource_scope'],'capabilities':body['resource_scope']['required_capabilities']}
            scope.need(set(task['requirements'].get('capabilities',[]))<=set(requirements['capabilities']),'Existing capability requirements cannot be removed')
            target={**task,'requirements':requirements};declaration(target,entry)
            db.execute('UPDATE tasks SET requirements=? WHERE id=?',(scope.encoded(requirements),ident))
            db.execute('INSERT INTO epoch_task_admissions VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET worker=excluded.worker,epoch=excluded.epoch,semantics_sha256=excluded.semantics_sha256',(ident,body['worker'],body['epoch'],semantics(target)))
            store.event(db,'admin','task-session-scoped',ident,scope.encoded({'epoch':body['epoch'],'requirements':requirements,'evidence':body['evidence']}))
            return {'task':ident,'epoch':body['epoch'],'requirements':requirements,'permission_granted':False}
    except (ValueError,TypeError,KeyError) as error:raise Conflict(str(error)) from None


def declaration(task,entry):
    archive=json.loads(entry['archive']);policy=json.loads(entry['policy'])
    if task.get('status')=='blocked':
        from .cli_results import clean
        scope.need(task.get('result',{}).get('execution_stopped') is True,'Retry requires actually stopped own execution')
        clean([task['result']]);task={**task,'status':'queued','owner':None}
    scope.need(policy['unknown_effects_bounded'] is True and all(b['effects_bounded'] is True for b in policy['unknown_bindings']),'Unbounded unknown effects prohibit fresh work')
    fresh=scope.path(archive['original_root'])/'epochs'/entry['epoch']
    result=scope.declaration(task,policy,media_root=str(fresh))
    for resource in result['resources']:
        if resource['kind'] not in ('path','media'):continue
        value=scope.path(resource['value'] if resource['kind']=='path' else resource['path'])
        in_fresh=value==fresh or fresh in value.parents
        scope.need(not scope.intersects(resource,{'kind':'path','value':archive['archive_root'],'access':'write'}),'Archive resource intersects task')
        scope.need(in_fresh or not scope.intersects(resource,{'kind':'path','value':archive['original_root'],'access':'write'}),'Old active namespace cannot be reused')
    return result


def retained_resources(db,worker):
    entry=current(db,worker)
    if not entry:return []
    resources=json.loads(entry['policy'])['resources']
    archive={'kind':'path','value':json.loads(entry['archive'])['archive_root'],'access':'write'}
    return resources if archive in resources else [*resources,archive]


def unknown_inventory(value,pointer=''):
    """Preserve old stop facts too; archiving a stop is not lifting its scope."""
    rows=[]
    if type(value) is dict:
        for key,item in value.items():
            child=pointer+'/'+key.replace('~','~0').replace('/','~1')
            if key in scope.UNKNOWN_KEYS:
                scope.need(type(item) is list,'Explicit retained unknown array required')
                rows.extend({'pointer':child+'/'+str(i),'value_sha256':scope.digest(v)} for i,v in enumerate(item))
            elif key in ('human_stop','stop_requested','global_stop') and item is True:
                rows.append({'pointer':child,'value_sha256':scope.digest(item)})
            else:rows.extend(unknown_inventory(item,child))
    elif type(value) is list:
        for i,item in enumerate(value):rows.extend(unknown_inventory(item,pointer+'/'+str(i)))
    return rows
