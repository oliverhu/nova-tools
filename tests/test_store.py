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
        t=c['task'];return self.store.finish(t['owner'],t['id'],{**self.body(c),'outcome':outcome,'receipt':{'summary':'Verified result','execution_stopped':True,**receipt}})

    def configure(self,name='oliverhu/nova-browser'):
        return self.store.configure_repository({'repository':name,'published_commit':'a'*40})

    def observe(self,head='b',repo='oliverhu/nova-browser',at=None):
        return self.store.observe_repository('oliver-mini',{'repository':repo,'branch':'main','commit':head*40,'observed_at':self.now if at is None else at})

    def release(self,repo='oliverhu/nova-browser',head='b'):
        self.configure(repo);self.now=(int(self.now)//3600+1)*3600;self.observe(head,repo);self.pulse()
        return self.store.tick_releases()[0]

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

    def test_new_merges_during_publish_wait_for_next_hour(self):
        ident=self.release();c=self.store.claim('oliver-mini')
        self.now=7200;self.observe('c');self.assertEqual([],self.store.tick_releases())
        self.finish(c,published_commit='b'*40,publication_ref='https://example.test/release')
        self.assertEqual([],self.store.tick_releases())
        self.now=10800;self.observe('c');self.assertEqual(1,len(self.store.tick_releases()))

    def test_no_global_publish_lock(self):
        first=self.release();second=self.release('other/repo')
        self.pulse();a=self.store.claim('oliver-mac',first);b=self.store.claim('oliver-mini',second)
        self.assertIsNotNone(a['task']);self.assertIsNotNone(b['task'])
        self.assertEqual([],self.store.snapshot()['locks'])

    def test_failed_publication_not_automatically_repeated(self):
        self.release();c=self.store.claim('oliver-mac');t=self.finish(c,'failed')
        self.assertEqual('failed',t['status']);self.assertIn('release_target',t['result'])
        self.now=7200;self.observe('c');self.assertEqual([],self.store.tick_releases())
        self.add('unrelated');self.pulse();self.assertIsNotNone(self.store.claim('oliver-mac')['task'])

    def test_success_requires_exact_target_and_verified_reference(self):
        self.release();c=self.store.claim('oliver-mini')
        with self.assertRaises(Invalid):self.finish(c,published_commit='c'*40,publication_ref='ref')
        with self.assertRaises(Invalid):self.finish(c,published_commit='b'*40)
        self.assertEqual('a'*40,self.store.snapshot()['repositories'][0]['published_head'])
        done=self.finish(c,published_commit='b'*40,publication_ref='verified:ref')
        self.assertEqual(done,self.finish(c,published_commit='b'*40,publication_ref='verified:ref'))
        self.assertEqual('b'*40,self.store.snapshot()['repositories'][0]['published_head'])

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
