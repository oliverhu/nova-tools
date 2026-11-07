import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest

from coordinator import app_sessions as app
from coordinator.store import Store, Conflict, Invalid


class AppSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.now = 1000
        self.store = Store(Path(self.tmp.name) / 'db', clock=lambda: self.now)
        self.worker = 'oliverhu-mac-studio'
        self.store.pulse(self.worker, {'capacity': {'cpus': 12, 'memory_gb': 32, 'disk_gb': 500, 'capabilities': ['development', 'gui']}})
        self.policy = {'expected_revision': 0, 'enabled': True, 'source_commit': 'a' * 40, 'inventory_sha256': 'b' * 64, 'evidence': 'Reviewed repository and verified worker onboarding'}
        app.configure(self.store, 'admin', self.worker, self.policy)
        self.registration = {'session_id': 'new-chat', 'expected_epoch': 0, 'host_id': 'test-host', 'account_sha256': 'c' * 64, 'session_token': 'd' * 64}
        app.register(self.store, self.worker, self.registration)
        self.binding = {'session_id': 'new-chat', 'epoch': 1, 'token': 'd' * 64}
        self.adopt()
        self.qualify()

    def adopt(self):
        return app.adopt(self.store, self.worker, {'binding': self.binding, 'policy_revision': 1, 'source_commit': 'a' * 40, 'inventory_sha256': 'b' * 64, 'instructions_read': True})

    def qualify(self, capabilities=None):
        return app.qualify(self.store, 'admin', self.worker, {'session_id': self.binding['session_id'], 'epoch': self.binding['epoch'], 'policy_revision': 1, 'capabilities': capabilities or ['development'], 'valid_until': self.now + 100, 'evidence_sha256': 'e' * 64})

    def task(self, key='task', capabilities=None):
        return self.store.submit('admin', {'request_key': key, 'title': key, 'requirements': {'worker': self.worker, 'capabilities': capabilities or ['development']}})

    def claim(self, task):
        return self.store.claim(self.worker, task['id'], app_binding=self.binding)

    def outcome(self, claim, outcome='completed'):
        return {'claim_token': claim['claim_token'], 'generation': claim['task']['generation'], 'step': 0,
                'app_binding': self.binding, 'outcome': outcome, 'receipt': {'summary': 'Actual test result', 'execution_stopped': True}}

    def test_claim_heartbeat_finish_roundtrip_and_no_token_projection(self):
        c = self.claim(self.task()); self.assertIsNotNone(c['task'])
        self.store.heartbeat(self.worker, c['task']['id'], {**self.outcome(c), 'progress': 'tested'})
        result = self.store.finish(self.worker, c['task']['id'], self.outcome(c))
        self.assertEqual('completed', result['status'])
        public = json.dumps([app.status(self.store, self.worker), self.store.snapshot()])
        self.assertNotIn(self.registration['session_token'], public)
        self.assertNotIn('token_hash', public)

    def test_legacy_cli_cannot_claim_enrolled_worker(self):
        t = self.task()
        with self.assertRaises(Conflict): self.store.claim(self.worker, t['id'])
        self.assertEqual('queued', self.store.detail(t['id'])['status'])

    def test_account_metadata_is_optional_but_session_authentication_is_not(self):
        replacement = {**self.registration, 'session_id': 'account-independent-chat',
                       'account_sha256': None, 'expected_epoch': 1, 'session_token': 'e' * 64}
        app.register(self.store, self.worker, replacement)
        self.assertIsNone(app.status(self.store, self.worker)['session']['registration']['account_sha256'])
        with self.assertRaises(Conflict): self.claim(self.task())
        with self.assertRaises(Invalid):
            app.register(self.store, self.worker, {**replacement, 'account_sha256': 'unverified'})

    def test_wrong_session_cannot_heartbeat_finish_or_defer(self):
        c = self.claim(self.task()); bad = {**self.outcome(c), 'app_binding': {**self.binding, 'token': 'wrong'}}
        for function in (self.store.heartbeat, self.store.finish):
            with self.assertRaises(Conflict): function(self.worker, c['task']['id'], bad)
        with self.assertRaises(Conflict):
            self.store.defer(self.worker, c['task']['id'], {**bad, 'execution_stopped': True, 'external_state_checked': True, 'evidence': 'stopped', 'worker': self.worker})

    def test_policy_change_invalidates_adoption_and_qualification(self):
        app.configure(self.store, 'admin', self.worker, {**self.policy, 'expected_revision': 1, 'source_commit': 'f' * 40})
        with self.assertRaises(Conflict): self.claim(self.task())
        with self.assertRaises(Conflict): self.adopt()

    def test_missing_capability_prevents_claim(self):
        task = self.task(capabilities=['gui'])
        self.assertIsNone(self.claim(task)['task'])
        self.assertFalse(self.store.detail(task['id'])['ready'])
        self.qualify(['development', 'gui'])
        self.assertIsNotNone(self.claim(task)['task'])

    def test_expired_readiness_stops_effects_but_permits_truthful_cleanup(self):
        c = self.claim(self.task()); self.now += 101
        with self.assertRaises(Conflict): self.store.heartbeat(self.worker, c['task']['id'], self.outcome(c))
        with self.assertRaises(Conflict): self.store.finish(self.worker, c['task']['id'], self.outcome(c))
        self.store.heartbeat(self.worker, c['task']['id'], {**self.outcome(c), 'phase': 'cleanup'})
        result = self.store.finish(self.worker, c['task']['id'], self.outcome(c, 'blocked'))
        self.assertEqual('blocked', result['status'])

    def test_replacement_preserves_active_claim_then_fences_old_session(self):
        c = self.claim(self.task())
        new = {**self.registration, 'session_id': 'replacement', 'expected_epoch': 1, 'session_token': 'x' * 64}
        with self.assertRaises(Conflict): app.register(self.store, self.worker, new)
        with self.assertRaises(Conflict): app.configure(self.store, 'admin', self.worker, {**self.policy, 'expected_revision': 1})
        self.store.finish(self.worker, c['task']['id'], self.outcome(c))
        self.assertEqual(2, app.register(self.store, self.worker, new)['epoch'])
        with self.assertRaises(Conflict): self.claim(self.task('second'))
        with self.assertRaises(Conflict): app.register(self.store, self.worker, self.registration)

    def test_register_and_policy_retry_idempotency(self):
        self.assertTrue(app.register(self.store, self.worker, self.registration)['duplicate'])
        self.assertTrue(app.configure(self.store, 'admin', self.worker, self.policy)['duplicate'])
        with self.assertRaises(Conflict):
            app.register(self.store, self.worker, {**self.registration, 'account_sha256': 'f' * 64})

    def test_concurrent_claim_has_one_owner(self):
        t = self.task()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.claim(t), range(2)))
        self.assertEqual(1, sum(r['task'] is not None for r in results))

    def test_unauthorized_qualification_and_registration_refused(self):
        with self.assertRaises(Invalid): app.configure(self.store, self.worker, self.worker, self.policy)
        with self.assertRaises(Invalid): app.register(self.store, 'admin', self.registration)
        with self.assertRaises(Conflict):
            app.adopt(self.store, 'oliverhu-mac-studio-vm1', {'binding': self.binding, 'policy_revision': 1, 'source_commit': 'a' * 40, 'inventory_sha256': 'b' * 64, 'instructions_read': True})

    def test_durable_inbox_duplicate_and_wrong_actor(self):
        c = self.claim(self.task()); t = c['task']
        message = {'app_binding': self.binding, 'request_id': '1' * 32, 'recipient': 'coordinator', 'task_id': t['id'], 'generation': t['generation'], 'kind': 'handoff', 'summary': 'Acceptance evidence is ready', 'artifact_refs': ['artifact:sha256:abc']}
        r = app.post_message(self.store, self.worker, message)
        self.assertTrue(app.post_message(self.store, self.worker, message)['duplicate'])
        with self.assertRaises(Conflict): app.post_message(self.store, 'oliverhu-mac-studio-vm1', message)
        with self.assertRaises(Conflict): app.post_message(self.store, self.worker, {**message, 'summary': 'changed'})
        again = Store(self.store.path, clock=lambda: self.now)
        inbox = app.inbox(again, 'admin', {'after': 0, 'limit': 10})
        self.assertEqual([r['id']], [m['id'] for m in inbox['messages']])
        self.assertEqual([], app.inbox(again, self.worker, {'after': 0, 'limit': 10})['messages'])
        self.assertEqual([], app.inbox(again, 'admin', {'after': inbox['next_cursor'], 'limit': 10})['messages'])


if __name__ == '__main__': unittest.main()
