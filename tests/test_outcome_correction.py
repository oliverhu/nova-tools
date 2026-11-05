import concurrent.futures
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coordinator.store import Conflict, Invalid, Store, encoded


def checksum(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class OutcomeCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = Store(Path(self.temp.name) / 'db', clock=lambda: self.now)
        for worker in ('oliver-mac', 'oliver-mini'):
            self.store.pulse(worker, {'capacity': {'cpus': 12, 'memory_gb': 24, 'disk_gb': 500}})

    def tearDown(self):
        self.temp.cleanup()

    def task(self, key='parent', **changes):
        return self.store.submit('oliver-mac', {'request_key': key, 'title': key, **changes})

    @staticmethod
    def identity(claim):
        return {'generation': claim['task']['generation'], 'step': claim['task']['step'],
                'claim_token': claim['claim_token']}

    def legacy_completion(self, key='parent', task_fields=None, **changes):
        task = self.task(key, **(task_fields or {}))
        claim = self.store.claim('oliver-mac', task['id'])
        receipt = {'summary': 'Original acceptance incomplete', 'execution_stopped': True,
                   'external_state_checked': True, 'outcome': 'blocked', 'acceptance_complete': False,
                   'fix_attempts_used': 17, 'remaining_acceptance': ['Original catalog'],
                   'unknown_backend_outcome': True, 'no_release': True, **changes}
        # Produce an authentic pre-fix finish transition using the old resolver
        # behavior, without manufacturing state with raw SQL.
        with patch('coordinator.store.finish_outcome', return_value='completed'):
            self.store.finish('oliver-mac', task['id'], {
                **self.identity(claim), 'outcome': 'completed', 'receipt': receipt})
        detail = self.store.detail(task['id'])
        request = {'generation': detail['generation'], 'expected_status': 'completed',
                   'expected_step': detail['step'], 'expected_result_sha256': checksum(detail['result']),
                   'expected_receipt_sha256': checksum(detail['receipts'][-1]['receipt']),
                   'outcome': 'blocked', 'execution_stopped': True, 'external_state_checked': True,
                   'evidence': 'Original immutable receipt explicitly says blocked and acceptance false; CLI default was wrong.'}
        return claim, detail, request

    def test_finish_consistency_old_client_rejected_and_implicit_receipt_honored(self):
        for index, (requested, declared, acceptance, valid, expected) in enumerate((
            ('completed', 'blocked', False, False, None),
            ('completed', None, False, False, None),
            ('blocked', 'failed', False, False, None),
            (None, 'blocked', False, True, 'blocked'),
            (None, 'pending_external_approval', False, True, 'pending_external_approval'),
            ('failed', None, False, True, 'failed'),
            (None, None, None, True, 'completed'),
            ('completed', 'completed', True, True, 'completed'),
        )):
            with self.subTest(index=index):
                task = self.task(str(index)); claim = self.store.claim('oliver-mac', task['id'])
                receipt = {'summary': 'Actual result', 'execution_stopped': True}
                if declared is not None: receipt['outcome'] = declared
                if acceptance is not None: receipt['acceptance_complete'] = acceptance
                body = {**self.identity(claim), 'receipt': receipt}
                if requested is not None: body['outcome'] = requested
                if valid:
                    self.assertEqual(expected, self.store.finish('oliver-mac', task['id'], body)['status'])
                else:
                    before = self.store.detail(task['id'])
                    with self.assertRaises(Invalid): self.store.finish('oliver-mac', task['id'], body)
                    self.assertEqual(before, self.store.detail(task['id']))
                    self.store.defer('oliver-mac', task['id'], {
                        **self.identity(claim), 'execution_stopped': True, 'external_state_checked': True,
                        'worker': 'oliver-mini', 'evidence': 'Fixture stopped after rejected contradictory finish'})

    def test_correction_preserves_original_receipts_history_counts_and_dependents(self):
        prerequisite = self.task('original prerequisite')
        original = self.store.claim('oliver-mini', prerequisite['id'])
        self.store.finish('oliver-mini', prerequisite['id'], {
            **self.identity(original), 'receipt': {'summary': 'Prerequisite complete', 'execution_stopped': True}})
        claim, before, request = self.legacy_completion(evidence='z' * 18000,
            task_fields={'dependencies': [prerequisite['id']], 'requirements': {'worker': 'oliver-mac'}})
        parent = before['id']
        dependent = self.task('dependent', dependencies=[parent])
        self.assertTrue(self.store.detail(dependent['id'])['ready'])
        corrected = self.store.correct_outcome('admin', parent, request)
        after = self.store.detail(parent)
        self.assertEqual(('blocked', 0, 1), (after['status'], after['step'], after['generation']))
        self.assertIsNone(after['owner']); self.assertEqual('oliver-mac', after['last_owner'])
        for key in ('result', 'receipts', 'requirements', 'dependencies', 'fix_attempts_used',
                    'total_fix_attempts', 'reassignment_count', 'attempt_base', 'attempts_host', 'attempt_exception'):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual((17, 17, 0), tuple(after[k] for k in
                         ('fix_attempts_used', 'total_fix_attempts', 'reassignment_count')))
        self.assertEqual(before['events'], after['events'][:-1])
        self.assertEqual('outcome-corrected', after['events'][-1]['kind'])
        audit = corrected['correction']
        self.assertEqual(before['result'], audit['prior_state']['result'])
        self.assertEqual(before['receipts'][-1]['receipt'], audit['prior_receipt'])
        self.assertEqual(18000, len(audit['prior_state']['result']['evidence']))
        self.assertEqual(audit, after['outcome_corrections'][0])
        self.assertNotIn('claim_hash', audit['prior_state'])
        self.assertFalse(self.store.detail(dependent['id'])['ready'])
        self.assertIsNone(self.store.claim('oliver-mini', dependent['id'])['task'])
        with self.assertRaises(Conflict): self.store.heartbeat('oliver-mac', parent, self.identity(claim))
        with self.assertRaises(Conflict): self.store.finish('oliver-mac', parent, {
            **self.identity(claim), 'outcome': 'blocked', 'receipt': before['result']})

    def test_exact_admin_guards_are_atomic_and_cannot_edit_counts_or_dependencies(self):
        _, before, body = self.legacy_completion()
        for change in ({'generation': 2}, {'expected_step': 2}, {'expected_status': 'running'},
                       {'outcome': 'queued'}, {'expected_result_sha256': 'a' * 64},
                       {'expected_receipt_sha256': 'b' * 64}, {'execution_stopped': False},
                       {'external_state_checked': False}, {'fix_attempts_used': 0},
                       {'dependencies': []}, {'generation': True}):
            with self.subTest(change=change), self.assertRaises((Conflict, Invalid)):
                self.store.correct_outcome('admin', before['id'], {**body, **change})
            self.assertEqual(before, self.store.detail(before['id']))
        with self.assertRaises(Conflict): self.store.correct_outcome('oliver-mac', before['id'], body)
        self.assertEqual(before, self.store.detail(before['id']))
        running = self.task('running'); self.store.claim('oliver-mini', running['id'])
        with self.assertRaises(Conflict): self.store.correct_outcome('admin', running['id'], body)
        self.assertEqual('running', self.store.detail(running['id'])['status'])

    def test_contradiction_and_side_effect_boundaries(self):
        cases = ({'acceptance_complete': True}, {'outcome': 'failed'},
                 {'external_state_checked': False}, {'publication_ref': 'some-publication'},
                 {'published_commit': 'a' * 40}, {'ios_upload': {'status': 'uploaded'}})
        for index, changes in enumerate(cases):
            _, before, body = self.legacy_completion(str(index), **changes)
            with self.subTest(changes=changes), self.assertRaises(Conflict):
                self.store.correct_outcome('admin', before['id'], body)
            self.assertEqual(before, self.store.detail(before['id']))

    def test_idempotent_concurrent_correction_survives_restart_and_fences_later_generation(self):
        claim, before, body = self.legacy_completion()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.store.correct_outcome('admin', before['id'], body), range(2)))
        self.assertEqual([False, True], sorted(r['replayed'] for r in results))
        restarted = Store(self.store.path, clock=lambda: self.now)
        self.assertTrue(restarted.correct_outcome('admin', before['id'], body)['replayed'])
        detail = restarted.detail(before['id'])
        self.assertEqual(1, len(detail['outcome_corrections']))
        self.assertEqual(1, sum(e['kind'] == 'outcome-corrected' for e in detail['events']))
        self.assertIsNone(restarted.claim('oliver-mac', before['id'])['task'])
        restarted.defer('admin', before['id'], {'generation': 1, 'execution_stopped': True,
            'external_state_checked': True, 'worker': 'oliver-mac', 'evidence': 'Separate explicit scheduling decision'})
        fresh = restarted.claim('oliver-mac', before['id'])
        self.assertEqual(2, fresh['task']['generation'])
        self.assertEqual(17, fresh['task']['fix_attempts_used'])
        with self.assertRaises(Conflict): restarted.correct_outcome('admin', before['id'], body)
        with self.assertRaises(Conflict): restarted.heartbeat('oliver-mac', before['id'], self.identity(claim))

    def test_active_publish_claim_and_repository_state_are_untouched(self):
        _, before, body = self.legacy_completion()
        self.store.configure_repository({'repository': 'owner/repo', 'published_commit': 'a' * 40})
        self.store.observe_repository('oliver-mini', {'repository': 'owner/repo', 'commit': 'b' * 40, 'observed_at': self.now})
        ident = self.store.tick_releases('owner/repo')[0]
        publish = self.store.claim('oliver-mini', ident)
        original = self.store.detail(ident)
        repositories = self.store.snapshot()['repositories']
        self.store.correct_outcome('admin', before['id'], body)
        self.assertEqual(original, self.store.detail(ident))
        self.assertEqual(repositories, self.store.snapshot()['repositories'])
        self.store.heartbeat('oliver-mini', ident, self.identity(publish))

    def test_completed_publisher_and_engine_are_never_reopened(self):
        self.store.configure_repository({'repository': 'owner/repo', 'published_commit': 'a' * 40})
        self.store.observe_repository('oliver-mini', {'repository': 'owner/repo', 'commit': 'b' * 40, 'observed_at': self.now})
        publish = self.store.tick_releases('owner/repo')[0]
        engine = self.task('engine', task_type='engine_rebuild')['id']
        for ident in (publish, engine):
            claim = self.store.claim('oliver-mini', ident)
            receipt = {'summary': 'Legacy contradictory receipt', 'execution_stopped': True,
                       'external_state_checked': True, 'outcome': 'blocked', 'acceptance_complete': False}
            if ident == publish:
                receipt.update(publication_ref='verified-publication', published_commit='b' * 40)
            with patch('coordinator.store.finish_outcome', return_value='completed'):
                self.store.finish('oliver-mini', ident, {
                    **self.identity(claim), 'outcome': 'completed', 'receipt': receipt})
            before = self.store.detail(ident)
            body = {'generation': before['generation'], 'expected_status': 'completed', 'expected_step': before['step'],
                    'expected_result_sha256': checksum(before['result']),
                    'expected_receipt_sha256': checksum(before['receipts'][-1]['receipt']), 'outcome': 'blocked',
                    'execution_stopped': True, 'external_state_checked': True, 'evidence': 'An unsupported correction'}
            with self.assertRaises(Conflict): self.store.correct_outcome('admin', ident, body)
            self.assertEqual(before, self.store.detail(ident))
        self.assertEqual('b' * 40, self.store.snapshot()['repositories'][0]['published_head'])


if __name__ == '__main__':
    unittest.main()
