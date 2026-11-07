"""Explicit archive-bound local reset application. No VM/process/permission actions."""
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import stat

from . import cli_recovery as records
from . import resource_scope as scope
from .session_epochs import unknown_inventory

MAX_MANIFEST=16*1024*1024

def load(reference):return records.bound(reference,MAX_MANIFEST)[0]

def scan_archive(reference):
    archive=load(reference);manifest_ref=archive['manifest'];manifest=load(manifest_ref)
    scope.need(manifest['schema']==1 and manifest['kind']=='worker-records-pre-reset-manifest' and manifest['worker']==archive['worker'] and manifest['source_root']==archive['original_root'],'Exact original archive manifest required')
    root=Path(archive['archive_root']);records.private(root)
    scope.need(0<=archive['total_file_bytes']<=2*1024**3,'Bounded archive byte count required')
    entries=manifest['entries'];scope.need(type(entries) is list and 0<len(entries)<=8192 and len(entries)==archive['entry_count'],'Bounded complete archive inventory required')
    seen=set();total=0;unknowns=[]
    for entry in entries:
        relative=entry['path'];parts=PurePosixPath(relative)
        scope.need(type(relative) is str and relative not in seen and not parts.is_absolute() and '..' not in parts.parts and str(parts)==relative,'Unsafe/duplicate archive entry')
        seen.add(relative);path=root/relative
        # Walk ancestors without following an archived link outside the archive.
        scope.need(path.parent.resolve()==path.parent,'Archived input has symlink ancestor')
        info=path.lstat();kind=entry['kind']
        metadata={'mode':stat.S_IMODE(info.st_mode),'uid':info.st_uid,'gid':info.st_gid,'device':info.st_dev,'inode':info.st_ino,'mtime_ns':info.st_mtime_ns,'nlink':info.st_nlink}
        scope.need(all(metadata[key]==entry[key] for key in metadata if key in entry),'Archive metadata differs from immutable manifest')
        if kind=='directory':scope.need(stat.S_ISDIR(info.st_mode) and not path.is_symlink(),'Archive directory differs')
        elif kind=='symlink':
            target=entry.get('target',entry.get('symlinktarget',entry.get('symlink_target')))
            scope.need(stat.S_ISLNK(info.st_mode) and os.readlink(path)==target,'Archived link differs')
        else:
            scope.need(kind=='file' and stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_size==entry.get('size',entry.get('bytes')),'Archive file differs')
            digest=hashlib.sha256();captured=[]
            control=path.name in ('state.json','worker-report.json','task-readback.json','wait-receipt.json','finish-response.json','stopped-cleanup.json') and len(parts.parts)==2
            scope.need(not control or info.st_size<=MAX_MANIFEST,'Bounded original JSON record required')
            key=lambda v:(v.st_dev,v.st_ino,v.st_size,v.st_mtime_ns,v.st_ctime_ns)
            with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as stream:
                scope.need(key(info)==key(os.fstat(stream.fileno())),'Archive file identity changed before read')
                for chunk in iter(lambda:stream.read(1024*1024),b''):
                    digest.update(chunk)
                    if control:captured.append(chunk)
                after=os.fstat(stream.fileno())
            scope.need(digest.hexdigest()==entry['sha256'] and key(info)==key(after)==key(path.lstat()),'Archived bytes changed during verification')
            total+=info.st_size;scope.need(total<=2*1024**3,'Archive byte limit exceeded')
            if control:
                raw=json.loads(b''.join(captured))
                unknowns.extend({'source':'archive:'+relative,**u} for u in unknown_inventory(raw))
    scope.need(total==archive['total_file_bytes'],'Archive byte census differs')
    # No extra active evidence may be omitted from the manifest. Never traverse links.
    actual=set()
    for directory,dirs,files in os.walk(root,followlinks=False):
        for name in dirs+files:
            actual.add(str((Path(directory)/name).relative_to(root)))
            scope.need(len(actual)<=8192,'Extra archive entry limit exceeded')
    scope.need(actual==seen,'Archive contains unmanifested or missing entries')
    scope.need(load(reference)==archive and load(manifest_ref)==manifest,'Archive binding changed')
    return archive,unknowns


def verify_archive(reference,policy):
    archive,unknowns=scan_archive(reference)
    bound=[{k:r[k] for k in ('source','pointer','value_sha256')} for r in policy['unknown_bindings'] if r['source'].startswith('archive:')]
    scope.need(sorted(bound,key=scope.encoded)==sorted(unknowns,key=scope.encoded),'Every archived unknown must remain resource-bound')
    for workspace in policy['allowed_workspaces']:
        scope.need(Path(workspace).resolve()==Path(workspace),'Workspace alias is not disjoint')
    return archive


def prepare(plan_ref,archive_ref,policy_ref,decision_ref,active_root,output,agent_stopped=False):
    from .cli_dispatch import save
    from .session_epochs import validate_policy
    scope.need(agent_stopped is True and not output.exists(),'Actual operator stop and new private request path required')
    records.private(active_root)
    with records.lock_file(active_root/'worker.lock'),records.lock_file(active_root/'event-listener.lock'):
        plan=load(plan_ref);archive,unknowns=scan_archive(archive_ref);base=load(policy_ref);decision=load(decision_ref)
        scope.need(plan['worker']==archive['worker']==base['worker'] and plan['paused'] is True,'Exact paused worker/archive policy required')
        scope.need(not list(active_root.glob('*/state.json')),'Unarchived active run remains')
        resources=base['unknown_binding_resources']
        held=sorted([{'task':t['task']} for t in plan['held']]+[{'task':t} for t in plan['quarantined_tasks']],key=lambda r:r['task'])
        policy={**{k:v for k,v in base.items() if k!='unknown_binding_resources'},'held':held,
                'unknown_bindings':[{**u,'resource_indexes':resources,'effects_bounded':base['unknown_effects_bounded']} for u in [*plan['unknowns'],*unknowns]]}
        validate_policy(policy,archive['worker'],held)
        for workspace in policy['allowed_workspaces']:scope.need(Path(workspace).resolve()==Path(workspace),'Workspace alias is not disjoint')
        request={'id':archive['reset_id'],'worker':archive['worker'],'expected_epoch':plan['expected_epoch'],'fingerprint':plan['fingerprint'],
                 'archive':archive,'decision':decision,'policy':policy,'local_admission':{'worker_lock_held':True,'listener_lock_held':True,'agent_stopped':True}}
        save(output,request)
        return {'path':str(output),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'unknown_bindings':len(policy['unknown_bindings']),'old_cleanup_verified':False}


def verify(reference,policy,active_root,agent_stopped=False):
    scope.need(agent_stopped is True,'Operator must attest the actual owned agent stop; no stop inferred')
    records.private(active_root)
    with records.lock_file(active_root/'worker.lock'),records.lock_file(active_root/'event-listener.lock'):
        archive=verify_archive(reference,policy)
        scope.need(not list(active_root.glob('*/state.json')),'Unarchived active run remains')
        return {'archive':archive,'local_admission':{'worker_lock_held':True,'listener_lock_held':True,'agent_stopped':True},
                'old_cleanup_verified':False,'agents_started':False}

def apply(client,request_ref,archive_ref,active_root):
    from .cli_dispatch import save
    body=load(request_ref);worker=body['worker']
    scope.need(body['local_admission']=={'worker_lock_held':True,'listener_lock_held':True,'agent_stopped':True},'Actual operator agent-stop receipt required')
    records.private(active_root)
    # These existing entrypoint locks are live-operation fences, not process absence.
    with records.lock_file(active_root/'worker.lock'),records.lock_file(active_root/'event-listener.lock'):
        scope.need(verify_archive(archive_ref,body['policy'])==body['archive'],'Request differs from actual immutable archive')
        # Fresh namespace must have no active runs before this explicit install.
        scope.need(not list(active_root.glob('*/state.json')),'Unarchived active records prevent reset')
        journal=active_root/'reset-journal';journal.mkdir(mode=0o700,exist_ok=True)
        intent=journal/(body['id']+'-request.json')
        if intent.exists():scope.need(load({'path':str(intent),'sha256':hashlib.sha256(intent.read_bytes()).hexdigest()})==body,'Conflicting reset journal')
        else:save(intent,body)
        response=client.request('/api/workers/'+worker+'/session-reset/apply',body)
        scope.need(response.get('recorded') is True and response.get('epoch')==body['id'] and response.get('active_epoch')==body['id'],'Reset response stale/unknown; preserve journal, no activation')
        save(journal/(body['id']+'-response.json'),response)
        return response

def install(client,worker,epoch,client_config,dispatch_config,active_root):
    """Recoverable local adoption only; no agent or model is started."""
    from .cli_dispatch import save
    scope.need(scope.hexid(epoch,32) and client_config!=dispatch_config,'Exact epoch and distinct config paths required')
    if isinstance(getattr(client,'config',None),dict) and client.config.get('worker')==worker:
        client.session_epoch=epoch  # Explicit operator adoption, never automatic discovery.
        plan=client.request('/api/worker/session')
        scope.need(plan['worker']==worker and plan['epoch']==epoch and plan['paused'] is True,'Current paused own epoch readback required')
    else:
        plan=client.request('/api/workers/'+worker+'/session-reset/plan',{})
        scope.need(plan['expected_epoch']==epoch and plan['paused'] is True,'Current paused epoch readback required')
    directory=active_root/'epochs'/epoch;directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    with records.lock_file(active_root/'worker.lock'),records.lock_file(active_root/'event-listener.lock'):
        for path,cli in ((client_config,False),(dispatch_config,True)):
            value=json.loads(records.read(path)[0]);scope.need(value['worker']==worker and value.get('session_epoch') in (None,epoch),'Foreign/stale local config cannot be adopted')
            previous=active_root/'reset-journal'/(epoch+'-'+path.name+'.before.json')
            if not previous.exists():save(previous,value)
            updated={**value,'session_epoch':epoch}
            if cli:updated.update(admission='disabled',event_dispatch={**value.get('event_dispatch',{}),'enabled':False})
            save(path,updated)
    return {'epoch':epoch,'active_root':str(directory),'admission':'disabled','agents_started':False,'credentials_changed':False}
