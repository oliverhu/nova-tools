"""Temporary DB/HTTP/archive tests; no guest or native operations."""
import copy
import concurrent.futures
import fcntl
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock,patch

from coordinator import session_epochs as epochs
from coordinator import cli_session_reset as local
from coordinator import cli_dispatch as cli
from coordinator import event_dispatch as event
from coordinator import resource_scope as scope
from coordinator.server import Server
from coordinator.store import Store,Conflict
from test_resource_scope import Fixture


def write(path,value):
    cli.save(path,value)
    return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


class Model(Fixture):
    def __init__(self,root):
        super().__init__(root);self.ident='e'*32;self.plan=epochs.plan(self.store,self.worker)
        self.policy={k:copy.deepcopy(self.policy[k]) for k in ('schema','worker','held','resources','allowed_workspaces','allowed_kinds','allowed_capabilities')}
        self.policy['held']=[{'task':self.old['id']}]
        self.policy['unknown_effects_bounded']=True
        self.policy['unknown_bindings']=[{'source':self.old['id'],**u,'resource_indexes':[0,1,2],'effects_bounded':True} for u in scope.unknowns(self.old_snapshot)]
        self.archive={'schema':1,'kind':'worker-session-archive','worker':self.worker,'reset_id':self.ident,'original_root':'/inert/active','archive_root':'/inert/archive','manifest':{'path':'/inert/archive/manifest.json','sha256':'d'*64},'verified':True,'entry_count':1,'total_file_bytes':1}
        self.body={'id':self.ident,'worker':self.worker,'expected_epoch':None,'fingerprint':self.plan['fingerprint'],'archive':self.archive,'policy':self.policy,
            'decision':{'source':{'thread_id':'inert-thread','message_id':'inert-not-human-authority'},'response':'inert explicit reset decision fixture','scope':'archive-active-session-only','verified':True},
            'local_admission':{'worker_lock_held':True,'listener_lock_held':True,'agent_stopped':True}}
        self.store.set_worker_state('admin',self.worker,{'paused':True,'reason':'Inert archive/reset boundary'})
        self.refresh()
    def refresh(self):
        self.plan=epochs.plan(self.store,self.worker);self.body['fingerprint']=self.plan['fingerprint']
    def reset(self):return epochs.apply(self.store,'admin',self.worker,self.body)
    def fresh(self):
        self.reset();self.now+=1
        event.configure(self.store,'admin',self.worker,{'enabled':True,'readiness_revision':'f'*64,'evidence':'Inert new own epoch readiness'})
        self.store.set_worker_state('admin',self.worker,{'paused':False,'reason':'Inert new scope qualified'})
        self.store.pulse(self.worker,{'capacity':{'cpus':4,'memory_gb':8,'disk_gb':100,'capabilities':['cli-shell','git-source','cli-media','cli-media-video']}})
        event.report(self.store,self.worker,{'readiness_revision':'f'*64,'sequence':1,'availability':'available','latest_run_id':None,'settlement_sha256':'d'*64,'issue':None})
    def unchanged_unknown(self,test):
        after=self.store.detail(self.old['id'])
        for key in ('result','receipts','generation','fix_attempts_used','total_fix_attempts','requirements','dependencies'):test.assertEqual(after[key],self.old_snapshot[key])
        test.assertEqual(after['approval'],self.old_snapshot['approval'])
        test.assertEqual(after['status'],'abandoned_unverified');test.assertFalse(after['session_quarantine']['cleanup_verified'])
        with self.store.connection() as db:
            wake=dict(db.execute('SELECT * FROM dispatch_wakes WHERE id=?',(self.oldrun,)).fetchone())
            for key,value in self.oldwake.items():
                if key!='status':test.assertEqual(wake[key],value)
            test.assertEqual(wake['status'],'quarantined')
            reset=db.execute('SELECT document FROM worker_epoch_resets WHERE id=?',(self.ident,)).fetchone()
            test.assertEqual(json.loads(reset[0])['before']['wakes'][0],self.oldwake)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.f=Model(Path(self.temp.name))
    def test_explicit_abandonment_does_not_require_old_owned_wait_or_claim_cleanup(self):
        response=self.f.reset();self.assertFalse(response['execution_stopped']);self.assertFalse(response['wakeACKAuthorized']);self.assertFalse(response['resourceReleaseAuthorized'])
        self.f.unchanged_unknown(self)
        worker=next(w for w in self.f.store.snapshot()['workers'] if w['id']==self.f.worker)
        self.assertTrue(worker['paused']);self.assertFalse(worker['event_dispatch']['enabled']);self.assertEqual(worker['session_epoch']['id'],self.f.ident)
        self.assertFalse(epochs.protocol(self.f.store,self.f.worker,None));self.assertTrue(epochs.protocol(self.f.store,self.f.worker,self.f.ident))
    def test_fresh_disjoint_source_inert_media_are_usable_old_native_and_unscoped_deny(self):
        self.f.fresh()
        for kind in ('source','inert','media'):
            target=self.f.task(kind,kind=kind)
            with self.f.store.connection() as db:self.assertTrue(epochs.admit(self.f.store,db,self.f.worker,scope.task_snapshot(self.f.store,db,target['id'])))
        target=self.f.task('ordinary')
        token='new-epoch-wake-token-inert-only-000000';run='c'*32
        event.reserve(self.f.store,self.f.worker,{'wake_id':run,'run_id':run,'task_id':target['id'],'queued_generation':0,'readiness_revision':'f'*64,'token_hash':hashlib.sha256(token.encode()).hexdigest()})
        binding={'wake_id':run,'run_id':run,'token':token}
        self.assertIsNone(self.f.store.claim(self.f.worker,target['id'],self.f.oldbinding)['task'])
        self.assertEqual(self.f.store.claim(self.f.worker,target['id'],binding)['task']['status'],'running');self.f.unchanged_unknown(self)
    def test_missing_unknown_binding_unbounded_effects_wrong_worker_and_global_stop_refuse(self):
        cases=[lambda b:b['policy']['unknown_bindings'].pop(),lambda b:b.update(worker='oliverhu-mini-vm2'),lambda b:b['decision'].update(verified=False),lambda b:b['local_admission'].update(agent_stopped=False),lambda b:b['local_admission'].update(worker_lock_held=False)]
        for change in cases:
            body=copy.deepcopy(self.f.body);change(body)
            with self.subTest(change=change),self.assertRaises(Conflict):epochs.apply(self.f.store,'admin',self.f.worker,body)
        self.assertEqual(self.f.store.detail(self.f.old['id']),self.f.old_snapshot)
    def test_abandonment_can_record_unbounded_unknown_without_allowing_any_fresh_work(self):
        self.f.body['policy']['unknown_effects_bounded']=False
        self.f.body['policy']['unknown_bindings'][0]['effects_bounded']=False
        self.assertTrue(self.f.reset()['recorded'])
        self.f.now+=1;task=self.f.task('denied')
        with self.f.store.connection() as db:self.assertFalse(epochs.admit(self.f.store,db,self.f.worker,scope.task_snapshot(self.f.store,db,task['id'])))
        self.f.unchanged_unknown(self)

    def test_full_cas_generation_history_pause_and_wake_races_refuse(self):
        for case in ('history','generation','pause','wake'):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as directory:
                f=Model(Path(directory))
                with f.store.transaction() as db:
                    if case=='history':f.store.event(db,f.worker,'note',f.old['id'],'inert history race')
                    elif case=='generation':db.execute('UPDATE tasks SET generation=generation+1 WHERE id=?',(f.old['id'],))
                    elif case=='pause':db.execute('UPDATE workers SET paused=0 WHERE id=?',(f.worker,))
                    else:db.execute("UPDATE dispatch_wakes SET phase='testing' WHERE id=?",(f.oldrun,))
                with self.assertRaises(Conflict):f.reset()
    def test_idempotency_concurrent_reset_and_old_claim_token_fences_survive_restart(self):
        def attempt(_):return self.f.reset()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:responses=list(pool.map(attempt,(1,2)))
        self.assertEqual(sum(r['duplicate'] for r in responses),1)
        changed=copy.deepcopy(self.f.body);changed['decision']['response']='other'
        with self.assertRaises(Conflict):epochs.apply(self.f.store,'admin',self.f.worker,changed)
        self.f.store=Store(self.f.store.path,clock=lambda:1000)
        with self.assertRaises(Conflict):self.f.store.heartbeat(self.f.worker,self.f.old['id'],{'generation':1,'claim_token':self.f.oldclaim['claim_token']})
        with self.assertRaises(Conflict):event.settle(self.f.store,self.f.worker,{**self.f.oldbinding,'generation':1,'result_sha256':'d'*64,'outcome':'pending_external_approval','execution_stopped':True,'input_settled':True,'unknown_operations':[],'unsettled_operations':[],'unreceived_message_ids':[],'local_lock_released':True,'readiness_revision':self.f.readiness,'failure_classification':None})
        self.f.unchanged_unknown(self)
    def test_new_epoch_never_admits_unscoped_native_or_intersecting_work(self):
        self.f.fresh()
        plain=self.f.store.submit('admin',{'request_key':'unscoped','title':'No explicit scope','requirements':{'worker':self.f.worker}})
        bad=self.f.task('native',operations=['native-launch'])
        overlap=self.f.task('overlap',workspace='/inert/native/candidate.app')
        for task in (plain,bad,overlap):
            with self.f.store.connection() as db:self.assertFalse(epochs.admit(self.f.store,db,self.f.worker,scope.task_snapshot(self.f.store,db,task['id'])))
        self.assertIsNone(self.f.store.claim(self.f.worker,self.f.old['id'],self.f.oldbinding)['task'])


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        self.archive=self.root/'archive';self.archive.mkdir(mode=0o700);self.active=self.root/'active';self.active.mkdir(mode=0o700)
        run=self.archive/('b'*32);run.mkdir(mode=0o700);write(run/'state.json',{'unknown_operations':[{'pid':7777}]})
        data=(run/'state.json').read_bytes()
        entries=[{'path':'b'*32,'kind':'directory'},{'path':'b'*32+'/state.json','kind':'file','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}]
        self.manifest=write(self.root/'manifest.json',{'schema':1,'kind':'worker-records-pre-reset-manifest','worker':'oliverhu-mini-vm1','source_root':str(self.active),'entries':entries})
        self.document={'schema':1,'kind':'worker-session-archive','worker':'oliverhu-mini-vm1','reset_id':'e'*32,'original_root':str(self.active),'archive_root':str(self.archive),'manifest':self.manifest,'verified':True,'entry_count':2,'total_file_bytes':len(data)}
        self.reference=write(self.root/'archive-binding.json',self.document)
        self.policy={'allowed_workspaces':[str(self.root/'source')],'unknown_bindings':[{'source':'archive:'+'b'*32+'/state.json',**scope.unknowns(json.loads(data))[0],'resource_indexes':[0],'effects_bounded':True}]}
    def test_actual_archive_bytes_unknowns_and_entry_census_match(self):
        self.assertEqual(local.verify_archive(self.reference,self.policy),self.document)
    def test_changed_omitted_extra_or_unsafe_archive_inputs_refuse(self):
        original=(self.archive/('b'*32)/'state.json').read_bytes()
        path=self.archive/('b'*32)/'state.json';path.write_bytes(b'{}')
        with self.assertRaises(ValueError):local.verify_archive(self.reference,self.policy)
        path.write_bytes(original);write(self.archive/'extra.json',{})
        with self.assertRaises(ValueError):local.verify_archive(self.reference,self.policy)
        (self.archive/'extra.json').unlink();self.policy['unknown_bindings']=[]
        with self.assertRaises(ValueError):local.verify_archive(self.reference,self.policy)
    def test_active_worker_or_listener_lock_refuses_before_any_reset_request(self):
        request=write(self.root/'request.json',{'worker':'oliverhu-mini-vm1','id':'e'*32,'archive':self.document,'policy':self.policy,'local_admission':{'worker_lock_held':True,'listener_lock_held':True,'agent_stopped':True}})
        client=Mock()
        for name in ('worker.lock','event-listener.lock'):
            with (self.active/name).open('a+') as held:
                (self.active/name).chmod(0o600)
                fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):local.apply(client,request,self.reference,self.active)
        client.request.assert_not_called()
    def test_lost_response_journal_preserved_exact_request_can_be_reconciled(self):
        body={'worker':'oliverhu-mini-vm1','id':'e'*32,'archive':self.document,'policy':self.policy,'local_admission':{'worker_lock_held':True,'listener_lock_held':True,'agent_stopped':True}}
        reference=write(self.root/'request.json',body);client=Mock();client.request.side_effect=OSError('response lost')
        with self.assertRaises(OSError):local.apply(client,reference,self.reference,self.active)
        self.assertTrue((self.active/'reset-journal'/('e'*32+'-request.json')).exists())
        client.request.side_effect=None;client.request.return_value={'recorded':True,'epoch':'e'*32,'active_epoch':'e'*32,'duplicate':True}
        self.assertTrue(local.apply(client,reference,self.reference,self.active)['duplicate'])
    def test_local_epoch_install_is_recoverable_and_does_not_change_account_token_or_start_agent(self):
        config=write(self.root/'client.json',{'worker':'oliverhu-mini-vm1','token':'inert-retained-worker-token','account':'unchanged'})
        dispatch=write(self.root/'dispatch.json',{'worker':'oliverhu-mini-vm1','admission':'enabled','event_dispatch':{'enabled':True},'model':'same-model'})
        client=Mock();client.request.return_value={'expected_epoch':'e'*32,'paused':True}
        for _ in range(2):local.install(client,'oliverhu-mini-vm1','e'*32,Path(config['path']),Path(dispatch['path']),self.active)
        result=json.loads(Path(config['path']).read_text());self.assertEqual(result['token'],'inert-retained-worker-token');self.assertEqual(result['account'],'unchanged')
        state=json.loads(Path(dispatch['path']).read_text());self.assertEqual(state['admission'],'disabled');self.assertFalse(state['event_dispatch']['enabled']);self.assertEqual(state['model'],'same-model')

class EpochHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.f=Model(Path(self.temp.name))
        self.tokens={'admin':'a'*40,self.f.worker:'b'*40,'oliverhu-mini-vm2':'c'*40}
        self.server=Server(('127.0.0.1',0),self.f.store,self.tokens,Path(__file__).resolve().parents[1]/'web')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def request(self,path,actor,body=None,epoch=None):
        headers={'Content-Type':'application/json','Authorization':'Bearer '+self.tokens[actor]}
        if epoch:headers['X-Nova-Session-Epoch']=epoch
        req=urllib.request.Request('http://127.0.0.1:'+str(self.server.server_port)+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with urllib.request.urlopen(req,timeout=3) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as error:
            with error:return error.code,json.load(error)
    def test_real_reset_auth_stale_report_and_new_current_session_readback(self):
        route='/api/workers/'+self.f.worker+'/session-reset/'
        for actor in (self.f.worker,'oliverhu-mini-vm2'):
            self.assertEqual(self.request(route+'plan',actor,{})[0],403)
            self.assertEqual(self.request(route+'apply',actor,self.f.body)[0],403)
        code,plan=self.request(route+'plan','admin',{});self.assertEqual(code,200);self.assertEqual(plan['fingerprint'],self.f.body['fingerprint'])
        code,response=self.request(route+'apply','admin',self.f.body);self.assertEqual(code,200);self.assertFalse(response['wakeACKAuthorized'])
        self.assertEqual(self.request('/api/worker/pulse',self.f.worker,{'capacity':{}})[0],409)
        self.assertEqual(self.request('/api/worker/pulse',self.f.worker,{'capacity':{}},epoch='a'*32)[0],409)
        self.assertEqual(self.request('/api/worker/session',self.f.worker)[0],409)
        code,current=self.request('/api/worker/session',self.f.worker,epoch=self.f.ident);self.assertEqual(code,200);self.assertEqual(current['epoch'],self.f.ident);self.assertTrue(current['paused'])
        self.assertEqual(self.request('/api/worker/pulse',self.f.worker,{'capacity':{}},epoch=self.f.ident)[0],200)
        # Other workers remain legacy-compatible; no cross-worker global epoch.
        self.assertEqual(self.request('/api/worker/pulse','oliverhu-mini-vm2',{'capacity':{}})[0],200)
        self.f.unchanged_unknown(self)
    def test_actual_client_sends_explicit_cached_epoch_and_isolates_old_claim_namespace(self):
        from coordinator.client import Client,ServiceRejected
        self.f.reset()
        config=Path(self.temp.name)/'worker-client.json';config.write_text(json.dumps({'worker':self.f.worker,'token':self.tokens[self.f.worker],'url':'http://127.0.0.1:'+str(self.server.server_port)}))
        old=Client(config)
        with self.assertRaises(ServiceRejected):old.request('/api/worker/pulse',{'capacity':{}})
        original=json.loads(config.read_text());config.write_text(json.dumps({**original,'session_epoch':self.f.ident}))
        new=Client(config);self.assertEqual(new.state,config.parent/'claims'/'epochs'/self.f.ident)
        self.assertEqual(new.request('/api/worker/pulse',{'capacity':{}})['worker'],self.f.worker)
        with self.assertRaises(ServiceRejected):old.request('/api/worker/pulse',{'capacity':{}})
        self.assertEqual(json.loads(config.read_text())['token'],original['token'])
    def test_preexisting_media_task_explicit_scope_adoption_and_no_quarantined_task_adoption(self):
        old_task=self.f.task('preexisting',kind='media')
        self.f.refresh();self.f.reset()
        with self.f.store.connection() as db:self.assertFalse(epochs.admit(self.f.store,db,self.f.worker,scope.task_snapshot(self.f.store,db,old_task['id'])))
        current=self.f.store.detail(old_task['id']);body={'worker':self.f.worker,'epoch':self.f.ident,'task_sha256':scope.task_fingerprint(current),'resource_scope':old_task['requirements']['resource_scope'],'evidence':'Inert existing media acceptance fully fits this new scope'}
        self.assertEqual(self.request('/api/tasks/'+old_task['id']+'/session-scope',self.f.worker,body,epoch=self.f.ident)[0],403)
        self.assertEqual(self.request('/api/tasks/'+old_task['id']+'/session-scope','admin',body)[0],200)
        with self.f.store.connection() as db:self.assertTrue(epochs.admit(self.f.store,db,self.f.worker,scope.task_snapshot(self.f.store,db,old_task['id'])))
        body['task_sha256']=scope.task_fingerprint(self.f.store.detail(self.f.old['id']))
        self.assertEqual(self.request('/api/tasks/'+self.f.old['id']+'/session-scope','admin',body)[0],409)

class EpochBoundaryTests(unittest.TestCase):
    def test_new_epoch_media_inbox_is_disjoint_old_inbox_and_whole_root_deny(self):
        with tempfile.TemporaryDirectory() as directory:
            f=Model(Path(directory));f.fresh()
            fresh='/inert/active/epochs/'+f.ident
            declaration={'schema':1,'kind':'media','operations':['media-read','image-inspect'],'workspace':'/inert/source/new','resources':[{'kind':'path','value':'/inert/source/new','access':'write'},{'kind':'path','value':fresh,'access':'read'},*({'kind':'capability','value':v} for v in ('cli-shell','cli-media','cli-media-v1'))],'required_capabilities':['cli-shell','cli-media','cli-media-v1'],'effects_declared':True,'acceptance_within_scope':True}
            # The epoch policy explicitly permits actual hash-bound media-v1;
            # its old strict resource-scoped counterpart still requires content.
            with f.store.transaction() as db:
                row=epochs.current(db,f.worker);policy=json.loads(row['policy']);policy['allowed_capabilities'].append('cli-media-v1');db.execute('UPDATE worker_epochs SET policy=? WHERE worker=?',(scope.encoded(policy),f.worker))
            task=f.store.submit('admin',{'request_key':'fresh-inbox','title':'Inert inbox qualification','requirements':{'worker':f.worker,'capabilities':declaration['required_capabilities'],'resource_scope':declaration}})
            with f.store.connection() as db:
                entry=epochs.current(db,f.worker);actual=scope.task_snapshot(f.store,db,task['id']);self.assertTrue(epochs.admit(f.store,db,f.worker,actual))
                for path in ('/inert/active','/inert/active/epochs/'+'a'*32,'/inert/archive'):
                    changed=copy.deepcopy(actual);changed['requirements']['resource_scope']['resources'][1]['value']=path
                    self.assertFalse(epochs.admit(f.store,db,f.worker,changed))
                with self.assertRaises(ValueError):scope.declaration(actual,json.loads(entry['policy']))
    def test_distinct_reset_ids_same_cas_only_one_can_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            f=Model(Path(directory));bodies=[copy.deepcopy(f.body) for _ in range(2)];bodies[1]['id']='f'*32;bodies[1]['archive']['reset_id']='f'*32
            def run(body):
                try:return epochs.apply(f.store,'admin',f.worker,body)['epoch']
                except Conflict:return None
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(run,bodies))
            self.assertEqual(sum(v is not None for v in result),1)
    def test_legacy_index_upgrade_is_atomic_and_retains_old_wake_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            f=Model(Path(directory))
            with f.store.connection() as db:
                db.execute('DROP INDEX one_active_worker_wake');db.execute("CREATE UNIQUE INDEX one_active_worker_wake ON dispatch_wakes(worker) WHERE status!='settled'")
            upgraded=Store(f.store.path,clock=lambda:1000);self.assertEqual(upgraded.detail(f.old['id']),f.old_snapshot)
            with upgraded.connection() as db:self.assertIn('quarantined',db.execute("SELECT sql FROM sqlite_master WHERE name='one_active_worker_wake'").fetchone()[0])
            self.assertEqual(epochs.apply(upgraded,'admin',f.worker,f.body)['epoch'],f.ident)
    def test_namespace_requires_explicit_local_epoch_and_never_scans_archive(self):
        from coordinator import session_paths
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'client.json';config.write_text('{}')
            self.assertEqual(session_paths.root(config),session_paths.BASE)
            config.write_text(json.dumps({'session_epoch':'e'*32}));self.assertEqual(session_paths.root(config),session_paths.BASE/'epochs'/('e'*32))
            config.write_text(json.dumps({'session_epoch':'../archive'}))
            with self.assertRaises(ValueError):session_paths.root(config)


class EpochJournalTests(unittest.TestCase):
    def test_fresh_epoch_never_recovers_old_pending_approval_journal(self):
        from coordinator.client import Client
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'client.json';config.write_text(json.dumps({'worker':'oliverhu-mini-vm1','token':'inert-only','url':'http://127.0.0.1:1','session_epoch':'e'*32}))
            previous=root/'approvals';previous.mkdir();old=previous/'old.json';old.write_text(json.dumps({'state':'pending','request':{'do_not_replay':'old held decision'}}))
            client=Client(config)
            with patch.object(client,'deliver_approval') as delivery:self.assertEqual(client.recover_approvals(),[])
            delivery.assert_not_called();self.assertTrue(old.exists());self.assertEqual(client.approval_directory(),previous/'epochs'/('e'*32))


class EpochExecutionTests(unittest.TestCase):
    def test_model_gets_only_fresh_workspace_run_claims_and_current_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();workspace=root/'workspace';workspace.mkdir();run=root/'run';run.mkdir()
            task={'id':'a'*16,'status':'queued','owner':None,'task_type':'work','compute':'standard','requirements':{'worker':'oliverhu-mini-vm1','capabilities':['cli-shell'],'resource_scope':{'schema':1,'kind':'source','operations':['source-read','source-edit'],'workspace':str(workspace),'resources':[{'kind':'path','value':str(workspace),'access':'write'},{'kind':'capability','value':'cli-shell'}],'required_capabilities':['cli-shell'],'effects_declared':True,'acceptance_within_scope':True}}}
            config={'worker':'oliverhu-mini-vm1','session_epoch':'e'*32,'codex':'inert-never-executed','reasoning':'medium','model':'inert','repository':str(workspace)}
            write(run/'state.json',{'worker':config['worker'],'task':task['id'],'run_id':'c'*32});(run/'prompt.txt').write_text('inert')
            binding={'epoch':'e'*32,'archive':{'original_root':str(root/'active'),'archive_root':str(root/'archive')},'policy':{'worker':config['worker'],'held':[],'allowed_kinds':['source'],'allowed_capabilities':['cli-shell'],'allowed_workspaces':[str(workspace)],'resources':[{'kind':'capability','value':'native-ui'}],'unknown_effects_bounded':True,'unknown_bindings':[]}}
            client=Mock(state=root/'claims/epochs'/('e'*32));client.request.side_effect=lambda path:binding if path=='/api/worker/session' else task
            child=Mock(pid=777,returncode=0)
            with patch.object(cli,'Client',return_value=client),patch.object(cli.subprocess,'Popen',return_value=child) as popen,patch('coordinator.cli_messages.closeout',return_value=[]),patch.object(cli,'finalize_result'):
                cli.execute(config,run,0)
            command=popen.call_args.args[0];self.assertIn('sandbox_workspace_write.network_access=false',command)
            self.assertEqual(command[command.index('--cd')+1],str(workspace))
            writes=[command[i+1] for i,v in enumerate(command) if v=='--add-dir']
            self.assertEqual(writes,[str(run),str(client.state)])


class EpochRetryTests(unittest.TestCase):
    def test_stopped_known_retry_is_allowed_but_unknown_retry_never_becomes_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            f=Model(Path(directory));f.fresh();target=f.task('known-retry')
            with f.store.connection() as db:
                entry=epochs.current(db,f.worker);task=scope.task_snapshot(f.store,db,target['id']);task.update(status='blocked',owner=f.worker,result={'execution_stopped':True,'unknown_operations':[]})
                self.assertEqual(epochs.declaration(task,entry)['kind'],'source')
                task['result']['unknown_operations']=['unresolved own operation']
                with self.assertRaises(ValueError):epochs.declaration(task,entry)


class EpochStopPreservationTests(unittest.TestCase):
    def test_explicit_abandonment_archives_stop_without_lifting_old_task_permission_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            f=Model(Path(directory))
            with f.store.transaction() as db:
                old=f.old_snapshot['result'];old['human_stop']=True
                db.execute('UPDATE tasks SET result=? WHERE id=?',(scope.encoded(old),f.old['id']))
            f.old_snapshot=f.store.detail(f.old['id']);f.refresh()
            f.body['policy']['unknown_bindings']=[{'source':f.old['id'],**u,'resource_indexes':[0,1,2],'effects_bounded':True} for u in epochs.unknown_inventory(f.old_snapshot)]
            f.reset();after=f.store.detail(f.old['id'])
            self.assertTrue(after['result']['human_stop']);self.assertEqual(after['approval']['state'],'awaiting_decision');self.assertEqual(after['status'],'abandoned_unverified')
            self.assertIsNone(f.store.claim(f.worker,f.old['id'],f.oldbinding)['task'])


class EpochPrepareTests(unittest.TestCase):
    setUp=ArchiveTests.setUp
    # Only this additional test is collected here; archive cases remain above.
    def test_private_request_is_generated_from_real_archive_hashes_without_manual_unknown_retyping(self):
        plan=write(self.root/'plan.json',{'worker':self.document['worker'],'paused':True,'held':[],'quarantined_tasks':[],'unknowns':[],'expected_epoch':None,'fingerprint':'f'*64})
        base=write(self.root/'base-policy.json',{'schema':1,'worker':self.document['worker'],'resources':[{'kind':'capability','value':'native-ui'}],'allowed_workspaces':[str(self.root/'source')],'allowed_kinds':['media'],'allowed_capabilities':['cli-shell','cli-media','cli-media-v1'],'unknown_effects_bounded':True,'unknown_binding_resources':[0]})
        decision=write(self.root/'decision.json',{'source':{'thread_id':'inert','message_id':'inert-not-human'},'response':'inert fixture','scope':'archive-active-session-only','verified':True})
        output=self.root/'request-prepared.json'
        with self.assertRaises(ValueError):local.prepare(plan,self.reference,base,decision,self.active,output)
        ref=local.prepare(plan,self.reference,base,decision,self.active,output,agent_stopped=True)
        actual=local.load({k:ref[k] for k in ('path','sha256')})
        self.assertEqual(actual['archive'],self.document);self.assertEqual(len(actual['policy']['unknown_bindings']),1);self.assertFalse(ref['old_cleanup_verified'])


if __name__=='__main__':unittest.main()
