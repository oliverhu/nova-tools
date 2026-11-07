import tempfile
import unittest
from pathlib import Path

from coordinator.store import Store, Conflict, Invalid


class RunningDiscardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1000
        self.store = Store(Path(self.tmp.name) / 'db', clock=lambda: self.now)
        self.worker = 'oliverhu-mac-studio-vm1'
        self.store.pulse(self.worker, {'capacity': {'cpus': 8, 'memory_gb': 24,
                         'disk_gb': 500, 'capabilities': ['development']}})
        self.task = self.store.submit('admin', {'request_key': 'discard', 'title': 'discard',
                                     'requirements': {'worker': self.worker}})
        self.claim = self.store.claim(self.worker, self.task['id'])
        self.body = {'action': 'cancel', 'generation': 1, 'expected_owner': self.worker,
                     'expected_updated': self.now, 'execution_stopped': True,
                     'external_state_checked': True, 'evidence': 'Idle owner, settled commands and source preserved',
                     'discard_decision': {'thread_id': 'human-chat', 'response': 'Discard active work is ok', 'verified': True}}

    def cancel(self, body=None, actor='admin'):
        return self.store.reconcile(actor, self.task['id'], body or self.body)

    def test_cancellation_fences_claim_pauses_worker_and_preserves_history(self):
        result = self.cancel()
        self.assertEqual('cancelled', result['status'])
        self.assertIsNone(result['owner'])
        self.assertEqual(self.worker, result['last_owner'])
        self.assertEqual(0, result['step'])
        self.assertEqual(1, result['generation'])
        detail = self.store.detail(self.task['id'])
        event = next(e for e in detail['events'] if e['kind'] == 'human-discarded-running-task')
        self.assertNotIn(self.claim['claim_token'], event['detail'])
        with self.assertRaises(Conflict):
            self.store.heartbeat(self.worker, self.task['id'], {'generation': 1,
                'claim_token': self.claim['claim_token'], 'progress': 'stale'})
        self.assertTrue(next(w for w in self.store.snapshot()['workers'] if w['id'] == self.worker)['paused'])
        with self.assertRaises(Conflict): self.cancel()
        self.assertEqual('cancelled', self.store.detail(self.task['id'])['status'])

    def test_missing_authority_or_settlement_rejected(self):
        with self.assertRaises(Conflict): self.cancel({**self.body, 'discard_decision': None})
        for key, value in [('discard_decision', {}), ('execution_stopped', False),
                           ('external_state_checked', False)]:
            with self.subTest(key=key), self.assertRaises(Invalid): self.cancel({**self.body, key: value})
        with self.assertRaises(Conflict): self.cancel(actor=self.worker)
        self.assertEqual('running', self.store.detail(self.task['id'])['status'])

    def test_generation_owner_and_update_races_rejected(self):
        for key, value in [('generation', 2), ('expected_owner', 'oliver-mini'), ('expected_updated', 999)]:
            with self.subTest(key=key), self.assertRaises(Conflict): self.cancel({**self.body, key: value})
        self.now += 1
        self.store.heartbeat(self.worker, self.task['id'], {'generation': 1,
            'claim_token': self.claim['claim_token'], 'progress': 'owner resumed'})
        with self.assertRaises(Conflict): self.cancel()

    def test_not_an_admin_completion_or_retry_escape(self):
        for action in ['complete', 'retry', 'pending_external_approval']:
            with self.subTest(action=action), self.assertRaises(Conflict): self.cancel({**self.body, 'action': action})
        self.assertEqual('running', self.store.detail(self.task['id'])['status'])

    def test_resource_locks_are_retained(self):
        with self.store.transaction() as db:
            db.execute('INSERT INTO locks VALUES(?,?,?,?,?)', ('test-resource', self.task['id'], 1, self.worker, self.now))
        with self.assertRaises(Conflict): self.cancel()
        with self.store.transaction() as db:
            self.assertIsNotNone(db.execute('SELECT * FROM locks WHERE task_id=?', (self.task['id'],)).fetchone())

    def test_park_preserves_task_but_requires_waiting_readiness(self):
        from coordinator import task_readiness
        body = {**self.body, 'action': 'park', 'pause_decision': self.body['discard_decision'], 'readiness_revision': 1}
        with self.assertRaises(Conflict): self.cancel(body)
        detail = self.store.detail(self.task['id'])
        task_readiness.update(self.store, 'admin', self.task['id'], {
            'request_id': 'a'*32, 'generation': 1, 'expected_revision': 0,
            'context_sha256': detail['task_readiness']['context_sha256'], 'worker': self.worker,
            'state': 'waiting', 'valid_until': self.now+100, 'checks': [
                {'name': 'human-resume', 'state': 'missing', 'evidence_sha256': 'b'*64}],
            'decisions': [], 'evidence': 'Human asked to skip this work for now'})
        with self.assertRaises(Conflict): self.cancel({**body, 'readiness_revision': 0})
        result = self.cancel(body)
        self.assertEqual('queued', result['status'])
        self.assertEqual(0, result['step'])
        self.assertEqual(1, result['generation'])
        self.assertIsNone(result['owner'])
        self.assertFalse(self.store.detail(self.task['id'])['ready'])
        self.now += 200
        self.assertFalse(self.store.detail(self.task['id'])['ready'])

    def test_park_is_not_a_worker_retry_or_completion(self):
        with self.assertRaises(Conflict): self.cancel({**self.body, 'action': 'park'}, actor=self.worker)


if __name__ == '__main__': unittest.main()
