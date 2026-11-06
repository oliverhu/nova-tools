import concurrent.futures
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from coordinator import publication_policy as policy
from coordinator.publication_ancestry import attestation
from coordinator.store import Store, Conflict, Invalid
from coordinator.workers import STUDIO_WORKERS

STUDIO, VM1, VM2 = STUDIO_WORKERS


class PublicationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.now = 1000.0
        self.store = Store(Path(self.temp.name)/'state', clock=lambda:self.now)
        self.repo = 'owner/repo'
        self.store.configure_repository({'repository':self.repo, 'published_commit':'a'*40})
        self.pulse()

    def tearDown(self):
        self.temp.cleanup()

    def pulse(self):
        for w in STUDIO_WORKERS:
            self.store.pulse(w, {'capacity':{'cpus':8,'memory_gb':24,'disk_gb':100}})

    def observe(self, head, ancestors=('a',)):
        return self.store.observe_repository(STUDIO, {'repository':self.repo,'commit':head*40,'observed_at':self.now,
            'publication_ancestry':{'head':head*40,'published_head':self.store.snapshot()['repositories'][0]['published_head'],
                                    'ancestors':[x*40 for x in ancestors]}})

    def tick(self, head=None, ancestors=('a',)):
        self.now += 3600; self.pulse()
        if head:self.observe(head,ancestors)
        return self.store.tick_releases(self.repo)

    def enable(self):
        plan = policy.plan(self.store,'admin',{'repository':self.repo})
        body = {'repository':self.repo,'fingerprint':plan['fingerprint'],'execution_stopped':True,
                'external_state_checked':True,'evidence':'Reviewed exact task histories and actual source ancestry, preserving all genuine holds'}
        return policy.apply_plan(self.store,'admin',body), body

    def raw(self, ident):
        with self.store.connection() as db:
            return policy.history(self.store,db,db.execute('SELECT * FROM tasks WHERE id=?',(ident,)).fetchone())

    def claim(self, ident):
        c=self.store.claim(STUDIO,ident);self.assertIsNotNone(c['task']);return c

    def finish(self, claim, outcome='completed', **receipt):
        return self.store.finish(STUDIO,claim['task']['id'],{'generation':claim['task']['generation'],'step':0,
            'claim_token':claim['claim_token'],'outcome':outcome,
            'receipt':{'summary':'Actual owned outcome','execution_stopped':True,**receipt}})

    def test_disabled_policy_does_not_rewrite_legacy_scheduling(self):
        old=self.tick('b')[0];new=self.tick('c')[0]
        self.assertEqual('queued',self.store.detail(old)['status']);self.assertEqual('queued',self.store.detail(new)['status'])
        self.assertEqual(old,self.claim(old)['task']['id'])

    def test_latest_ancestor_coalesces_and_audits_preserving_original_target(self):
        old=self.tick('b')[0];before=self.raw(old);self.enable()
        new=self.tick('c',('a','b'))[0]
        after=self.raw(old)
        self.assertEqual('cancelled',after['task']['status'])
        for key in ('result','generation','step','requirements','dependencies','fix_attempts_used','total_fix_attempts'):
            self.assertEqual(before['task'][key],after['task'][key])
        self.assertEqual(before['receipts'],after['receipts'])
        self.assertEqual('publication-superseded',after['events'][-1]['kind'])
        self.assertIsNone(self.store.claim(VM1,old)['task'])
        self.assertEqual(new,self.claim(new)['task']['id'])

    def test_constraints_are_carried_before_latest_can_be_claimed(self):
        dep=self.store.submit('admin',{'request_key':'prior-test','title':'Original test prerequisite'})
        old=self.tick('b')[0]
        self.store.defer('admin',old,{'generation':0,'execution_stopped':True,'external_state_checked':True,
            'evidence':'Original unstarted prerequisite','dependencies':[dep['id']],'worker':STUDIO})
        # Existing defer/claim history is a barrier, never silently converted.
        original=self.raw(old);self.enable();self.assertEqual([],self.tick('c',('a','b')))
        self.assertEqual(original,self.raw(old));self.assertFalse(self.store.detail(old)['ready'])

    def test_service_added_constraints_remain_pristine_and_are_rechecked(self):
        old=self.tick('b')[0]
        dep=self.store.submit('admin',{'request_key':'original-test','title':'Original prerequisite'})
        # Model the initial submission's actual constraints, not a stopped claim.
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET requirements=?,dependencies=? WHERE id=?',
                (policy.encode({'worker':STUDIO,'cpus':4}),policy.encode([dep['id']]),old))
        self.enable();new=self.tick('c',('a','b'))[0]
        row=self.store.detail(new)
        self.assertEqual([dep['id']],row['dependencies']);self.assertEqual({'worker':STUDIO,'cpus':4},row['requirements'])
        self.assertIsNone(self.store.claim(STUDIO,new)['task'])
        self.observe('c',('a','b'));self.assertEqual([],self.store.tick_releases(self.repo))
        self.assertEqual('queued',self.store.detail(new)['status'])

    def test_running_publication_keeps_claim_and_later_commit_waits(self):
        old=self.tick('b')[0];self.enable();c=self.claim(old);before=self.raw(old)
        self.assertEqual([],self.tick('c',('a','b')))
        self.assertEqual(before,self.raw(old));self.assertEqual('c'*40,self.store.snapshot()['repositories'][0]['merged_head'])
        self.finish(c,published_commit='b'*40,publication_ref='verified:b')
        new=self.tick('c',('a','b'))[0]
        self.assertEqual('c'*40,self.store.detail(new)['result']['release_target']['commit'])
        self.assertEqual('b'*40,self.store.snapshot()['repositories'][0]['published_head'])

    def test_unknown_partial_and_approval_waits_are_not_replayed_or_superseded(self):
        for outcome,receipt in [('failed',{}),('blocked',{'publication_ref':'partial:mac','published_commit':'b'*40}),
                                ('pending_external_approval',{'approval_request':{'thread_id':'human','question_id':'original'}})]:
            with self.subTest(outcome=outcome):
                self.tearDown(); self.setUp()
                try:
                    old=self.tick('b')[0];c=self.claim(old);self.finish(c,outcome,**receipt);before=self.raw(old)
                    self.enable();self.assertEqual([],self.tick('c',('a','b')))
                    self.assertEqual(before,self.raw(old));self.assertIsNone(self.store.claim(STUDIO,old)['task'])
                finally:self.tearDown()

    def test_no_change_retires_proven_obsolete_queue_without_changing_baseline(self):
        old=self.tick('b')[0];new=self.tick('c')[0];c=self.claim(new)
        self.finish(c,published_commit='c'*40,publication_ref='verified:c')
        self.enable();count=len(self.store.snapshot()['tasks'])
        self.assertEqual([],self.tick('c',('a','b')))
        self.assertEqual('cancelled',self.store.detail(old)['status']);self.assertEqual(count,len(self.store.snapshot()['tasks']))
        self.assertEqual('c'*40,self.store.snapshot()['repositories'][0]['published_head'])
        restarted=Store(self.store.path,clock=lambda:self.now)
        self.assertEqual([],restarted.tick_releases(self.repo))

    def test_missing_divergent_or_stale_ancestry_refuses_supersession_and_pickup(self):
        old=self.tick('b')[0];self.enable();before=self.raw(old)
        self.assertEqual([],self.tick('c',('a',)))
        self.assertEqual(before,self.raw(old));self.assertIsNone(self.store.claim(STUDIO,old)['task'])
        self.observe('c',('b',))
        self.assertEqual([],self.store.tick_releases(self.repo));self.assertEqual(before,self.raw(old))
        self.observe('b',('a',));self.now+=601;self.pulse()
        self.assertIsNone(self.store.claim(STUDIO,old)['task'])

    def test_claim_race_invalidates_admin_plan_and_exact_replay_is_idempotent(self):
        old=self.tick('b')[0];plan=policy.plan(self.store,'admin',{'repository':self.repo})
        body={'repository':self.repo,'fingerprint':plan['fingerprint'],'execution_stopped':True,'external_state_checked':True,'evidence':'Reviewed no-effects ancestry'}
        self.claim(old)
        with self.assertRaises(Conflict):policy.apply_plan(self.store,'admin',body)
        result,body=self.enable();count=len(self.store.detail(old)['events'])
        replay=policy.apply_plan(self.store,'admin',body)
        self.assertEqual(result['audit_id'],replay['audit_id']);self.assertTrue(replay['replayed'])
        self.assertEqual(count,len(self.store.detail(old)['events']))

    def test_concurrent_ticks_create_only_one_latest_and_survive_restart(self):
        old=self.tick('b')[0];self.enable();self.now+=3600;self.observe('c',('a','b'));self.pulse()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.store.tick_releases(self.repo),range(4)))
        created=[x for row in results for x in row];self.assertEqual(1,len(created))
        self.assertEqual('cancelled',self.store.detail(old)['status'])
        self.store=Store(self.store.path,clock=lambda:self.now)
        self.assertEqual([],self.store.tick_releases(self.repo));self.claim(created[0])

    def test_legacy_claim_history_and_conflicting_requirements_remain_exceptions(self):
        old=self.tick('b')[0];c=self.claim(old)
        self.store.defer(STUDIO,old,{'generation':c['task']['generation'],'claim_token':c['claim_token'],
            'execution_stopped':True,'external_state_checked':True,'evidence':'Stopped unstarted owner claim','worker':STUDIO})
        before=self.raw(old);self.enable();self.assertEqual([],self.tick('c',('a','b')))
        self.assertEqual(before,self.raw(old))

    def test_conflicting_original_workers_are_not_silently_replaced(self):
        old=self.tick('b')[0];new=self.tick('c')[0]
        with self.store.transaction() as db:
            for ident,worker in [(old,VM1),(new,STUDIO)]:
                db.execute('UPDATE tasks SET requirements=? WHERE id=?',(policy.encode({'worker':worker}),ident))
        before=[self.raw(x) for x in [old,new]]
        self.enable();self.observe('c',('a','b'))
        self.assertEqual([],self.store.tick_releases(self.repo))
        self.assertEqual(before,[self.raw(x) for x in [old,new]])
        for worker,ident in [(VM1,old),(STUDIO,new)]:
            self.assertIsNone(self.store.claim(worker,ident)['task'])

    def test_offline_publisher_accumulates_only_latest_proven_pending_target(self):
        old=self.tick('b')[0]
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET requirements=? WHERE id=?',(policy.encode({'worker':STUDIO}),old))
        self.enable();self.store.set_worker_state('admin',STUDIO,{'paused':True,'reason':'Original publisher maintenance'})
        current=old
        for head,ancestors in [('c',('a','b')),('d',('a','b','c')),('e',('a','b','c','d'))]:
            previous=current;current=self.tick(head,ancestors)[0]
            self.assertEqual('cancelled',self.store.detail(previous)['status'])
            self.assertEqual({'worker':STUDIO},self.store.detail(current)['requirements'])
            self.assertFalse(self.store.detail(current)['ready'])
            self.assertIsNone(self.store.claim(VM2,current)['task'])
        active=[t for t in self.store.snapshot()['tasks'] if t['task_type']=='publish' and t['status']=='queued']
        self.assertEqual([current],[t['id'] for t in active])

    def handoff(self):
        old=self.tick('b')[0];c=self.claim(old)
        self.store.defer(STUDIO,old,{'generation':c['task']['generation'],'claim_token':c['claim_token'],
            'execution_stopped':True,'external_state_checked':True,'evidence':'Owner confirms no build, version, sign, notarization, upload or deployment started','worker':STUDIO})
        self.enable();self.tick('c',('a','b'))
        plan=policy.plan(self.store,'admin',{'repository':self.repo})
        item=next(x for x in plan['reviewable_handoffs'] if x['task']==old)
        body={'repository':self.repo,'task':old,'fingerprint':plan['fingerprint'],'owner_receipt_hashes':item['owner_receipt_hashes'],
            'release_actions_unstarted':True,'execution_stopped':True,'external_state_checked':True,
            'evidence':'Independently reviewed every original owner no-release-action statement and external state; source ancestor positively verified'}
        return old,body

    def test_reviewed_old_handoff_has_exact_receipt_audit_and_no_history_reset(self):
        old,body=self.handoff();before=self.raw(old)
        result=policy.retire_reviewed_handoff(self.store,'admin',body)
        after=self.raw(old)
        self.assertEqual('cancelled',result['task']['status'])
        for k in ('result','generation','step','requirements','dependencies','fix_attempts_used','total_fix_attempts','last_owner'):
            self.assertEqual(before['task'][k],after['task'][k])
        self.assertEqual(before['receipts'],after['receipts']);self.assertEqual('publication-unstarted-reviewed',after['events'][-1]['kind'])
        self.assertTrue(policy.retire_reviewed_handoff(self.store,'admin',body)['replayed'])
        new=self.store.tick_releases(self.repo)[0]
        self.assertEqual({'worker':STUDIO},self.store.detail(new)['requirements']);self.claim(new)

    def test_handoff_review_rejects_partial_receipt_unknown_action_and_stale_state(self):
        old,body=self.handoff()
        with self.assertRaises(Conflict):policy.retire_reviewed_handoff(self.store,VM1,body)
        with self.assertRaises(Conflict):policy.retire_reviewed_handoff(self.store,'admin',{**body,'owner_receipt_hashes':['f'*64]})
        with self.assertRaises(Invalid):policy.retire_reviewed_handoff(self.store,'admin',{**body,'release_actions_unstarted':False})
        with self.store.transaction() as db:
            receipt=db.execute('SELECT receipt FROM receipts WHERE task_id=?',(old,)).fetchone()
            value=json.loads(receipt['receipt']);value['receipt']['ios_upload']={'status':'unknown'}
            db.execute('UPDATE receipts SET receipt=? WHERE task_id=?',(policy.encode(value),old))
        with self.assertRaises(Conflict):policy.retire_reviewed_handoff(self.store,'admin',body)
        fresh=policy.plan(self.store,'admin',{'repository':self.repo})
        self.assertNotIn(old,[x['task'] for x in fresh['reviewable_handoffs']])
        self.assertEqual('queued',self.store.detail(old)['status'])

    def test_supersession_never_breaks_an_original_exact_target_dependency(self):
        old=self.tick('b')[0]
        self.store.submit('admin',{'request_key':'depends-on-publication','title':'Needs exact old delivery','dependencies':[old]})
        self.enable();before=self.raw(old)
        self.assertEqual([],self.tick('c',('a','b')))
        self.assertEqual(before,self.raw(old))
        self.assertFalse(self.store.detail(old)['ready'])

    def test_explicit_upload_only_keeps_original_scope_and_single_admission(self):
        old=self.tick('b')[0];second=self.tick('c')[0]
        with self.store.transaction() as db:
            for ident,commit in [(old,'b'),(second,'c')]:
                value={'release_target':{'repository':self.repo,'branch':'main','commit':commit*40},
                       'release_scope':'ios_upload_only','release_authorization':{'thread_id':'human','message_id':'original'}}
                db.execute('UPDATE tasks SET request_key=?,result=?,requirements=? WHERE id=?',
                    ('ios-upload-only:'+ident,policy.encode(value),policy.encode({'worker':STUDIO}),ident))
        self.enable();before=[self.raw(x) for x in [old,second]]
        self.assertEqual([],self.tick('d',('a','b','c')))
        self.assertEqual(before,[self.raw(x) for x in [old,second]])
        self.assertTrue(self.store.detail(old)['ready']);self.assertFalse(self.store.detail(second)['ready'])
        c=self.claim(old)
        self.assertIsNone(self.store.claim(VM1,second)['task'])
        self.assertFalse(self.store.detail(second)['ready'])
        self.assertEqual('ios_upload_only',c['task']['result']['release_scope'])

    def test_unauthorized_and_malformed_policy_requests_refused(self):
        with self.assertRaises(Conflict):policy.plan(self.store,VM2,{'repository':self.repo})
        with self.assertRaises(Conflict):policy.apply_plan(self.store,VM2,{})
        _,body=self.enable()
        for key,value in [('fingerprint','bad'),('execution_stopped',False),('external_state_checked',False),('repository',{})]:
            with self.subTest(key=key),self.assertRaises((Invalid,Conflict)):
                policy.apply_plan(self.store,'admin',{**body,key:value})
        with self.assertRaises(Invalid):
            self.store.observe_repository(STUDIO,{'repository':self.repo,'commit':'b'*40,'observed_at':self.now,
                'publication_ancestry':{'head':'b'*40,'published_head':'a'*40,'ancestors':[{}]}})


class ActualGitAncestryTests(unittest.TestCase):
    def test_actual_git_proof_excludes_divergent_and_missing_objects_without_source_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)
            def git(*args):return subprocess.check_output(['git','-C',temp,*args],stderr=subprocess.DEVNULL,text=True).strip()
            git('init');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-m','root')
            root=git('rev-parse','HEAD')
            git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-m','next');head=git('rev-parse','HEAD')
            git('checkout','--orphan','other');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-m','different');other=git('rev-parse','HEAD')
            before=git('status','--porcelain');branch=git('rev-parse','HEAD')
            snapshot={'capabilities':{'publication_policy':1},'repositories':[{'name':'owner/repo','published_head':root}],
                'tasks':[{'task_type':'publish','status':'queued','result':{'release_target':{'repository':'owner/repo','commit':c}}} for c in [head,other,'f'*40]]}
            proof=attestation({'name':'owner/repo','path':temp},head,snapshot)
            self.assertEqual(set([root,head]),set(proof['ancestors']))
            snapshot['publication_candidates']=[t['result']['release_target'] for t in snapshot['tasks']]
            snapshot['tasks']=[]
            self.assertEqual(set([root,head]),set(attestation({'name':'owner/repo','path':temp},head,snapshot)['ancestors']))
            self.assertEqual(branch,git('rev-parse','HEAD'));self.assertEqual(before,git('status','--porcelain'))
            self.assertIsNone(attestation({'name':'owner/repo','path':temp},head,{}))
