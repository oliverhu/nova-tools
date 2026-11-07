"""Temporary-DB resource admission; all process/media facts are inert models."""
import copy
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from coordinator import event_dispatch as normal
from coordinator import resource_scope as scope
from coordinator.store import Store,Conflict,Invalid
from coordinator.workers import MINI_VM_WORKERS


class Fixture:
    def __init__(self,root):
        self.root=Path(root);self.now=1000.;self.worker=MINI_VM_WORKERS[0]
        self.store=Store(self.root/'db',clock=lambda:self.now)
        self.store.set_worker_state('admin',self.worker,{'paused':False,'reason':'inert source qualification'})
        self.store.pulse(self.worker,{'capacity':{'cpus':4,'memory_gb':8,'disk_gb':100,'capabilities':['cli-shell','git-source','cli-media','cli-media-video']}})
        self.readiness='a'*64;normal.configure(self.store,'admin',self.worker,{'enabled':True,'readiness_revision':self.readiness,'evidence':'inert qualified source'})
        normal.report(self.store,self.worker,{'readiness_revision':self.readiness,'sequence':1,'availability':'available','latest_run_id':None,'settlement_sha256':'a'*64,'issue':None})
        self.old=self.store.submit('admin',{'request_key':'held','title':'inert held native task','requirements':{'worker':self.worker}})
        self.oldtoken='original-inert-wake-token-000000000000';self.oldrun='b'*32
        normal.reserve(self.store,self.worker,{'wake_id':self.oldrun,'run_id':self.oldrun,'task_id':self.old['id'],'queued_generation':0,'readiness_revision':self.readiness,'token_hash':hashlib.sha256(self.oldtoken.encode()).hexdigest()})
        self.oldbinding={'wake_id':self.oldrun,'run_id':self.oldrun,'token':self.oldtoken}
        self.oldclaim=self.store.claim(self.worker,self.old['id'],self.oldbinding)
        unknown={'pid':7777,'uid':os.getuid(),'birth':[1,2]}
        self.store.finish(self.worker,self.old['id'],{'claim_token':self.oldclaim['claim_token'],'generation':1,'step':0,'outcome':'pending_external_approval','receipt':{'summary':'inert native unknown remains','acceptance_complete':False,'execution_stopped':True,'original_final_census_clean':False,'runtime_cleanup_acceptance_complete':False,'unresolved_observations':[unknown],'unknown_operations':[],'resourceReleaseAuthorized':False}})
        held=self.store.detail(self.old['id'])
        self.old_snapshot=copy.deepcopy(held)
        with self.store.connection() as db:self.oldwake=dict(normal.active(db,self.worker));self.oldconfig=dict(db.execute('SELECT * FROM dispatch_workers WHERE worker=?',(self.worker,)).fetchone())
        resources=[{'kind':'path','value':'/inert/native/candidate.app','access':'write'}, {'kind':'process',**unknown}, {'kind':'capability','value':'native-ui'}]
        self.proofs=[];self.sources=[]
        for index,role in enumerate(('host','driver','executor','controller','native-main','server')):
            ident={'pid':100+index,'uid':os.getuid(),'birth':[2,index]};waiter={'pid':200,'uid':os.getuid(),'birth':[1,0]}
            facts={'reaped':True,'normal_exit':True,'returncode':64 if role=='native-main' else 0,'wait_target_pid':ident['pid']}
            if role=='server':facts={'thread_joined':True,'listener_closed':True,'connection_refused':True,'endpoint':'http://127.0.0.1:49152','nonce':'c'*32}
            ref={'path':'/inert/proofs/'+role+'.json','sha256':str(index+1)*64};self.sources.append(ref)
            provenance={k:{'reference':ref,'pointer':'/'+k} for k in [*facts,'role','run_id','task','generation','identity','waiter','recorded_at','observer_source_sha256']}
            self.proofs.append({'role':role,'run_id':self.oldrun,'task':held['id'],'generation':1,'not_before_epoch':900,'not_after_epoch':990,'identity':ident,'waiter':waiter,'facts':facts,'provenance':provenance,'evidence_sha256':ref['sha256'],'recorded_at':950,'observer_source_sha256':'e'*64})
        self.policy={'schema':1,'worker':self.worker,'uid':os.getuid(),'held':[{'run_id':self.oldrun,'task':held['id'],'generation':1,'api_sha256':scope.task_fingerprint(held),
             'originals':{name:'d'*64 for name in ('state.json','worker-report.json','task-readback.json')},
             'unknown_bindings':[{'source':'fresh-api',**row,'resource_indexes':[1,2],'effects_known':True} for row in scope.unknowns(held)],
             'approval_sha256':scope.digest(held['approval']),'approval_resources':[0,1,2],'approval_scope_known':True}],
             'resources':resources,'owned_settlement':self.proofs,'proof_sources':self.sources,'local_fence_sha256':'f'*64,
             'allowed_workspaces':['/inert/source'],'allowed_kinds':['source','inert','media'],'allowed_capabilities':['cli-shell','git-source','cli-media','cli-media-video'],
             'expires_at':2000,'review_source':{'thread_id':'inert-review','message_id':'inert-no-human-authority'}}
        self.revision=None

    def configure(self):
        value=scope.configure(self.store,'admin',self.worker,{'policy':self.policy,'expected_revision':self.revision});self.revision=value['revision'];return value
    def task(self,key='safe',kind='source',**updates):
        caps=['cli-shell'] if kind!='media' else ['cli-shell','cli-media','cli-media-video']
        resources=[{'kind':'path','value':'/inert/source/'+key,'access':'write'}]+[{'kind':'capability','value':cap} for cap in caps]
        if kind=='media':resources.append({'kind':'media','path':'/inert/source/assets/video.mp4','sha256':'a'*64,'access':'read'})
        declaration={'schema':1,'kind':kind,'operations':{'source':['source-read','source-edit'],'inert':['inert-python'],'media':['media-read','video-inspect']}[kind],
                     'resources':resources,'workspace':'/inert/source/'+key,'required_capabilities':caps,'effects_declared':True,'acceptance_within_scope':True}
        declaration.update(updates)
        return self.store.submit('admin',{'request_key':key,'title':key,'requirements':{'worker':self.worker,'capabilities':caps,'resource_scope':declaration}})
    def reserve(self,task,ident='1'):
        token='scoped-inert-token-00000000000000000000'
        body={'id':ident*32,'run_id':ident*32,'task_id':task['id'],'queued_generation':task['generation'],'policy_revision':self.revision,
              'task_sha256':scope.task_fingerprint(self.store.detail(task['id'])),'token_hash':hashlib.sha256(token.encode()).hexdigest(),'local_fence_sha256':self.policy['local_fence_sha256']}
        response=scope.reserve(self.store,self.worker,body)
        return {'lane':'resource-scoped','id':body['id'],'run_id':body['run_id'],'token':token},response
    def assert_held(self,test):
        test.assertEqual(self.store.detail(self.old['id']),self.old_snapshot)
        with self.store.connection() as db:
            test.assertEqual(dict(normal.active(db,self.worker)),self.oldwake)
            test.assertEqual(dict(db.execute('SELECT * FROM dispatch_workers WHERE worker=?',(self.worker,)).fetchone()),self.oldconfig)
        test.assertIsNone(normal.next_candidate(self.store,self.worker)['task_id'])


class ScopedServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.f=Fixture(Path(self.temp.name))

    def test_disjoint_scope_reserve_claim_and_complete_never_ack_or_release_old_wake(self):
        self.f.configure();task=self.f.task();binding,response=self.f.reserve(task)
        self.assertFalse(response['generic_available']);self.assertFalse(response['wakeACKAuthorized'])
        self.assertIsNone(self.f.store.claim(self.f.worker,task['id'])['task'])
        claim=self.f.store.claim(self.f.worker,task['id'],binding);self.assertEqual(claim['task']['status'],'running')
        receipt={'summary':'inert source acceptance complete','acceptance_complete':True,'execution_stopped':True,
                 'unknown_operations':[],'unsettled_operations':[],'cleanup':{'execution_stopped':True,'external_state_checked':True,'input_settled':True,'unresolved_observations':[],'unknown_operations':[],'unsettled_operations':[]}}
        self.f.store.finish(self.f.worker,task['id'],{'generation':1,'step':0,'claim_token':claim['claim_token'],'outcome':'completed','receipt':receipt})
        body={k:v for k,v in binding.items() if k!='lane'};body.update(generation=1,result_sha256='d'*64,own_scope_stopped=True,unknown_operations=[],unsettled_operations=[],unreceived_message_ids=[],local_lock_released=True)
        done=scope.complete(self.f.store,self.f.worker,body);self.assertTrue(done['scope_completed']);self.assertEqual(done['held_wakes_acked'],[])
        self.f.assert_held(self)

    def test_explicit_source_inert_and_media_are_independently_eligible_only(self):
        self.f.configure()
        for kind in ('source','inert','media'):
            task=self.f.task(key=kind,kind=kind)
            with self.f.store.connection() as db:
                _,_,decl=scope.candidate(self.f.store,db,self.f.worker,task['id'],self.f.revision);self.assertEqual(decl['kind'],kind)
        projection=next(w for w in self.f.store.snapshot()['workers'] if w['id']==self.f.worker)
        self.assertEqual(len(projection['resource_scope']['eligible_task_ids']),3)
        self.assertNotEqual(projection['event_dispatch']['availability'],'available');self.f.assert_held(self)

    def test_unknown_unbounded_effects_missing_owned_proof_and_global_stop_deny(self):
        deltas=[lambda p:p['owned_settlement'].pop(),lambda p:p['owned_settlement'][0]['facts'].update(reaped=False),
                lambda p:p['held'][0]['unknown_bindings'][0].update(effects_known=False),lambda p:p['held'][0].update(approval_scope_known=False),
                lambda p:p['resources'].append({'kind':'unknown'}),lambda p:p['allowed_workspaces'].append('/inert/native')]
        for change in deltas:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                f=Fixture(Path(directory));change(f.policy)
                with self.assertRaises(Conflict):f.configure()
                f.assert_held(self)
        with self.assertRaises(ValueError):scope.unknowns({'human_stop':True})

    def test_unknown_or_intersecting_task_declarations_fail_closed(self):
        self.f.configure()
        cases=[{'operations':['native-launch']},{'operations':['source-read','ui-input']},{'effects_declared':False},
               {'acceptance_within_scope':False},{'workspace':'/inert/native/candidate.app'},
               {'resources':[{'kind':'path','value':'/inert/native/candidate.app/Profile','access':'write'}]},
               {'resources':[{'kind':'process','pid':7777,'uid':os.getuid(),'birth':[1,2]}]},
               {'resources':[{'kind':'path','value':'/inert/source/job','access':'write'}]},
               {'operations':['git-transfer']}, {'required_capabilities':['cli-shell','native-ui']}]
        for i,change in enumerate(cases):
            task=self.f.task('bad'+str(i),**change)
            with self.subTest(change=change),self.assertRaises(Conflict):self.f.reserve(task,ident=str(i%9+1))
        plain=self.f.store.submit('admin',{'request_key':'undeclared','title':'undeclared','requirements':{'worker':self.f.worker}})
        with self.assertRaises(Conflict):self.f.reserve(plain)
        self.f.assert_held(self)

    def test_held_or_target_history_owner_and_resource_revision_changes_refuse(self):
        self.f.configure();task=self.f.task()
        with self.f.store.transaction() as db:self.f.store.event(db,self.f.worker,'execution-phase',self.f.old['id'],'inert history change')
        with self.assertRaises(Conflict):self.f.reserve(task)
        # Fresh fixture isolates target-generation history from held-history drift.
        with tempfile.TemporaryDirectory() as directory:
            f=Fixture(Path(directory));f.configure();target=f.task();binding,_=f.reserve(target)
            with f.store.transaction() as db:f.store.event(db,f.worker,'note',target['id'],'inert target history changed')
            self.assertIsNone(f.store.claim(f.worker,target['id'],binding)['task'])
            f.assert_held(self)

    def test_scoped_reservation_concurrency_and_unknown_delivery_never_expire_into_retry(self):
        self.f.configure();task=self.f.task()
        def attempt(i):
            try:return self.f.reserve(task,ident=str(i))[0]
            except Conflict:return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:values=list(executor.map(attempt,(1,2)))
        self.assertEqual(sum(v is not None for v in values),1)
        binding=next(v for v in values if v);self.f.now+=100
        self.assertIsNone(self.f.store.claim(self.f.worker,task['id'],binding)['task'])
        other=self.f.task('another')
        with self.assertRaises(Conflict):self.f.reserve(other,ident='3')
        self.f.assert_held(self)

    def test_normal_handoff_task_wake_fence_cannot_be_bypassed_by_scoped_lane(self):
        self.f.configure()
        # Even a queued task with a changed hard destination retains its active wake fence.
        with self.assertRaises(Conflict):self.f.reserve(self.f.store.detail(self.f.old['id']))
        self.f.assert_held(self)

    def test_wrong_token_foreign_worker_and_failed_scope_completion_stay_held(self):
        self.f.configure();task=self.f.task();binding,_=self.f.reserve(task)
        wrong={**binding,'token':'wrong'}
        self.assertIsNone(self.f.store.claim(self.f.worker,task['id'],wrong)['task'])
        self.assertIsNone(self.f.store.claim(MINI_VM_WORKERS[1],task['id'],binding)['task'])
        claim=self.f.store.claim(self.f.worker,task['id'],binding)
        self.f.store.finish(self.f.worker,task['id'],{'generation':1,'step':0,'claim_token':claim['claim_token'],'outcome':'blocked',
            'receipt':{'summary':'inert own scope blocked','acceptance_complete':False,'execution_stopped':True}})
        body={k:v for k,v in binding.items() if k!='lane'};body.update(generation=1,result_sha256='d'*64,own_scope_stopped=True,unknown_operations=[],unsettled_operations=[],unreceived_message_ids=[],local_lock_released=True)
        with self.assertRaises(Conflict):scope.complete(self.f.store,self.f.worker,body)
        self.f.assert_held(self)

    def test_wrong_held_owner_generation_policy_expiry_and_declared_history_are_fenced(self):
        for case in ('owner','generation','expired','description'):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as directory:
                f=Fixture(Path(directory));f.configure();target=f.task()
                with f.store.transaction() as db:
                    if case=='owner':db.execute('UPDATE tasks SET last_owner=? WHERE id=?',(MINI_VM_WORKERS[1],f.old['id']))
                    elif case=='generation':db.execute('UPDATE tasks SET generation=generation+1 WHERE id=?',(f.old['id'],))
                if case=='expired':f.now=f.policy['expires_at']
                if case=='description':
                    binding,_=f.reserve(target)
                    with f.store.transaction() as db:db.execute('UPDATE tasks SET description=? WHERE id=?',('changed acceptance',target['id']))
                    self.assertIsNone(f.store.claim(f.worker,target['id'],binding)['task'])
                else:
                    with self.assertRaises(Conflict):f.reserve(target)

    def test_scoped_task_reservation_survives_cross_worker_handoff_and_normal_claim(self):
        self.f.configure();target=self.f.task();self.f.reserve(target)
        other=MINI_VM_WORKERS[1]
        self.f.store.set_worker_state('admin',other,{'paused':False,'reason':'inert handoff'})
        self.f.store.pulse(other,{'capacity':{'cpus':4,'memory_gb':8,'disk_gb':100,'capabilities':['cli-shell']}})
        with self.f.store.transaction() as db:
            requirements=json.loads(db.execute('SELECT requirements FROM tasks WHERE id=?',(target['id'],)).fetchone()[0])
            requirements['worker']=other
            db.execute('UPDATE tasks SET requirements=? WHERE id=?',(scope.encoded(requirements),target['id']))
        self.assertIsNone(self.f.store.claim(other,target['id'])['task'])
        self.f.assert_held(self)


if __name__=='__main__':unittest.main()
