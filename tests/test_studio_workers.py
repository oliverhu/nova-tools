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

    def test_heavy_work_waits_for_studio_and_never_falls_back(self):
        host = STUDIO_WORKERS[0]
        self.store = Store(self.store.path, clock=lambda: 1000, heavy_worker=host)
        for old in ('oliver-mac', 'oliver-mini'):
            self.store.pulse(old, {'capacity': {'cpus': 30, 'memory_gb': 96}})
        heavy = self.store.submit('admin', {'title': 'Compile engine', 'request_key': 'heavy', 'task_type': 'engine_rebuild'})
        for worker in (*STUDIO_WORKERS[1:], 'oliver-mac', 'oliver-mini'):
            self.assertIsNone(self.store.claim(worker, heavy['id'])['task'])
        self.store.set_worker_state('admin', host, {'paused': True, 'reason': 'Host not ready'})
        self.assertFalse(self.store.detail(heavy['id'])['ready'])
        self.assertEqual(host, self.store.detail(heavy['id'])['next_worker'])
        self.store.set_worker_state('admin', host, {'paused': False, 'reason': 'Host ready'})
        claim = self.store.claim(host, heavy['id'])
        self.assertEqual(host, claim['task']['owner'])
        second = self.store.submit('admin', {'title': 'Second engine', 'request_key': 'heavy2', 'task_type': 'engine_rebuild'})
        self.assertFalse(self.store.detail(second['id'])['ready'])
        self.assertIsNone(self.store.claim(STUDIO_WORKERS[1], second['id'])['task'])
        with self.assertRaises(Invalid):
            self.store.submit('admin', {'title': 'Bad route', 'request_key': 'bad', 'task_type': 'engine_rebuild', 'requirements': {'worker': STUDIO_WORKERS[1]}})
