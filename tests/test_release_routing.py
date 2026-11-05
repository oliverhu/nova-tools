"""Combined Nova releases require the currently qualified Studio publisher."""
from pathlib import Path
import tempfile
import unittest

from coordinator.store import Store, encoded
from coordinator.workers import STUDIO_WORKERS


STUDIO, VM1, VM2 = STUDIO_WORKERS
NOVA = 'oliverhu/nova-browser'


class ReleaseRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.now = 1000.0
        self.store = Store(Path(self.temp.name) / 'state', clock=lambda: self.now)
        self.pulse()

    def tearDown(self):
        self.temp.cleanup()

    def pulse(self, workers=STUDIO_WORKERS):
        for worker in workers:
            self.store.pulse(worker, {'capacity': {'cpus': 12, 'memory_gb': 24,
                'disk_gb': 500, 'capabilities': ['macos', 'development']}})

    def create(self, head='b', manual=False, repository=NOVA):
        self.store.configure_repository({'repository': repository, 'published_commit': 'a' * 40})
        if not manual:
            self.now = (int(self.now) // 3600 + 1) * 3600
        self.pulse()
        self.store.observe_repository(STUDIO, {'repository': repository, 'commit': head * 40,
                                              'observed_at': self.now})
        return self.store.tick_releases(repository if manual else None)[0]

    def raw(self, ident):
        with self.store.connection() as db:
            return dict(db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone())

    def test_hourly_and_manual_use_hard_studio_requirement_and_keep_pinned_targets(self):
        manual = self.create(manual=True); hourly = self.create('c')
        for ident, head, submitter in ((manual, 'b', 'admin'), (hourly, 'c', 'hourly-scheduler')):
            row = self.store.detail(ident)
            self.assertEqual({'worker': STUDIO}, row['requirements'])
            self.assertEqual({'repository': NOVA, 'branch': 'main', 'commit': head * 40},
                             row['result']['release_target'])
            self.assertEqual(submitter, row['submitted_by'])
            self.assertEqual(['macos_public_release', 'ios_app_store_connect_upload_only'], row['delivery_targets'])
            self.assertIn('Upload only:', row['steps'][0]['instructions'])
            self.assertNotIn('Either Mac may claim', row['steps'][0]['instructions'])
            before = self.raw(ident)
            for worker in (VM1, VM2):
                self.assertIsNone(self.store.claim(worker, ident)['task'])
                self.assertEqual(before, self.raw(ident))
        self.assertEqual(manual, self.store.claim(STUDIO, manual)['task']['id'])

    def test_unfiltered_guest_next_skips_release_and_continues_ordinary_work(self):
        publisher = self.create(); before = self.store.detail(publisher)
        for index, worker in enumerate((VM1, VM2)):
            task = self.store.submit('admin', {'request_key': 'feature-' + str(index),
                'title': 'Independent feature', 'requirements': {'worker': worker}})
            claim = self.store.claim(worker)
            self.assertEqual(task['id'], claim['task']['id'])
            self.assertEqual(worker, claim['task']['owner'])
            self.assertEqual(before, self.store.detail(publisher))

    def test_busy_offline_or_paused_studio_waits_without_guest_fallback(self):
        publisher = self.create()
        work = self.store.submit('admin', {'request_key': 'studio-feature', 'title': 'Active work',
                                          'requirements': {'worker': STUDIO}})
        claim = self.store.claim(STUDIO, work['id']); active = self.raw(work['id'])
        for worker in (VM1, VM2):
            self.assertIsNone(self.store.claim(worker, publisher)['task'])
        self.assertFalse(self.store.detail(publisher)['ready'])
        self.assertEqual(active, self.raw(work['id']))
        self.store.finish(STUDIO, work['id'], {'generation': claim['task']['generation'],
            'step': 0, 'claim_token': claim['claim_token'], 'outcome': 'completed',
            'receipt': {'summary': 'Owned feature completed', 'execution_stopped': True}})
        self.now += 91; self.pulse((VM1, VM2))
        self.assertFalse(self.store.detail(publisher)['ready'])
        self.assertIsNone(self.store.claim(VM1, publisher)['task'])
        self.pulse((STUDIO,))
        self.store.set_worker_state('admin', STUDIO, {'paused': True, 'reason': 'Existing readiness maintenance'})
        self.assertFalse(self.store.detail(publisher)['ready'])
        self.assertIsNone(self.store.claim(VM2, publisher)['task'])
        self.store.set_worker_state('admin', STUDIO, {'paused': False, 'reason': 'Existing readiness restored'})
        self.assertEqual(STUDIO, self.store.claim(STUDIO, publisher)['task']['owner'])

    def test_existing_publisher_hold_and_identity_survive_restart_and_future_creation(self):
        old = self.create()
        # Model a pre-upgrade task; future creation must not rewrite its history.
        with self.store.connection() as db:
            db.execute('UPDATE tasks SET requirements=? WHERE id=?', (encoded({}), old))
        claim = self.store.claim(VM1, old)
        self.store.finish(VM1, old, {'generation': claim['task']['generation'], 'step': 0,
            'claim_token': claim['claim_token'], 'outcome': 'pending_external_approval',
            'receipt': {'summary': 'Genuine existing human decision outstanding', 'execution_stopped': True,
                        'approval_request': {'thread_id': 'original', 'question_id': 'actual-decision'}}})
        before = self.store.detail(old); raw = self.raw(old)
        self.store = Store(self.store.path, clock=lambda: self.now)
        self.assertEqual([], self.store.tick_releases(NOVA))
        new = self.create('c')
        self.assertEqual(before, self.store.detail(old)); self.assertEqual(raw, self.raw(old))
        self.assertNotEqual(old, new)
        self.assertEqual({'worker': STUDIO}, self.store.detail(new)['requirements'])
        self.assertEqual('c' * 40, self.store.detail(new)['result']['release_target']['commit'])
        self.assertEqual([], self.store.tick_releases(NOVA))
        self.assertEqual(self.now + 3600, self.store.snapshot()['repositories'][0]['next_check'])

    def test_existing_running_guest_claim_is_not_stolen_or_modified(self):
        old = self.create()
        with self.store.connection() as db:
            db.execute('UPDATE tasks SET requirements=? WHERE id=?', (encoded({}), old))
        claim = self.store.claim(VM2, old); raw = self.raw(old); before = self.store.detail(old)
        self.create('c')
        after = self.store.detail(old)
        # Advancing to another hour changes only this derived age diagnostic.
        before.pop('heartbeat_stale'); after.pop('heartbeat_stale')
        self.assertEqual(raw, self.raw(old)); self.assertEqual(before, after)
        self.store.heartbeat(VM2, old, {'generation': claim['task']['generation'],
                                      'claim_token': claim['claim_token']})

    def test_other_repository_publishers_keep_existing_worker_eligibility(self):
        ident = self.create(repository='owner/another-repo')
        row = self.store.detail(ident)
        self.assertEqual({}, row['requirements'])
        self.assertNotIn('ios_app_store_connect_upload_only', row.get('delivery_targets', []))
        self.assertEqual(VM1, self.store.claim(VM1, ident)['task']['owner'])


if __name__ == '__main__':
    unittest.main()
