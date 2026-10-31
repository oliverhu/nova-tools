import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from coordinator.store import Store, Conflict, Invalid


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.now=1000.0
        self.store=Store(Path(self.temp.name)/'db',clock=lambda:self.now,lease_seconds=60)
        self.pulse()

    def tearDown(self):self.temp.cleanup()

    def pulse(self):
        for w,m,c in [('oliver-mini',24,12),('oliver-mac',16,8)]:
            self.store.pulse(w,{'capacity':{'memory_gb':m,'cpus':c,'disk_gb':500,'capabilities':['macos']}})

    def add(self,key='task',**kw):
        return self.store.submit('oliver-mac',{'request_key':key,'title':key,**kw})

    def body(self,c):
        t=c['task'];return {'claim_token':c['claim_token'],'generation':t['generation'],'step':t['step']}

    def finish(self,c,outcome='completed',**receipt):
        t=c['task']
        target=t.get('result',{}).get('release_target',{})
        if target.get('repository')=='oliverhu/nova-browser' and outcome=='completed':
            receipt.setdefault('ios_upload',{'status':'uploaded','source_commit':target['commit'],
                'version':'0.2.21','build_number':'21','upload_ref':'apple-upload:verified-fixture',
                'testflight_distributed':False,'submitted_for_review':False})
        return self.store.finish(t['owner'],t['id'],{**self.body(c),'outcome':outcome,'receipt':{'summary':'Verified result','execution_stopped':True,**receipt}})

    def configure(self,name='oliverhu/nova-browser'):
        return self.store.configure_repository({'repository':name,'published_commit':'a'*40})

    def observe(self,head='b',repo='oliverhu/nova-browser',at=None):
        return self.store.observe_repository('oliver-mini',{'repository':repo,'branch':'main','commit':head*40,'observed_at':self.now if at is None else at})

    def release(self,repo='oliverhu/nova-browser',head='b'):
        self.configure(repo);self.now=(int(self.now)//3600+1)*3600;self.observe(head,repo);self.pulse()
        return self.store.tick_releases()[0]

    def wait_body(self, claim, **changes):
        return {**self.body(claim), 'execution_stopped':True, 'external_state_checked':True,
                'evidence':'Source inspected and all owned execution settled; waiting for prerequisite', **changes}

    def test_wait_routes_to_mini_after_dependency_without_manual_retry(self):
        dep=self.add('prerequisite',requirements={'worker':'oliver-mini'})
        prerequisite=self.store.claim('oliver-mini',dep['id'])
        task=self.add('dependent');old=self.store.claim('oliver-mac',task['id'])
        waiting=self.store.defer('oliver-mac',task['id'],self.wait_body(old,worker='oliver-mini',dependencies=[dep['id']]))
        self.assertEqual('queued',waiting['status']);self.assertIsNone(waiting['owner'])
        self.assertEqual('oliver-mac',waiting['last_owner']);self.assertEqual('oliver-mini',waiting['next_worker'])
        self.assertFalse(waiting['ready']);self.assertEqual(dep['id'],waiting['waiting_dependencies'][0]['id'])
        self.assertIsNone(self.store.claim('oliver-mac',task['id'])['task'])
        self.assertIsNone(self.store.claim('oliver-mini',task['id'])['task'])
        self.add('unrelated');self.assertIsNotNone(self.store.claim('oliver-mac')['task'])
        self.finish(prerequisite)
        self.assertTrue(self.store.detail(task['id'])['ready'])
        new=self.store.claim('oliver-mini',task['id']);self.assertIsNotNone(new['task'])
        with self.assertRaises(Conflict):self.finish(old)
        self.assertEqual('completed',self.finish(new)['status'])

    def test_admin_migrates_blocked_wait_without_losing_evidence(self):
        task=self.add();old=self.store.claim('oliver-mac');self.finish(old,'blocked',branch='existing-branch')
        result=self.store.defer('admin',task['id'],self.wait_body(old,worker='oliver-mini'))
        self.assertEqual('existing-branch',result['result']['branch'])
        self.assertEqual('oliver-mini',self.store.detail(task['id'])['next_worker'])
        self.assertEqual(1,len(self.store.detail(task['id'])['receipts']))
        self.assertIsNotNone(self.store.claim('oliver-mini')['task'])

    def test_wait_rejects_foreign_active_admin_unsettled_and_stale_changes(self):
        task=self.add();old=self.store.claim('oliver-mac');body=self.wait_body(old,worker='oliver-mini')
        for actor in ['admin','oliver-mini']:
            with self.assertRaises(Conflict):self.store.defer(actor,task['id'],body)
        with self.assertRaises(Invalid):self.store.defer('oliver-mac',task['id'],{**body,'execution_stopped':False})
        with self.assertRaises(Conflict):self.store.defer('oliver-mac',task['id'],{**body,'generation':0})
        self.assertEqual('running',self.store.detail(task['id'])['status'])

    def test_dependency_cycles_and_unknown_dependencies_rejected(self):
        first=self.add('first');second=self.add('second',dependencies=[first['id']])
        c=self.store.claim('oliver-mac',first['id'])
        for deps in [[first['id']],[second['id']],['missing']]:
            with self.assertRaises(Invalid):self.store.defer('oliver-mac',first['id'],self.wait_body(c,dependencies=deps))
        self.assertEqual([],self.store.detail(first['id'])['dependencies'])

    def test_waiting_worker_display_stays_mini_when_busy_or_offline(self):
        busy=self.add('busy',requirements={'worker':'oliver-mini'});self.store.claim('oliver-mini',busy['id'])
        task=self.add('waiting',requirements={'worker':'oliver-mini'})
        shown=self.store.detail(task['id']);self.assertEqual('oliver-mini',shown['next_worker']);self.assertFalse(shown['ready'])
        self.now+=100;shown=self.store.detail(task['id'])
        self.assertEqual('oliver-mini',shown['next_worker']);self.assertFalse(shown['ready'])

    def test_blocked_retries_on_different_host_after_cooldown(self):
        task=self.add();old=self.store.claim('oliver-mac')
        self.finish(old,'blocked',fix_attempts_used=3,branch='keep-source')
        shown=self.store.detail(task['id'])
        self.assertEqual('oliver-mini',shown['next_worker']);self.assertFalse(shown['ready'])
        self.assertIsNone(self.store.claim('oliver-mini')['task'])
        self.now+=900;self.pulse()
        self.assertTrue(self.store.detail(task['id'])['ready'])
        self.assertIsNone(self.store.claim('oliver-mac')['task'])
        new=self.store.claim('oliver-mini')
        self.assertEqual(0,new['task']['fix_attempts_used'])
        self.assertEqual(3,new['task']['total_fix_attempts'])
        self.assertEqual(1,new['task']['reassignment_count'])
        self.assertEqual('keep-source',new['task']['result']['branch'])
        self.assertEqual(2,new['task']['generation'])
        with self.assertRaises(Conflict):self.finish(old)
        self.finish(new,'blocked',fix_attempts_used=4)
        self.assertEqual('oliver-mac',self.store.detail(task['id'])['next_worker'])

    def test_blocked_retry_falls_back_without_ignoring_hard_requirements(self):
        task=self.add(requirements={'worker':'oliver-mini'})
        old=self.store.claim('oliver-mini');self.finish(old,'blocked')
        self.now+=900;self.pulse()
        self.assertIsNone(self.store.claim('oliver-mac')['task'])
        self.assertEqual('oliver-mini',self.store.claim('oliver-mini')['task']['owner'])
        other=self.add('ordinary');old=self.store.claim('oliver-mac',other['id']);self.finish(old,'blocked')
        self.now+=900;self.pulse()
        self.assertEqual('oliver-mac',self.store.claim('oliver-mac',other['id'])['task']['owner'])

    def test_six_host_changes_reset_attempts_then_stop(self):
        task=self.add();claim=self.store.claim('oliver-mac')
        self.assertEqual(0,claim['task']['reassignment_count'])
        for changes in range(7):
            self.assertEqual(0,claim['task']['fix_attempts_used'])
            self.assertEqual(changes,claim['task']['reassignment_count'])
            self.assertEqual(changes*10,claim['task']['total_fix_attempts'])
            self.finish(claim,'blocked',fix_attempts_used=10)
            self.store=Store(self.store.path,clock=lambda:self.now)
            self.now+=900;self.pulse()
            current=claim['task']['owner']
            other='oliver-mini' if current=='oliver-mac' else 'oliver-mac'
            self.assertIsNone(self.store.claim(current)['task'])
            if changes<6:
                claim=self.store.claim(other)
                self.assertIsNotNone(claim['task'])
            else:
                self.assertIsNone(self.store.claim(other)['task'])
        shown=self.store.detail(task['id'])
        self.assertEqual(70,shown['total_fix_attempts']);self.assertFalse(shown['ready'])
        self.assertEqual('Retry limits reached',shown['next_worker_label'])
        with self.assertRaises(Conflict):
            self.store.reconcile('admin',task['id'],self.wait_body(claim,action='retry'))
        # A deferred queue entry cannot bypass either budget.
        with self.assertRaises(Conflict):self.store.defer('admin',task['id'],self.wait_body(claim,worker='oliver-mini'))
        self.assertIsNone(self.store.claim('oliver-mini')['task'])

    def test_final_host_can_use_remaining_attempts_but_not_transfer(self):
        task=self.add();claim=self.store.claim('oliver-mac')
        for _ in range(6):
            self.finish(claim,'blocked',fix_attempts_used=1)
            self.now+=900;self.pulse()
            claim=self.store.claim('oliver-mini' if claim['task']['owner']=='oliver-mac' else 'oliver-mac')
        self.finish(claim,'blocked',fix_attempts_used=2)
        self.now+=900;self.pulse()
        self.assertIsNone(self.store.claim('oliver-mini')['task'])
        retry=self.store.claim('oliver-mac')
        self.assertEqual(2,retry['task']['fix_attempts_used'])
        self.assertEqual(6,retry['task']['reassignment_count'])
        self.assertEqual(8,retry['task']['total_fix_attempts'])

    def test_manual_retry_and_defer_count_only_actual_host_changes(self):
        task=self.add();claim=self.store.claim('oliver-mac');self.finish(claim,'blocked',fix_attempts_used=10)
        self.store.reconcile('admin',task['id'],self.wait_body(claim,action='retry'))
        self.assertEqual(0,self.store.detail(task['id'])['reassignment_count'])
        self.assertIsNone(self.store.claim('oliver-mac')['task'])
        retry=self.store.claim('oliver-mini')
        self.assertEqual(1,retry['task']['reassignment_count']);self.assertEqual(0,retry['task']['fix_attempts_used'])
        self.store.defer('oliver-mini',task['id'],self.wait_body(retry,worker='oliver-mac'))
        self.assertEqual(1,self.store.detail(task['id'])['reassignment_count'])
        self.assertEqual(2,self.store.claim('oliver-mac')['task']['reassignment_count'])

    def test_migration_counts_previous_transfers_and_preserves_active_claim(self):
        import sqlite3
        from coordinator.store import SCHEMA
        task=self.add();old=self.store.claim('oliver-mac');self.finish(old,'blocked',feature_attempts=10)
        self.now+=900;self.pulse();active=self.store.claim('oliver-mini')
        legacy_path=Path(self.temp.name)/'legacy'
        legacy=sqlite3.connect(legacy_path)
        legacy.executescript(SCHEMA)
        for definition in ('last_owner TEXT', "task_type TEXT NOT NULL DEFAULT 'work'", 'fix_attempts_used INTEGER NOT NULL DEFAULT 0', 'retry_after REAL'):
            legacy.execute('ALTER TABLE tasks ADD COLUMN '+definition)
        with self.store.connection() as db:
            for table in ('tasks','events','receipts','workers'):
                columns=[r[1] for r in legacy.execute('PRAGMA table_info('+table+')')]
                for row in db.execute('SELECT '+','.join(columns)+' FROM '+table):
                    legacy.execute('INSERT INTO '+table+' ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',tuple(row))
        # Old policy retained the previous host's ten attempts after transfer.
        legacy.execute('UPDATE tasks SET fix_attempts_used=10');legacy.commit();legacy.close()
        self.store=Store(legacy_path,clock=lambda:self.now)
        migrated=self.store.detail(task['id'])
        self.assertEqual('running',migrated['status']);self.assertEqual(2,migrated['generation'])
        self.assertEqual(1,migrated['reassignment_count']);self.assertEqual(0,migrated['fix_attempts_used'])
        self.assertEqual(10,migrated['total_fix_attempts']);self.assertEqual(1,len(migrated['receipts']))
        self.store.heartbeat('oliver-mini',task['id'],self.body(active))
        self.assertEqual(11,self.finish(active,'blocked',feature_attempts=11)['total_fix_attempts'])
        self.store=Store(legacy_path,clock=lambda:self.now)
        self.assertEqual(1,self.store.detail(task['id'])['fix_attempts_used'])

    def test_legacy_cumulative_receipt_translates_after_host_change(self):
        self.add();claim=self.store.claim('oliver-mac');self.finish(claim,'blocked',feature_attempts=10)
        self.now+=900;self.pulse();retry=self.store.claim('oliver-mini')
        result=self.finish(retry,'blocked',feature_attempts=11)
        self.assertEqual(1,result['fix_attempts_used']);self.assertEqual(11,result['total_fix_attempts'])

    def test_pending_external_approval_never_retries_until_verified(self):
        task=self.add();old=self.store.claim('oliver-mac')
        self.finish(old,'pending_external_approval',approval_request='existing-request')
        self.now+=86400;self.pulse()
        self.assertIsNone(self.store.claim('oliver-mini')['task'])
        self.assertIsNone(self.store.claim('oliver-mac')['task'])
        shown=self.store.detail(task['id']);self.assertIsNone(shown['next_worker'])
        self.assertEqual(0,shown['fix_attempts_used']);self.assertFalse(shown['ready'])
        body=self.wait_body(old,action='retry')
        with self.assertRaises(Invalid):self.store.reconcile('admin',task['id'],body)
        with self.assertRaises(Conflict):self.store.retry('oliver-mac',task['id'],{**body,'approval_received':True})
        resumed=self.store.reconcile('admin',task['id'],{**body,'approval_received':True})
        self.assertEqual('queued',resumed['status'])
        self.assertEqual('existing-request',resumed['result']['approval_request'])
        self.assertIsNotNone(self.store.claim('oliver-mini')['task'])

    def test_classify_external_approval_preserves_history_and_fences_claims(self):
        task=self.add();old=self.store.claim('oliver-mac')
        body=self.wait_body(old,action='pending_external_approval')
        with self.assertRaises(Conflict):self.store.reconcile('admin',task['id'],body)
        self.finish(old,'blocked',branch='preserved',fix_attempts_used=2)
        with self.assertRaises(Conflict):self.store.reconcile('admin',task['id'],{**body,'generation':0})
        pending=self.store.reconcile('admin',task['id'],body)
        self.assertEqual('pending_external_approval',pending['status'])
        self.assertEqual('preserved',pending['result']['branch']);self.assertIsNone(pending['owner'])
        self.assertEqual(2,pending['fix_attempts_used'])
        self.assertEqual(1,len(self.store.detail(task['id'])['receipts']))
        self.now+=900;self.pulse();self.assertIsNone(self.store.claim('oliver-mini')['task'])

    def test_blocked_retry_respects_dependencies_and_has_one_winner(self):
        task=self.add();old=self.store.claim('oliver-mac');self.finish(old,'blocked')
        dep=self.add('repair')
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET dependencies=? WHERE id=?',(json.dumps([dep['id']]),task['id']))
        self.now+=900;self.pulse()
        self.assertFalse(self.store.detail(task['id'])['ready'])
        self.assertIsNone(self.store.claim('oliver-mini',task['id'])['task'])
        self.finish(self.store.claim('oliver-mini',dep['id']))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            claims=list(pool.map(lambda _:self.store.claim('oliver-mini',task['id']),range(16)))
        self.assertEqual(1,sum(c['task'] is not None for c in claims))
        self.assertEqual(1,self.store.detail(task['id'])['reassignment_count'])

    def test_blocked_retry_defaults_consume_attempts_and_counts_cannot_decrease(self):
        task=self.add();old=self.store.claim('oliver-mac');self.finish(old,'blocked')
        self.assertEqual(1,self.store.detail(task['id'])['fix_attempts_used'])
        self.now+=900;self.pulse();self.store.pulse('oliver-mini',{'capacity':{}})
        # Keep the same host through a hard requirement to verify no reset.
        with self.store.transaction() as db:db.execute('UPDATE tasks SET requirements=? WHERE id=?',(json.dumps({'worker':'oliver-mac'}),task['id']))
        new=self.store.claim('oliver-mac')
        with self.assertRaises(Invalid):self.finish(new,'blocked',fix_attempts_used=0)
        self.finish(new,'blocked')
        self.assertEqual(2,self.store.detail(task['id'])['fix_attempts_used'])

    def test_concurrent_claims_one_winner(self):
        task=self.add()
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results=list(pool.map(lambda n:self.store.claim(['oliver-mac','oliver-mini'][n%2]),range(32)))
        self.assertEqual(1,sum(r['task'] is not None for r in results))
        self.assertEqual(task['id'],next(r['task']['id'] for r in results if r['task']))

    def test_independent_work_no_shared_locks(self):
        self.add('one');self.add('two')
        self.assertIsNotNone(self.store.claim('oliver-mini')['task'])
        self.assertIsNotNone(self.store.claim('oliver-mac')['task'])
        self.assertIsNone(self.store.claim('oliver-mini')['task'])
        self.assertEqual([],self.store.snapshot()['locks'])

    def test_only_engine_rebuild_is_heavy(self):
        task=self.add(compute='heavy')
        self.assertEqual('standard',task['compute'])
        self.assertIsNotNone(self.store.claim('oliver-mac')['task'])

    def test_engine_prefers_mini_and_falls_back_when_offline(self):
        task=self.add(task_type='engine_rebuild')
        self.assertEqual('heavy',task['compute'])
        self.assertIsNone(self.store.claim('oliver-mac')['task'])
        self.now+=91
        self.store.pulse('oliver-mac',{'capacity':{'memory_gb':16,'disk_gb':500,'cpus':8}})
        self.assertIsNotNone(self.store.claim('oliver-mac')['task'])

    def test_stale_heartbeat_does_not_hold_or_reassign(self):
        self.add();c=self.store.claim('oliver-mac');self.now+=1000;self.pulse()
        self.store=Store(self.store.path,clock=lambda:self.now)
        task=self.store.detail(c['task']['id'])
        self.assertEqual('running',task['status']);self.assertTrue(task['heartbeat_stale'])
        self.assertIsNone(self.store.claim('oliver-mini')['task'])
        self.store.heartbeat('oliver-mac',task['id'],self.body(c))
        self.assertFalse(self.store.detail(task['id'])['heartbeat_stale'])
        self.assertEqual('completed',self.finish(c)['status'])

    def test_own_settled_failure_can_retry_without_admin(self):
        self.add();old=self.store.claim('oliver-mac');self.finish(old,'failed')
        body={**self.body(old),'execution_stopped':True,'external_state_checked':True,'evidence':'Exact process stopped, no external effects, correction validated'}
        with self.assertRaises(Conflict):self.store.retry('oliver-mini',old['task']['id'],body)
        self.store.retry('oliver-mac',old['task']['id'],body)
        c=self.store.claim('oliver-mini');self.assertEqual(2,c['task']['generation'])
        with self.assertRaises(Conflict):self.finish(old)
        self.finish(c)

    def test_retry_rejects_unsettled_operation(self):
        self.add();c=self.store.claim('oliver-mac');self.finish(c,'blocked')
        with self.assertRaises(Invalid):self.store.retry('oliver-mac',c['task']['id'],{**self.body(c),'evidence':'No stop proof'})

    def test_requirements_and_foreign_claim_fenced(self):
        self.add(requirements={'memory_gb':20});self.assertIsNone(self.store.claim('oliver-mac')['task'])
        c=self.store.claim('oliver-mini')
        with self.assertRaises(Conflict):self.store.heartbeat('oliver-mac',c['task']['id'],self.body(c))

    def test_submit_and_finish_retries_idempotent(self):
        self.assertEqual(self.add()['id'],self.add()['id'])
        with self.assertRaises(Conflict):self.add(description='Changed')
        c=self.store.claim('oliver-mac');self.assertEqual(self.finish(c),self.finish(c))

    def test_no_token_exposed(self):
        self.add();c=self.store.claim('oliver-mac');s=str(self.store.snapshot())
        self.assertNotIn(c['claim_token'],s);self.assertNotIn('claim_hash',s)

    def test_default_worker_merges_without_publishing(self):
        task=self.add();self.assertEqual(1,len(task['steps']))
        self.assertIn('DO NOT PUBLISH',task['steps'][0]['instructions'])
        with self.assertRaises(Invalid):self.add('direct',steps=[{'name':'Ship','kind':'publish','instructions':'Publish'}])
        with self.assertRaises(Invalid):self.add('fake',task_type='publish')

    def test_explicit_release_starts_now_and_hourly_tick_does_not_duplicate(self):
        self.configure();self.observe()
        self.assertEqual([],self.store.tick_releases())
        ids=self.store.tick_releases('oliverhu/nova-browser');self.assertEqual(1,len(ids))
        t=self.store.detail(ids[0]);self.assertEqual('b'*40,t['result']['release_target']['commit'])
        self.assertEqual(90,t['priority']);self.assertIn('ios_app_store_connect_upload_only',t['delivery_targets'])
        self.assertEqual([],self.store.tick_releases('oliverhu/nova-browser'))
        self.now=3600;self.observe();self.assertEqual([],self.store.tick_releases())

    def test_explicit_release_rejects_stale_or_unknown_repository(self):
        with self.assertRaises(Invalid):self.store.tick_releases('unknown/repo')
        self.configure();self.observe();self.now+=601
        with self.assertRaises(Conflict):self.store.tick_releases('oliverhu/nova-browser')
        self.assertEqual([],self.store.snapshot()['tasks'])

    def test_hourly_check_no_change_no_task(self):
        self.configure();self.now=3600;self.observe('a')
        self.assertEqual([],self.store.tick_releases())
        self.assertEqual(7200,self.store.snapshot()['repositories'][0]['next_check'])

    def test_new_commit_queued_only_on_hour_and_either_worker_claims(self):
        self.configure();self.observe();self.assertEqual([],self.store.tick_releases())
        self.now=3600;self.observe();self.pulse();ids=self.store.tick_releases()
        self.assertEqual(1,len(ids));t=self.store.detail(ids[0])
        self.assertEqual('publish',t['task_type']);self.assertEqual('standard',t['compute'])
        self.assertEqual('b'*40,t['result']['release_target']['commit'])
        self.assertEqual(ids[0],self.store.claim('oliver-mac')['task']['id'])

    def test_restart_and_concurrent_ticks_do_not_duplicate(self):
        self.configure();self.now=3600;self.observe()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.store.tick_releases(),range(12)))
        self.assertEqual(1,sum(len(r) for r in results))
        self.store=Store(self.store.path,clock=lambda:self.now)
        self.assertEqual([],self.store.tick_releases())
        self.assertEqual(1,len(self.store.snapshot()['tasks']))

    def test_new_merges_create_independent_task_at_next_hour(self):
        ident=self.release();c=self.store.claim('oliver-mini')
        self.now=7200;self.observe('c');self.assertEqual(1,len(self.store.tick_releases()))
        self.finish(c,published_commit='b'*40,publication_ref='https://example.test/release')
        self.assertEqual([],self.store.tick_releases())
        self.now=10800;self.observe('c');self.assertEqual([],self.store.tick_releases())

    def test_no_global_publish_lock(self):
        first=self.release();second=self.release('other/repo')
        self.pulse();a=self.store.claim('oliver-mac',first);b=self.store.claim('oliver-mini',second)
        self.assertIsNotNone(a['task']);self.assertIsNotNone(b['task'])
        self.assertEqual([],self.store.snapshot()['locks'])

    def test_failed_publication_not_automatically_repeated(self):
        self.release();c=self.store.claim('oliver-mac');t=self.finish(c,'failed')
        self.assertEqual('failed',t['status']);self.assertIn('release_target',t['result'])
        self.now=7200;self.observe('b');self.assertEqual([],self.store.tick_releases())
        self.now=10800;self.observe('c');self.assertEqual(1,len(self.store.tick_releases()))
        self.add('unrelated');self.pulse();self.assertIsNotNone(self.store.claim('oliver-mac')['task'])

    def test_success_requires_exact_target_and_verified_reference(self):
        self.release();c=self.store.claim('oliver-mini')
        with self.assertRaises(Invalid):self.finish(c,published_commit='c'*40,publication_ref='ref')
        with self.assertRaises(Invalid):self.finish(c,published_commit='b'*40)
        self.assertEqual('a'*40,self.store.snapshot()['repositories'][0]['published_head'])
        done=self.finish(c,published_commit='b'*40,publication_ref='verified:ref')
        self.assertEqual(done,self.finish(c,published_commit='b'*40,publication_ref='verified:ref'))
        self.assertEqual('b'*40,self.store.snapshot()['repositories'][0]['published_head'])

    def test_nova_release_requires_ios_upload_without_distribution(self):
        self.release();c=self.store.claim('oliver-mini')
        self.assertIn('ios_app_store_connect_upload_only',c['task']['delivery_targets'])
        valid={'status':'uploaded','source_commit':'b'*40,'version':'0.2.21','build_number':'21',
               'upload_ref':'apple-upload:verified-fixture','testflight_distributed':False,'submitted_for_review':False}
        for invalid in [None,{}, {**valid,'source_commit':'c'*40},{**valid,'testflight_distributed':True},
                        {**valid,'submitted_for_review':True},{**valid,'upload_ref':''},{**valid,'status':'archived'}]:
            with self.assertRaises(Invalid):
                self.finish(c,published_commit='b'*40,publication_ref='mac-release',ios_upload=invalid)
            self.assertEqual('a'*40,self.store.snapshot()['repositories'][0]['published_head'])
        result=self.finish(c,published_commit='b'*40,publication_ref='mac-release',ios_upload=valid)
        self.assertEqual('completed',result['status'])
        self.assertEqual(valid,result['result']['ios_upload'])

    def test_release_metadata_commit_does_not_create_endless_release(self):
        self.release();c=self.store.claim('oliver-mini')
        self.finish(c,published_commit='b'*40,release_commit='c'*40,publication_ref='verified:ref')
        self.now=7200;self.observe('c');self.assertEqual([],self.store.tick_releases())

    def test_stale_observation_defers_check_and_out_of_order_is_ignored(self):
        self.configure();self.observe();self.now=3600
        self.assertEqual([],self.store.tick_releases());self.assertEqual(3600,self.store.snapshot()['repositories'][0]['next_check'])
        self.observe('c');self.observe('b',at=self.now-10)
        self.assertEqual('c'*40,self.store.snapshot()['repositories'][0]['merged_head'])
        self.assertEqual(1,len(self.store.tick_releases()))

    def test_late_old_receipt_does_not_regress_published_baseline(self):
        self.release();old=self.store.claim('oliver-mini')
        self.now=7200;self.observe('c');self.pulse();new_id=self.store.tick_releases()[0]
        new=self.store.claim('oliver-mac',new_id)
        self.finish(new,published_commit='c'*40,publication_ref='new:ref')
        self.finish(old,published_commit='b'*40,publication_ref='historical:ref')
        self.assertEqual('c'*40,self.store.snapshot()['repositories'][0]['published_head'])

    def test_configuration_does_not_reset_published_baseline(self):
        self.release();c=self.store.claim('oliver-mini');self.finish(c,published_commit='b'*40,publication_ref='ref')
        self.configure();self.assertEqual('b'*40,self.store.snapshot()['repositories'][0]['published_head'])

    def test_legacy_locks_retired_but_task_and_receipts_preserved(self):
        task=self.add();c=self.store.claim('oliver-mac')
        with self.store.connection() as db:db.execute('INSERT INTO locks VALUES(?,?,?,?,?)',('publish',task['id'],1,'oliver-mac',self.now))
        self.store=Store(self.store.path,clock=lambda:self.now)
        self.assertEqual([],self.store.snapshot()['locks'])
        self.assertEqual('running',self.store.detail(task['id'])['status']);self.finish(c)


if __name__=='__main__':unittest.main()
