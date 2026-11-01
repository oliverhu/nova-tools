import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from coordinator.store import Store, Conflict, Invalid


class AttemptExceptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db'
        self.store = Store(self.path, clock=lambda: 1000)
        for worker in ('oliver-mini', 'oliver-mac'):
            self.store.pulse(worker, {'capacity': {'cpus': 12}})

    def tearDown(self):
        self.temp.cleanup()

    def prepared(self, name='feature', used=10):
        task = self.store.submit('oliver-mini', {'request_key': name, 'title': name,
            'requirements': {'worker': 'oliver-mini'}})
        claim = self.store.claim('oliver-mini', task['id'])
        self.finish(claim, 'pending_external_approval', used)
        source = {'thread_id': 'main', 'question_id': name, 'message_id': name + '-human-answer'}
        approval = self.store.apply_approval('oliver-mini', {
            'source': source, 'decision': 'approved', 'decision_verified': True,
            'response': 'Approve exactly attempt 11 on Mini', 'scope': 'One bounded extra attempt; preserve all history',
            'tasks': [{'id': task['id'], 'generation': 1, 'claim_token': claim['claim_token'],
                       'execution_stopped': True, 'external_state_checked': True,
                       'scope_evidence': 'The actual human answer explicitly permits this task attempt 11.'}]})
        return {'approval_id': approval['approval_id'], 'source': source, 'task': task['id'],
                'generation': 1, 'worker': 'oliver-mini', 'expected_used': 10, 'attempt_number': 11,
                'scope_evidence': 'Verified exact human response: Approve exactly attempt 11 on Mini',
                'exception_verified': True, 'execution_stopped': True, 'external_state_checked': True}

    def finish(self, claim, outcome, count):
        t = claim['task']
        return self.store.finish(t['owner'], t['id'], {'claim_token': claim['claim_token'],
            'generation': t['generation'], 'step': t['step'], 'outcome': outcome,
            'receipt': {'summary': 'Owned operations settled', 'execution_stopped': True,
                        'external_state_checked': True, 'fix_attempts_used': count}})

    def test_original_approved_task_stays_capped_until_explicit_exception(self):
        body = self.prepared()
        before = self.store.detail(body['task'])
        self.assertIsNone(self.store.claim('oliver-mini', body['task'])['task'])
        grant = self.store.apply_attempt_exception('oliver-mini', body)
        self.assertTrue(grant['task']['ready'])
        self.assertEqual(11, grant['task']['fix_attempt_limit'])
        for key in ('generation', 'receipts', 'fix_attempts_used', 'total_fix_attempts', 'reassignment_count', 'result'):
            self.assertEqual(before[key], grant['task'][key] if key != 'receipts' else self.store.detail(body['task'])[key])
        claim = self.store.claim('oliver-mini', body['task'])
        self.assertEqual(11, claim['task']['fix_attempts_used'])
        self.assertEqual(11, claim['task']['total_fix_attempts'])
        self.assertEqual(2, claim['task']['attempt_exception']['consumed_generation'])
        self.finish(claim, 'completed', 11)
        self.assertEqual('completed', self.store.detail(body['task'])['status'])

    def test_exception_is_consumed_atomically_and_never_replayed_after_failure(self):
        body = self.prepared()
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            grants = list(pool.map(lambda _: self.store.apply_attempt_exception('oliver-mini', body), range(8)))
        self.assertEqual(1, sum(not g['replayed'] for g in grants))
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            claims = list(pool.map(lambda _: self.store.claim('oliver-mini', body['task']), range(8)))
        successes = [c for c in claims if c['task']]
        self.assertEqual(1, len(successes))
        self.finish(successes[0], 'blocked', 11)
        self.store = Store(self.path, clock=lambda: 3000)
        self.store.pulse('oliver-mini', {'capacity': {'cpus': 12}})
        replay = self.store.apply_attempt_exception('oliver-mini', body)
        self.assertTrue(replay['replayed'])
        self.assertIsNone(self.store.claim('oliver-mini', body['task'])['task'])
        self.assertEqual(11, self.store.detail(body['task'])['total_fix_attempts'])
        changed = {**body, 'scope_evidence': 'Different evidence cannot renew the old permission'}
        with self.assertRaises(Conflict): self.store.apply_attempt_exception('oliver-mini', changed)

    def test_missing_unverified_foreign_stale_or_larger_grant_is_rejected(self):
        body = self.prepared()
        for change in ({'exception_verified': False}, {'execution_stopped': False},
                       {'external_state_checked': False}, {'attempt_number': 12}, {'expected_used': 9},
                       {'expected_used': True}, {'generation': 2}, {'worker': 'oliver-mac'},
                       {'source': {**body['source'], 'message_id': 'unrelated'}}, {'approval_id': 'a' * 64}):
            with self.subTest(change=change), self.assertRaises((Conflict, Invalid)):
                self.store.apply_attempt_exception('oliver-mini', {**body, **change})
        for actor in ('dashboard', 'oliver-mac'):
            with self.assertRaises(Conflict): self.store.apply_attempt_exception(actor, body)
        self.assertEqual({}, self.store.detail(body['task'])['attempt_exception'])

    def test_actual_count_generation_host_or_human_stop_change_rejects_grant(self):
        for index, (field, value) in enumerate((('fix_attempts_used', 9), ('generation', 2),
                                               ('attempts_host', 'oliver-mac'), ('status', 'running'))):
            body = self.prepared(str(index))
            with self.store.transaction() as db:
                db.execute('UPDATE tasks SET ' + field + '=? WHERE id=?', (value, body['task']))
            with self.assertRaises(Conflict): self.store.apply_attempt_exception('oliver-mini', body)
            with self.store.transaction() as db:
                db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (body['task'],))
        body = self.prepared('stop')
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET result=? WHERE id=?', (json.dumps({'execution_stopped': True, 'human_stop': True}), body['task']))
        with self.assertRaises(Conflict): self.store.apply_attempt_exception('oliver-mini', body)

    def test_existing_owner_dependencies_and_default_cap_are_preserved(self):
        body = self.prepared()
        other = self.prepared('unrelated')
        active = self.store.submit('oliver-mini', {'request_key': 'active', 'title': 'Active prerequisite'})
        claim = self.store.claim('oliver-mini', active['id'])
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET dependencies=? WHERE id=?', (json.dumps([active['id']]), body['task']))
        grant = self.store.apply_attempt_exception('oliver-mini', body)
        self.assertFalse(grant['task']['ready'])
        self.assertIsNone(self.store.claim('oliver-mini', body['task'])['task'])
        self.store.heartbeat('oliver-mini', active['id'], {'claim_token': claim['claim_token'], 'generation': 1})
        self.assertEqual(10, self.store.detail(other['task'])['fix_attempt_limit'])
        self.assertIsNone(self.store.claim('oliver-mini', other['task'])['task'])
        self.finish(claim, 'completed', 1)
        self.assertIsNotNone(self.store.claim('oliver-mini', body['task'])['task'])

    def test_host_assignment_change_or_generation_advance_invalidates_unconsumed_grant(self):
        for field in ('generation', 'attempt_base', 'reassignment_count'):
            body = self.prepared(field)
            self.store.apply_attempt_exception('oliver-mini', body)
            with self.store.transaction() as db:
                db.execute('UPDATE tasks SET ' + field + '=' + field + '+1 WHERE id=?', (body['task'],))
            self.assertIsNone(self.store.claim('oliver-mini', body['task'])['task'])

    def test_later_human_approval_wait_cannot_reactivate_old_unconsumed_permission(self):
        body = self.prepared()
        self.store.apply_attempt_exception('oliver-mini', body)
        # An administrator can restore an external wait after a changed fact.
        # Simulate the new wait before applying its independent actual answer.
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='pending_external_approval' WHERE id=?", (body['task'],))
        newer = self.store.apply_approval('admin', {
            'source': {**body['source'], 'message_id': 'later-answer'},
            'decision': 'approved', 'decision_verified': True,
            'response': 'Allow account read only', 'scope': 'Account read; no extra attempt grant',
            'tasks': [{'id': body['task'], 'generation': 1, 'execution_stopped': True,
                       'external_state_checked': True, 'scope_evidence': 'Only the newer account decision'}]})
        self.assertTrue(newer['recorded'])
        self.assertIsNotNone(self.store.detail(body['task'])['attempt_exception']['invalidated_at'])
        self.assertIsNone(self.store.claim('oliver-mini', body['task'])['task'])
        self.assertTrue(self.store.apply_attempt_exception('oliver-mini', body)['replayed'])
        self.assertIsNone(self.store.claim('oliver-mini', body['task'])['task'])

