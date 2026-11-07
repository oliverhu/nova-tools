"""Locked resource-scoped local admission. No original run is settled or ACKed."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import time

from . import cli_recovery as records
from . import resource_scope as scope
from .cli_results import read_original


def load(reference):return records.bound(reference)[0]


def validate_local(root,worker,policy,client,exclude=None):
    """Recheck actual retained bytes/facts, not a cleanup boolean or process absence."""
    scope.validate_policy(policy);scope.need(policy['worker']==worker and policy['uid']==os.getuid() and time.time()<policy['expires_at'],'Local policy worker/UID/expiry differs')
    records.private(root)
    held_ids={h['run_id'] for h in policy['held']}
    paths=list(root.glob('*/state.json'));scope.need(len(paths)<=128,'Bounded exact local run inventory required')
    for path in paths:
        value=json.loads(read_original(path)[0]);scope.need(value.get('worker')==worker,'Foreign local runtime')
        if path.parent.name in held_ids or path.parent.name==exclude:continue
        scope.need(value.get('status') in ('completed','continued','settled','scope-settled'),'Unlisted active/unknown local runtime prevents scoped work')
        if value.get('status')=='scope-settled':
            proof=json.loads(read_original(path.parent/'scoped-run-result.json')[0])
            scope.need(proof.get('own_scope_stopped') is True and proof.get('outcome')=='completed','Prior scoped task is not actually settled')
    actual_unknowns=[];fence={};held_api={}
    for held in policy['held']:
        directory=root/held['run_id'];records.private(directory)
        for name,digest in held['originals'].items():
            value,raw,identity=records.bound({'path':str(directory/name),'sha256':digest})
            fence[str(directory/name)]={'sha256':hashlib.sha256(raw).hexdigest(),'identity':identity}
            actual_unknowns += [{'source':name,**row} for row in scope.unknowns(value)]
            if name=='state.json':
                scope.need(value.get('run_id')==held['run_id'] and value.get('task')==held['task'] and value.get('worker')==worker
                           and type(value.get('queued_generation')) is int and value['queued_generation']+1==held['generation']
                           and type(value.get('cli_exit_code')) is int
                           and all(value.get(k) is not None for k in ('created_at','ended_at'))
                           and all(value['created_at']<=p['not_before_epoch']<=p['not_after_epoch']<=value['ended_at'] for p in policy['owned_settlement']),'Actual owning run/generation/exit record required; no absence inference')
        task=client.request('/api/tasks/'+held['task']);held_api[held['task']]=task
        scope.need(scope.task_fingerprint(task)==held['api_sha256'],'Fresh held API/history differs')
        actual_unknowns += [{'source':'fresh-api',**row} for row in scope.unknowns(task)]
        expected=[{k:row[k] for k in ('source','pointer','value_sha256')} for row in held['unknown_bindings']]
        # Per-held comparison; never erase duplicates or an original unknown.
        current=[row for row in actual_unknowns]
        scope.need(sorted(current,key=scope.encoded)==sorted(expected,key=scope.encoded),'Every retained and current unknown must remain explicitly resource-bound')
        actual_unknowns=[]
    raw_sources={}
    for reference in policy['proof_sources']:
        value,raw,identity=records.bound(reference);raw_sources[reference['path']]=value;fence[reference['path']]={'sha256':hashlib.sha256(raw).hexdigest(),'identity':identity}
    for proof in policy['owned_settlement']:
        facts={**proof['facts'],**{key:proof[key] for key in ('role','run_id','task','generation','identity','waiter','recorded_at','observer_source_sha256')}}
        for fact,expected in facts.items():
            where=proof['provenance'][fact];actual=records.select(raw_sources[where['reference']['path']],where['pointer'])
            if fact in ('identity','waiter') and type(actual) is dict:
                scope.need(set(expected)<=set(actual),'Complete owned generation missing from raw proof')
                actual={k:actual[k] for k in expected}
            scope.need(scope.encoded(actual)==scope.encoded(expected),'Owned settlement fact differs from actual raw receipt')
        scope.need(proof['recorded_at']<=time.time(),'Owned proof cannot be future-dated')
    protected=[{'kind':'path','value':str(root),'access':'write'}]+[{'kind':'path','value':ref['path'],'access':'read'} for ref in policy['proof_sources']]
    for workspace in policy['allowed_workspaces']:
        scope.need(not any(scope.intersects({'kind':'path','value':workspace,'access':'write'},r) for r in protected),'Workspace intersects retained evidence')
        scope.need(Path(workspace).resolve()==Path(workspace),'Workspace alias/symlink is not a disjoint resource')
    actual_fence=scope.digest(fence);scope.need(actual_fence==policy['local_fence_sha256'],'Original/proof local fence changed')
    for task_id,before in held_api.items():
        scope.need(scope.task_fingerprint(client.request('/api/tasks/'+task_id))==scope.task_fingerprint(before),'Held history changed while checking local proof')
    for source,expected in fence.items():
        _,_,identity=records.bound({'path':source,'sha256':expected['sha256']})
        scope.need(tuple(expected['identity'])==identity,'Evidence identity changed during scoped admission')
    return actual_fence


def policy_from_service(client,worker,revision):
    value=client.request('/api/worker/resource-scope/policy')
    scope.need(value.get('worker')==worker and value.get('revision')==revision,'Exact enrolled resource policy required')
    return value['policy']


def qualified_sources(config):
    from . import cli_dispatch,cli_event_dispatch,cli_messages,client,workers,cli_results
    required={str(Path(module.__file__).resolve()) for module in (scope,records,cli_dispatch,cli_event_dispatch,cli_messages,client,workers,cli_results)}|{str(Path(__file__).resolve())}
    scope.need(config.get('resource_scope',{}).get('enabled') is True,'Separate scoped source/readiness enrollment required')
    sources=config['resource_scope'].get('sources')
    scope.need(type(sources) is dict and required<=set(sources) and all(Path(p).resolve()==Path(p) and hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in sources.items()),'Qualified scoped implementation/instructions changed')


def file_digest(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def validate_resources(declaration,root,policy):
    protected=[{'kind':'path','value':str(root),'access':'write'}]+[{'kind':'path','value':ref['path'],'access':'read'} for ref in policy['proof_sources']]
    for resource in declaration['resources']:
        scope.need(not any(scope.intersects(resource,r) for r in protected),'Task intersects retained evidence')
        if resource['kind'] in ('path','media'):
            value=resource['value'] if resource['kind']=='path' else resource['path']
            scope.need(Path(value).resolve()==Path(value),'Aliased task resource is not disjoint')
            if resource['kind']=='media':scope.need(file_digest(Path(value))==resource['sha256'],'Media content changed')

def wake(config,root,task_id,revision,client,lock):
    from . import cli_dispatch as adapter
    policy=policy_from_service(client,config['worker'],revision)
    qualified_sources(config)
    fence=validate_local(root,config['worker'],policy,client)
    state=client.request('/api/state');task=client.request('/api/tasks/'+task_id)
    declaration=scope.declaration(task,policy)
    validate_resources(declaration,root,policy)
    adapter.validate_task(task,state,config,scoped=True)
    scope.need(Path(declaration['workspace']).is_dir() and Path(declaration['workspace']).resolve()==Path(declaration['workspace']),'Existing isolated task workspace required')
    account,models,limits=adapter.rpc_account(config['codex']);account_digest=adapter.check_account(account,models,limits,config)
    # Fences are checked again after account preflight, before any reservation/model.
    scope.need(validate_local(root,config['worker'],policy,client)==fence,'Local scoped fence changed')
    fresh=client.request('/api/tasks/'+task_id);scope.need(scope.task_fingerprint(fresh)==scope.task_fingerprint(task),'Target requirements/history changed')
    run_id=secrets.token_hex(16);directory=root/run_id;directory.mkdir(mode=0o700)
    authorization={'lane':'resource-scoped','id':secrets.token_hex(16),'run_id':run_id,'token':secrets.token_urlsafe(32)}
    record={'run_id':run_id,'task':task_id,'queued_generation':task['generation'],'worker':config['worker'],'status':'starting','created_at':time.time(),
            'model':config['model'],'reasoning':config['reasoning'],'account_email_sha256':account_digest,'plan_type':account.get('planType'),
            'resource_scope':{'task_sha256':scope.task_fingerprint(task),'revision':revision,'policy':policy,'declaration':declaration,'local_fence_sha256':fence,'authorization':{k:v for k,v in authorization.items() if k!='token'}}}
    adapter.save(directory/'wake-authorization.json',authorization)
    adapter.save(directory/'state.json',record)
    # Durable intent is never replayed after ambiguous delivery.
    adapter.save(directory/'scope-reservation-intent.json',{'task':task_id,'generation':task['generation'],'revision':revision,'local_fence_sha256':fence})
    response=client.request('/api/worker/resource-scope/reserve',{'id':authorization['id'],'run_id':run_id,'task_id':task_id,'queued_generation':task['generation'],
        'policy_revision':revision,'task_sha256':scope.task_fingerprint(task),'token_hash':hashlib.sha256(authorization['token'].encode()).hexdigest(),'local_fence_sha256':fence})
    adapter.save(directory/'scope-reservation-response.json',response)
    scope.need(response.get('status')=='reserved' and response.get('id')==authorization['id'],'Scoped reservation unknown; no model or replay')
    adapter.save(directory/'state.json',record)
    text=adapter.prompt(config,task,directory)+('\nRESOURCE-SCOPED TASK ONLY. Generic worker/native holds remain unchanged.\n'
        'Complete acceptance must remain within this exact declaration; an intersecting/unknown operation requires stop and truthful wait, never a broader actor or tool.\n'
        'Do not access original held run/candidate/PID resources, native UI, browser, signing, consent, TCC, publication or undisclosed effects.\n'
        'No unfiltered next, generic capacity report, original wake ACK, old state rewrite or resource release.\n'
        'Use only the isolated workspace and declared source/inert/media operations/resources. Existing normal tool review boundaries still apply.\n'
        +scope.encoded(declaration)+'\n')
    (directory/'prompt.txt').write_text(text)
    adapter.spawn(record,directory,lock.fileno())
    return {**record,'generic_available':False,'wakeACKAuthorized':False,'resourceReleaseAuthorized':False}


def before_execute(config,root,record,client):
    binding=record['resource_scope'];policy=policy_from_service(client,config['worker'],binding['revision'])
    scope.need(scope.digest(policy)==scope.digest(binding['policy']),'Scoped policy changed before runtime')
    validate_local(root,config['worker'],policy,client,exclude=record['run_id'])
    qualified_sources(config)
    task=client.request('/api/tasks/'+record['task']);declaration=scope.declaration(task,policy)
    scope.need(scope.task_fingerprint(task)==binding['task_sha256'] and declaration==binding['declaration'],'Target changed before model launch')
    validate_resources(declaration,root,policy)
    return declaration


def finalize_document(document,record):
    # The normal reducer verified this task only, never the worker's held runs.
    return {**document,'availability':'scope-only','own_scope_stopped':document['execution_stopped'],
            'generic_available':False,'wakeACKAuthorized':False,'resourceReleaseAuthorized':False,
            'resource_scope_revision':record['resource_scope']['revision']}


def completed(client,config,directory):
    """Close this one successful scope, without ACKing any normal/held wake."""
    from .cli_dispatch import save
    record=json.loads(read_original(directory/'state.json')[0])
    result_raw=read_original(directory/'scoped-run-result.json')[0];result=json.loads(result_raw)
    scope.need(record.get('status')=='scope-settled' and result.get('own_scope_stopped') is True
               and result.get('outcome')=='completed' and result.get('acceptance_complete') is True,'Scoped failure/approval/unknown stays reserved; no automatic continuation')
    authorization=json.loads(read_original(directory/'wake-authorization.json')[0]);authorization.pop('lane')
    body={**authorization,'generation':result['generation'],'result_sha256':hashlib.sha256(result_raw).hexdigest(),
          'own_scope_stopped':True,'unknown_operations':[],'unsettled_operations':[],
          'unreceived_message_ids':record.get('unreceived_message_ids',[]),'local_lock_released':True}
    with records.lock_file(directory.parent/'worker.lock'):
        response=client.request('/api/worker/resource-scope/complete',body)
        scope.need(response.get('scope_completed') is True,'Scoped completion unknown; retain the reservation')
        save(directory/'scoped-completion-response.json',response)
