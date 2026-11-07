import concurrent.futures
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest

from coordinator import event_dispatch as dispatch
from coordinator.store import Store, Conflict, Invalid
from coordinator.workers import MINI_VM_WORKERS


class EventDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.
        self.store = Store(Path(self.temp.name)/'db', clock=lambda: self.now)
        self.worker = MINI_VM_WORKERS[0]
        self.revision = 'a'*64
        self.token = 'secret-token-for-exact-local-wake-0001'
        self.sequence = 0
        for worker in MINI_VM_WORKERS:
            self.store.set_worker_state('admin', worker, {'paused': False, 'reason': 'qualified fixture'})
            self.store.pulse(worker, {'capacity': {'cpus': 5, 'memory_gb': 8, 'disk_gb': 100,
                                                  'capabilities': ['macos', 'cli-shell']}})
            dispatch.configure(self.store, 'admin', worker, {'enabled': True,
                'readiness_revision': self.revision, 'evidence': 'exact fixture onboarding and source verified'})
        self.available()

    def tearDown(self):
        self.temp.cleanup()

    def available(self, **delta):
        self.sequence += 1
        body = {'sequence': self.sequence, 'readiness_revision': self.revision, 'availability': 'available',
                'latest_run_id': None, 'settlement_sha256': 'b'*64, 'issue': None, **delta}
        return dispatch.report(self.store, self.worker, body)

    def add(self, key='work', **delta):
        return self.store.submit('admin', {'request_key': key, 'title': key,
            'requirements': {'worker': self.worker}, **delta})

    def reservation(self, task, ident='c'):
        return {'wake_id': ident*32, 'run_id': ident*32, 'task_id': task['id'],
                'queued_generation': task['generation'], 'readiness_revision': self.revision,
                'token_hash': hashlib.sha256(self.token.encode()).hexdigest()}

    def reserve(self, task, ident='c'):
        body = self.reservation(task, ident)
        dispatch.reserve(self.store, self.worker, body)
        return {'wake_id': body['wake_id'], 'run_id': body['run_id'], 'token': self.token}

    def finish(self, claim, outcome='completed'):
        task = claim['task']
        return self.store.finish(self.worker, task['id'], {'claim_token': claim['claim_token'],
            'generation': task['generation'], 'step': task['step'], 'outcome': outcome,
            'receipt': {'summary': 'actual fixture cleanup verified', 'acceptance_complete': outcome == 'completed',
                        'execution_stopped': True}})

    def settlement(self, binding, generation=1, outcome='completed'):
        return {**binding, 'generation': generation, 'result_sha256': 'd'*64, 'outcome': outcome,
                'local_lock_released': True, 'readiness_revision': self.revision,
                'failure_classification': None,
                'execution_stopped': True, 'input_settled': True, 'unknown_operations': [],
                'unsettled_operations': [], 'unreceived_message_ids': []}

    def test_dependency_commit_notifies_existing_event_wait(self):
        prerequisite = self.add('prerequisite', requirements={'worker': MINI_VM_WORKERS[1]})
        # The other worker is not enrolled as locally available yet, so an
        # ordinary non-event claim is disabled; explicitly unenroll the fixture.
        dispatch.configure(self.store, 'admin', MINI_VM_WORKERS[1], {'enabled': False,
            'readiness_revision': self.revision, 'evidence': 'manual prerequisite fixture'})
        claim = self.store.claim(MINI_VM_WORKERS[1], prerequisite['id'])
        task = self.add(dependencies=[prerequisite['id']])
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(dispatch.next_candidate, self.store, self.worker, 2)
            self.assertFalse(future.done())
            self.store.finish(MINI_VM_WORKERS[1], prerequisite['id'], {
                'claim_token': claim['claim_token'], 'generation': 1, 'step': 0, 'outcome': 'completed',
                'receipt': {'summary': 'dependency complete', 'execution_stopped': True}})
            self.assertEqual(future.result(timeout=2), {'task_id': task['id'], 'queued_generation': 0})

    def test_finish_does_not_release_local_supervisor_or_duplicate_wake(self):
        first = self.add('first'); second = self.add('second')
        binding = self.reserve(first)
        claim = self.store.claim(self.worker, first['id'], binding)
        self.finish(claim)
        self.assertIsNone(dispatch.next_candidate(self.store, self.worker)['task_id'])
        with self.assertRaises(Conflict): self.available()
        with self.assertRaises(Conflict): dispatch.reserve(self.store, self.worker, self.reservation(second, 'e'))
        dispatch.settle(self.store, self.worker, self.settlement(binding))
        self.available(latest_run_id=binding['run_id'])
        self.assertEqual(dispatch.next_candidate(self.store, self.worker)['task_id'], second['id'])

    def test_simultaneous_event_and_fallback_reserve_only_once(self):
        task = self.add()
        def attempt(i):
            try:
                return dispatch.reserve(self.store, self.worker, self.reservation(task, str(i)))
            except Conflict:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(1, 9)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertIsNone(self.store.claim(self.worker, task['id'])['task'])

    def test_exact_token_generation_and_foreign_worker_claim_fences(self):
        task = self.add(requirements={}); binding = self.reserve(task)
        for worker, value in [(self.worker, None), (self.worker, {**binding, 'token': 'wrong'*8}),
                              (MINI_VM_WORKERS[1], binding)]:
            self.assertIsNone(self.store.claim(worker, task['id'], value)['task'])
        claim = self.store.claim(self.worker, task['id'], binding)
        self.assertEqual(claim['task']['generation'], 1)
        self.assertIsNone(self.store.claim(self.worker, task['id'], binding)['task'])

    def test_restart_or_elapsed_time_never_replays_ambiguous_delivery(self):
        task = self.add(); request = self.reservation(task)
        dispatch.reserve(self.store, self.worker, request)
        self.now += 100000
        restarted = Store(self.store.path, clock=lambda: self.now)
        self.assertTrue(dispatch.reserve(restarted, self.worker, request)['duplicate'])
        self.assertIsNone(dispatch.next_candidate(restarted, self.worker)['task_id'])
        with self.assertRaises(Conflict):
            dispatch.reserve(restarted, self.worker, self.reservation(task, 'e'))
        with self.assertRaises(Conflict):
            dispatch.configure(restarted, 'admin', self.worker, {'enabled': False,
                'readiness_revision': self.revision, 'evidence': 'cannot drop an unknown wake'})

    def test_busy_stale_unqualified_paused_and_approval_work_never_wake(self):
        task = self.add()
        for availability in ('busy', 'unknown'):
            self.available(availability=availability)
            self.assertIsNone(dispatch.next_candidate(self.store, self.worker)['task_id'])
        self.available(); self.now += 91
        self.store.pulse(self.worker, {'capacity': {'cpus': 5, 'memory_gb': 8, 'disk_gb': 100}})
        self.assertIsNone(dispatch.next_candidate(self.store, self.worker)['task_id'])
        with self.assertRaises(Conflict): self.available(readiness_revision='f'*64)
        self.available()
        self.store.set_worker_state('admin', self.worker, {'paused': True, 'reason': 'actual stop'})
        self.assertIsNone(dispatch.next_candidate(self.store, self.worker)['task_id'])

    def test_pilot_filter_does_not_wake_unrelated_eligible_work(self):
        unrelated = self.add('unrelated', priority=100)
        pilot = self.add('pilot')
        dispatch.configure(self.store, 'admin', self.worker, {'enabled': True,
            'readiness_revision': self.revision, 'evidence': 'exact pilot only', 'task_ids': [pilot['id']]})
        self.available()
        self.assertEqual(dispatch.next_candidate(self.store, self.worker)['task_id'], pilot['id'])
        with self.assertRaises(Conflict): dispatch.reserve(self.store, self.worker, self.reservation(unrelated))

    def test_settled_approval_capacity_never_retries_the_approval_task(self):
        task = self.add(); binding = self.reserve(task)
        claim = self.store.claim(self.worker, task['id'], binding)
        self.finish(claim, 'pending_external_approval')
        dispatch.settle(self.store, self.worker, self.settlement(binding, outcome='pending_external_approval'))
        self.assertEqual(self.store.detail(task['id'])['status'], 'pending_external_approval')
        self.assertIsNone(dispatch.next_candidate(self.store, self.worker)['task_id'])

    def test_unsettled_or_unread_messages_cannot_release_claimed_wake(self):
        task = self.add(); binding = self.reserve(task)
        claim = self.store.claim(self.worker, task['id'], binding); self.finish(claim)
        original = self.settlement(binding)
        for delta in ({'unknown_operations': ['input']}, {'unreceived_message_ids': ['message']},
                      {'input_settled': False}, {'execution_stopped': False}, {'generation': 2}):
            with self.assertRaises((Invalid, Conflict)):
                dispatch.settle(self.store, self.worker, {**original, **delta})
        self.assertFalse(dispatch.settle(self.store, self.worker, original)['duplicate'])
        self.assertTrue(dispatch.settle(self.store, self.worker, original)['duplicate'])

    def test_private_token_not_projected_and_missing_test_time_remains_null(self):
        task = self.add(); binding = self.reserve(task)
        claim = self.store.claim(self.worker, task['id'], binding)
        snapshot = self.store.snapshot()
        worker = next(w for w in snapshot['workers'] if w['id'] == self.worker)
        self.assertNotIn(self.token, str(snapshot)); self.assertNotIn('token_hash', str(worker))
        self.assertIsNone(worker['event_dispatch']['latest_run']['time_to_first_test_seconds'])
        self.now += 3
        self.store.heartbeat(self.worker, task['id'], {'claim_token': claim['claim_token'], 'generation': 1,
                                                    'phase': 'testing', 'progress': 'Explicit native test begins'})
        worker = next(w for w in self.store.snapshot()['workers'] if w['id'] == self.worker)
        self.assertEqual(worker['event_dispatch']['latest_run']['time_to_first_test_seconds'], 3)

    def test_projection_stale_reserved_and_audited_metrics_keep_distinct_meanings(self):
        task = self.add(); binding = self.reserve(task)
        worker = next(w for w in self.store.snapshot()['workers'] if w['id'] == self.worker)
        projection = worker['event_dispatch']
        self.assertIsNone(projection['latest_run']['blocker'])
        self.assertEqual(projection['latest_run']['next_action'], 'Waiting for worker to claim this task')
        self.assertIsNone(projection['metrics']['harness_failure_count'])
        claim = self.store.claim(self.worker, task['id'], binding)
        self.finish(claim, 'blocked')
        with self.store.transaction() as db:
            self.store.event(db, 'admin', 'approval-applied', task['id'], 'explicit unit audit fixture')
        dispatch.settle(self.store, self.worker, {**self.settlement(binding, outcome='blocked'),
                                                'failure_classification': 'harness'})
        self.now += 91
        worker = next(w for w in self.store.snapshot()['workers'] if w['id'] == self.worker)
        projection = worker['event_dispatch']
        self.assertEqual(projection['availability'], 'unknown'); self.assertTrue(projection['stale'])
        self.assertEqual(projection['metrics']['harness_failure_count'], 1)
        self.assertEqual(projection['metrics']['manual_approval_count'], 1)
        self.assertEqual(projection['metrics']['manual_intervention_count'], 1)
        self.assertIn('stale', projection['latest_run']['blocker'])


if __name__ == '__main__':
    unittest.main()
