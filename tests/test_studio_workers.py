import json
import tempfile
import unittest
from pathlib import Path
from coordinator.store import Store, Conflict, Invalid
from coordinator.workers import STUDIO_WORKERS

class StudioWorkersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'db', clock=lambda: 1000)
        for worker in STUDIO_WORKERS:
            self.store.pulse(worker, {'capacity': {'cpus': 8, 'memory_gb': 24, 'disk_gb': 500}})

    def tearDown(self):
        self.temp.cleanup()

    def test_three_distinct_claims_and_legacy_retirement_preserve_holds(self):
        held = self.store.submit('admin', {'title': 'Approval hold', 'request_key': 'hold'})
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='pending_external_approval',owner='oliver-mac' WHERE id=?", (held['id'],))
        before = self.store.detail(held['id'])
        for worker in ('oliver-mac', 'oliver-mini'):
            self.store.set_worker_state('admin', worker, {'paused': True, 'reason': 'Verified Studio migration'})
            self.store.pulse(worker, {'capacity': {'cpus': 30}})
            self.assertIsNone(self.store.claim(worker)['task'])
        self.assertEqual(before, self.store.detail(held['id']))
        claims = []
        for i, worker in enumerate(STUDIO_WORKERS):
            task = self.store.submit('admin', {'title': worker, 'request_key': str(i), 'requirements': {'worker': worker}})
            claims.append(self.store.claim(worker, task['id']))
        self.assertEqual(3, len({c['task']['id'] for c in claims}))
        self.assertEqual(set(STUDIO_WORKERS), {c['task']['owner'] for c in claims})
        self.assertEqual(3, len({c['claim_token'] for c in claims}))

    def test_cannot_pause_active_work_or_mutate_as_worker(self):
        worker = STUDIO_WORKERS[0]
        with self.assertRaises(Invalid):
            self.store.set_worker_state(worker, worker, {'paused': True, 'reason': 'No admin authority'})
        task = self.store.submit('admin', {'title': 'Active', 'request_key': 'active'})
        self.store.claim(worker, task['id'])
        with self.assertRaises(Conflict):
            self.store.set_worker_state('admin', worker, {'paused': True, 'reason': 'Cannot abandon claim'})

    def test_retirement_survives_restart_and_pulse(self):
        worker = STUDIO_WORKERS[1]
        self.store.set_worker_state('admin', worker, {'paused': True, 'reason': 'Maintenance'})
        self.store = Store(self.store.path, clock=lambda: 1000)
        self.store.pulse(worker, {'capacity': {'cpus': 8}})
        self.assertTrue(next(w for w in self.store.snapshot()['workers'] if w['id'] == worker)['paused'])
