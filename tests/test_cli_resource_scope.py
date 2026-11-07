"""Actual private files/locks and temporary service; model/account processes are mocked."""
import copy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from coordinator import cli_dispatch as adapter
from coordinator import cli_resource_scope as local
from coordinator import cli_recovery as records
from coordinator import resource_scope as scope
from test_resource_scope import Fixture as ServiceFixture
from test_cli_results import fixture as result_fixture


def write(path,value):
    adapter.save(path,value)
    return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


class Fixture(ServiceFixture):
    def __init__(self,root):
        super().__init__(root)
        self.runs=root/'runs';self.runs.mkdir(mode=0o700)
        self.directory=self.runs/self.oldrun;self.directory.mkdir(mode=0o700)
        self.workspace=root/'source';self.workspace.mkdir()
        self.policy['allowed_workspaces']=[str(self.workspace)]
        self.policy['expires_at']=2000
        originals={'state.json':{'worker':self.worker,'run_id':self.oldrun,'task':self.old['id'],'queued_generation':0,
            'status':'needs_attention','created_at':800,'ended_at':995,'cli_exit_code':64},
            'worker-report.json':self.old_snapshot['result'],'task-readback.json':self.old_snapshot}
        self.policy['held'][0]['originals']={name:write(self.directory/name,value)['sha256'] for name,value in originals.items()}
        sources=[]
        for proof in self.policy['owned_settlement']:
            raw={**proof['facts'],**{key:proof[key] for key in ('role','run_id','task','generation','identity','waiter','recorded_at','observer_source_sha256')}}
            ref=write(root/(proof['role']+'-raw.json'),raw);sources.append(ref)
            proof['evidence_sha256']=ref['sha256']
            for row in proof['provenance'].values():row['reference']=ref
        self.policy['proof_sources']=sources
        bindings=[]
        for name,value in originals.items():bindings.extend({'source':name,**row,'resource_indexes':[0,1,2],'effects_known':True} for row in scope.unknowns(value))
        bindings.extend({'source':'fresh-api',**row,'resource_indexes':[0,1,2],'effects_known':True} for row in scope.unknowns(self.old_snapshot))
        self.policy['held'][0]['unknown_bindings']=bindings
        self.refence();self.configure()
        self.calls=[];self.client=Mock();self.client.config={'worker':self.worker};self.client.request.side_effect=self.request
        self.config={'hostname':socket.gethostname(),'worker':self.worker,'admission':'enabled','codex':'inert-never-executed',
            'repository':str(self.workspace),'model':'inert-model','reasoning':'high','account_email_sha256':hashlib.sha256(b'inert@example.invalid').hexdigest(),
            'resource_scope':{'enabled':True,'sources':{str(Path(local.__file__).resolve()):hashlib.sha256(Path(local.__file__).read_bytes()).hexdigest()}}}
        from coordinator import cli_event_dispatch,cli_messages,client,workers,cli_results
        self.config['resource_scope']['sources']={str(Path(module.__file__).resolve()):hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() for module in (local,scope,records,adapter,cli_event_dispatch,cli_messages,client,workers,cli_results)}
        self.account=({'type':'chatgpt','email':'inert@example.invalid'},[{'repository':str(self.workspace),'model':'inert-model','supportedReasoningEfforts':[{'reasoningEffort':'high'}]}],{'ordinaryUsageAllowed':True})
        self.before={p:p.read_bytes() for p in self.directory.iterdir()}

    def refence(self):
        refs=[{'path':str(self.directory/name),'sha256':digest} for name,digest in self.policy['held'][0]['originals'].items()]+self.policy['proof_sources']
        self.policy['local_fence_sha256']=scope.digest({r['path']:{'sha256':r['sha256'],'identity':records.bound(r)[2]} for r in refs})
    def request(self,path,body=None):
        self.calls.append((path,body))
        if path=='/api/state':return self.store.snapshot()
        if path.startswith('/api/tasks/'):return self.store.detail(path.rsplit('/',1)[-1])
        if path=='/api/worker/resource-scope/policy':return {'worker':self.worker,'revision':self.revision,'policy':copy.deepcopy(self.policy)}
        if path=='/api/worker/resource-scope/reserve':return scope.reserve(self.store,self.worker,body)
        raise AssertionError('Unexpected route '+path)
    def target(self,key='safe'):
        workspace=self.workspace/key;workspace.mkdir()
        task=super().task(key,workspace=str(workspace),resources=[{'kind':'path','value':str(workspace),'access':'write'},{'kind':'capability','value':'cli-shell'}])
        return self.store.detail(task['id'])
    def wake(self,task):
        with (self.runs/'worker.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return local.wake(self.config,self.runs,task['id'],self.revision,self.client,lock)
    def unchanged(self,test):
        self.assert_held(test)
        for path,raw in self.before.items():test.assertEqual(path.read_bytes(),raw)


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.f=Fixture(Path(self.temp.name).resolve())
        self.clock=patch.object(local.time,'time',return_value=1000);self.clock.start();self.addCleanup(self.clock.stop)

    def test_actual_all_six_raw_waits_leave_every_old_unknown_unchanged(self):
        self.assertEqual(local.validate_local(self.f.runs,self.f.worker,self.f.policy,self.f.client),self.f.policy['local_fence_sha256'])
        self.f.unchanged(self)

    def test_raw_provenance_generation_waiter_source_epoch_and_same_bytes_replacement_refuse(self):
        for case in ('identity','waiter','run_id','generation','observer_source_sha256','reaped','epoch','replacement'):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as directory:
                f=Fixture(Path(directory).resolve());proof=f.policy['owned_settlement'][0];path=Path(proof['provenance']['reaped']['reference']['path'])
                if case=='epoch':proof['not_before_epoch']=799
                elif case=='replacement':
                    replacement=path.with_name('replacement');replacement.write_bytes(path.read_bytes());replacement.chmod(0o600);os.replace(replacement,path)
                else:
                    raw=json.loads(path.read_text())
                    if case in ('identity','waiter'):raw[case]['birth']=[99,1]
                    else:raw[case]={'run_id':'0'*32,'generation':2,'observer_source_sha256':'0'*64,'reaped':False}[case]
                    ref=write(path,raw);proof['evidence_sha256']=ref['sha256']
                    f.policy['proof_sources'][0]=ref
                    for row in proof['provenance'].values():row['reference']=ref
                    f.refence()
                with self.assertRaises(ValueError):local.validate_local(f.runs,f.worker,f.policy,f.client)
                f.unchanged(self)

    def test_changed_api_unknown_missing_coverage_foreign_local_run_and_alias_refuse(self):
        for case in ('history','unknown','new-run','alias'):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as directory:
                f=Fixture(Path(directory).resolve())
                if case=='history':
                    with f.store.transaction() as db:f.store.event(db,f.worker,'note',f.old['id'],'history race')
                elif case=='unknown':f.policy['held'][0]['unknown_bindings'].pop()
                elif case=='new-run':write(f.runs/('c'*32)/'state.json',{'worker':f.worker,'status':'needs_attention'})
                else:
                    alias=f.root/'alias';alias.symlink_to(f.workspace);f.policy['allowed_workspaces']=[str(alias)]
                with self.assertRaises(ValueError):local.validate_local(f.runs,f.worker,f.policy,f.client)

    def test_one_scoped_wake_real_cli_binding_and_pre_execute_fence(self):
        target=self.f.target()
        with patch.object(adapter,'rpc_account',return_value=self.f.account),patch.object(adapter,'spawn') as spawn:
            record=self.f.wake(target)
        spawn.assert_called_once();directory=self.f.runs/record['run_id']
        from coordinator.cli_event_dispatch import claim_binding
        binding=claim_binding(self.f.runs,record['run_id']);self.assertEqual(binding['lane'],'resource-scoped')
        self.assertEqual(local.before_execute(self.f.config,self.f.runs,record,self.f.client),target['requirements']['resource_scope'])
        claim=self.f.store.claim(self.f.worker,target['id'],binding);self.assertEqual(claim['task']['status'],'running')
        self.assertFalse(record['generic_available']);self.assertFalse(record['wakeACKAuthorized'])
        self.assertTrue((directory/'scope-reservation-intent.json').exists());self.f.unchanged(self)

    def test_account_model_limits_or_source_failure_starts_no_model_or_reservation(self):
        target=self.f.target()
        for case in ('account','model','limits','source'):
            account=copy.deepcopy(self.f.account);config=copy.deepcopy(self.f.config)
            if case=='account':account[0]['email']='foreign@example.invalid'
            elif case=='model':account[1].clear()
            elif case=='limits':account[2]['ordinaryUsageAllowed']=False
            else:config['resource_scope']['sources'][str(Path(local.__file__).resolve())]='0'*64
            with self.subTest(case=case),patch.object(adapter,'rpc_account',return_value=account),patch.object(adapter,'spawn') as spawn:
                original=self.f.config;self.f.config=config
                with self.assertRaises(ValueError):self.f.wake(target)
                self.f.config=original
                spawn.assert_not_called()
        self.assertFalse(any('/reserve' in p for p,b in self.f.calls));self.f.unchanged(self)

    def test_ambiguous_reservation_leaves_durable_unknown_and_no_replay(self):
        target=self.f.target();original=self.f.request
        def lost(path,body=None):
            value=original(path,body)
            if path.endswith('/reserve'):raise OSError('inert response lost after commit')
            return value
        self.f.client.request.side_effect=lost
        with patch.object(adapter,'rpc_account',return_value=self.f.account),patch.object(adapter,'spawn') as spawn:
            with self.assertRaises(OSError):self.f.wake(target)
            with self.assertRaises(ValueError):self.f.wake(target)
        spawn.assert_not_called();self.assertEqual(sum(p.endswith('/reserve') for p,b in self.f.calls),1)
        self.f.unchanged(self)

    def test_scoped_cli_check_requires_worker_lock_and_never_touches_model_or_ack(self):
        config=self.f.root/'config.json';write(config,self.f.config);ref=write(self.f.root/'policy.json',self.f.policy)
        command=['worker','scoped-check','--note-file',ref['path'],'--request-sha256',ref['sha256']]
        with patch.object(adapter,'CONFIG',config),patch.object(adapter,'ROOT',self.f.runs),patch.object(adapter,'Client',return_value=self.f.client),patch.object(adapter,'rpc_account') as account,patch.object(adapter,'spawn') as spawn,patch('sys.argv',command),patch('sys.stdout',new_callable=io.StringIO) as out:
            adapter.main();self.assertFalse(json.loads(out.getvalue())['generic_available'])
            with (self.f.runs/'worker.lock').open('a+') as held:
                fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(ValueError):adapter.main()
        account.assert_not_called();spawn.assert_not_called();self.f.unchanged(self)

    def test_scoped_result_reuses_normal_reducer_but_never_normal_available_or_ack(self):
        run,task,docs=json.loads(json.dumps(result_fixture()).replace('b'*32,'e'*32));run['resource_scope']={'revision':'a'*64}
        directory=self.f.runs/run['run_id'];directory.mkdir(mode=0o700)
        for name,value in docs.items():write(directory/name,value)
        client=Mock();client.request.return_value=task
        adapter.finalize_result(directory,run,task,client)
        document=json.loads((directory/'scoped-run-result.json').read_text())
        self.assertEqual(run['status'],'scope-settled');self.assertEqual(document['availability'],'scope-only')
        self.assertFalse(document['wakeACKAuthorized']);self.assertFalse((directory/'run-result.json').exists())
        self.f.unchanged(self)

    def test_execute_restricts_write_roots_and_network_without_launching_any_process(self):
        target=self.f.target();declaration=target['requirements']['resource_scope']
        directory=self.f.runs/('d'*32);directory.mkdir(mode=0o700)
        write(directory/'state.json',{'worker':self.f.worker,'run_id':directory.name,'task':target['id'],'resource_scope':{'revision':self.f.revision}})
        (directory/'prompt.txt').write_text('inert')
        process=Mock();process.pid=999;process.returncode=0
        with patch.object(local,'before_execute',return_value=declaration),patch.object(adapter.subprocess,'Popen',return_value=process) as popen,patch.object(adapter,'Client',return_value=self.f.client),patch('coordinator.cli_messages.closeout',return_value=[]),patch.object(adapter,'finalize_result'):
            adapter.execute(self.f.config,directory,0)
        command=popen.call_args.args[0]
        self.assertIn('sandbox_workspace_write.network_access=false',command)
        self.assertEqual(command[command.index('--cd')+1],declaration['workspace'])
        added=[command[i+1] for i,v in enumerate(command) if v=='--add-dir']
        self.assertEqual(added,[str(directory),str(Path.home()/'.config/nova-coordinator/claims')]);self.f.unchanged(self)

    def test_evidence_changed_during_fresh_api_readback_refuses(self):
        original=self.f.request;calls=0
        def race(path,body=None):
            nonlocal calls
            value=original(path,body)
            if path=='/api/tasks/'+self.f.old['id']:
                calls+=1
                if calls==2:
                    source=Path(self.f.policy['proof_sources'][0]['path'])
                    replacement=self.f.root/'replacement';replacement.write_bytes(source.read_bytes());replacement.chmod(0o600);os.replace(replacement,source)
            return value
        self.f.client.request.side_effect=race
        with self.assertRaises(ValueError):local.validate_local(self.f.runs,self.f.worker,self.f.policy,self.f.client)
        self.f.unchanged(self)

    def test_completion_holds_worker_lock_and_never_reports_normal_capacity(self):
        directory=self.f.runs/('e'*32);directory.mkdir(mode=0o700)
        write(directory/'state.json',{'status':'scope-settled','unreceived_message_ids':[]})
        write(directory/'scoped-run-result.json',{'generation':1,'own_scope_stopped':True,'outcome':'completed','acceptance_complete':True})
        write(directory/'wake-authorization.json',{'lane':'resource-scoped','id':'e'*32,'run_id':directory.name,'token':'inert-token'})
        requests=[]
        def complete(path,body):
            requests.append(path)
            with (self.f.runs/'worker.lock').open('a+') as lock:
                with self.assertRaises(BlockingIOError):fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return {'scope_completed':True}
        client=Mock();client.request.side_effect=complete
        local.completed(client,self.f.config,directory)
        self.assertEqual(requests,['/api/worker/resource-scope/complete']);self.f.unchanged(self)


if __name__=='__main__':unittest.main()
