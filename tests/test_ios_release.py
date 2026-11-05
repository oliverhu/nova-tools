"""Source-pinned upload claims preserve Mac publication and real ownership."""
import concurrent.futures
import copy
import json
from pathlib import Path
import tempfile
import unittest

from coordinator.ios_release import start, SCOPE, TARGET, NOVA, STUDIO
from coordinator.store import Store, Conflict, Invalid
from coordinator.workers import STUDIO_WORKERS


class IOSReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.now = 1000.0
        self.store = Store(Path(self.temp.name) / 'db', clock=lambda: self.now)
        self.store.configure_repository({'repository': NOVA, 'published_commit': 'a' * 40})
        self.store.observe_repository(STUDIO, {'repository': NOVA, 'commit': 'b' * 40, 'observed_at': self.now})
        for worker in STUDIO_WORKERS:
            self.store.pulse(worker, {'capacity': {'cpus': 12}})
        self.dep = self.store.submit('admin', {'request_key': 'voice', 'title': 'Voice'})['id']
        self.body = {'scope': SCOPE, 'repository': NOVA, 'commit': 'b' * 40,
                     'dependencies': [self.dep], 'authorization': {'thread_id': 'primary', 'message_id': 'actual-upload'}}

    def tearDown(self):
        self.temp.cleanup()

    def repo(self):
        return self.store.snapshot()['repositories']

    def complete_dependency(self):
        claim = self.store.claim(STUDIO, self.dep)
        self.store.finish(STUDIO, self.dep, {**self.claim_body(claim), 'outcome': 'completed',
            'receipt': {'summary': 'Revised human acceptance completed', 'execution_stopped': True}})

    @staticmethod
    def claim_body(claim):
        return {'claim_token': claim['claim_token'], 'generation': claim['task']['generation'], 'step': 0}

    def receipt(self):
        return {'summary': 'Apple accepted upload; physical audio remains unverified', 'execution_stopped': True,
                'external_state_checked': True, 'publication_ref': 'verified-upload-journal',
                'ios_upload': {'status': 'uploaded', 'source_commit': 'b' * 40, 'version': '0.2.2',
                    'build_number': '52', 'upload_ref': 'verified-upload-journal',
                    'testflight_distributed': False, 'submitted_for_review': False,
                    'testflight_settings_changed': False, 'ios_public_release': False}}

    def test_dependency_self_claim_upload_and_replay_never_change_mac_state(self):
        before = self.repo(); ident = start(self.store, self.body)['task_ids'][0]
        self.assertEqual(before, self.repo())
        self.assertIsNone(self.store.claim(STUDIO, ident)['task'])
        self.complete_dependency()
        for worker in STUDIO_WORKERS[1:]:
            self.assertIsNone(self.store.claim(worker, ident)['task'])
        claim = self.store.claim(STUDIO, ident)
        row = claim['task']; self.assertEqual([TARGET], row['delivery_targets'])
        self.assertEqual({'worker': STUDIO}, row['requirements'])
        self.assertIn('No Mac build', row['current_step']['instructions'])
        args = {**self.claim_body(claim), 'outcome': 'completed', 'receipt': self.receipt()}
        result = self.store.finish(STUDIO, ident, args)
        self.assertEqual(('completed', 1), (result['status'], result['step']))
        self.assertEqual([TARGET], result['delivery_targets'])
        self.assertEqual(SCOPE, result['result']['release_scope'])
        self.assertEqual(before, self.repo())
        self.assertEqual(result, self.store.finish(STUDIO, ident, args))
        self.now += 601
        self.store = Store(self.store.path, clock=lambda: self.now)
        self.assertEqual({'created_ids': [], 'task_ids': [ident], 'replayed': True}, start(self.store, self.body))
        self.assertEqual(before, self.repo())

    def test_rejected_upload_evidence_preserves_running_claim_and_baseline(self):
        self.complete_dependency(); ident = start(self.store, self.body)['task_ids'][0]
        claim = self.store.claim(STUDIO, ident); baseline = self.repo()
        bad = []
        for key in ['status', 'source_commit', 'version', 'build_number', 'upload_ref',
                    'testflight_distributed', 'submitted_for_review', 'testflight_settings_changed', 'ios_public_release']:
            receipt = self.receipt(); receipt['ios_upload'].pop(key); bad.append(receipt)
        for change in [{'external_state_checked': False}, {'published_commit': 'b' * 40},
                       {'release_commit': 'c' * 40}, {'acceptance_complete': False}, {'publication_ref': ''}]:
            bad.append({**self.receipt(), **change})
        receipt = self.receipt(); receipt['ios_upload']['source_commit'] = 'c' * 40; bad.append(receipt)
        for receipt in bad:
            with self.assertRaises(Invalid):
                self.store.finish(STUDIO, ident, {**self.claim_body(claim), 'outcome': 'completed', 'receipt': receipt})
            self.assertEqual('running', self.store.detail(ident)['status']); self.assertEqual(baseline, self.repo())
        self.store.heartbeat(STUDIO, ident, self.claim_body(claim))

    def test_concurrent_creation_is_deduplicated_and_request_is_immutable(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: start(self.store, self.body), range(4)))
        self.assertEqual(1, sum(bool(x['created_ids']) for x in results))
        self.assertEqual(1, len({x['task_ids'][0] for x in results}))
        for change in [{'authorization': {'thread_id': 'other', 'message_id': 'different'}}, {'dependencies': []}]:
            with self.assertRaises((Conflict, Invalid)):
                start(self.store, {**self.body, **change})

    def test_invalid_or_stale_target_never_mutates_existing_release_state(self):
        before = self.repo()
        for change in [{'repository': 'other/repo'}, {'scope': 'combined'}, {'commit': 'c' * 40},
                       {'commit': 'main'}, {'authorization': {}}, {'dependencies': ['missing']},
                       {'dependencies': [self.dep, self.dep]}]:
            with self.assertRaises((Conflict, Invalid)):
                start(self.store, {**self.body, **change})
        self.now += 601
        with self.assertRaises(Conflict): start(self.store, self.body)
        self.assertEqual(before, self.repo())

    def test_combined_task_and_hourly_schedule_remain_independent(self):
        combined = self.store.tick_releases(NOVA)[0]; old = self.store.detail(combined)
        baseline = self.repo(); ios = start(self.store, self.body)['task_ids'][0]
        self.assertNotEqual(combined, ios); self.assertEqual(old, self.store.detail(combined))
        self.assertEqual(baseline, self.repo()); self.assertEqual([], self.store.tick_releases(NOVA))

    def test_existing_exact_target_human_hold_is_not_bypassed(self):
        combined = self.store.tick_releases(NOVA)[0]
        claim = self.store.claim(STUDIO, combined)
        with self.assertRaises(Conflict): start(self.store, self.body)
        self.store.finish(STUDIO, combined, {**self.claim_body(claim), 'outcome': 'pending_external_approval',
            'receipt': {'summary': 'Actual human stop', 'execution_stopped': True,
                        'approval_request': {'thread_id': 'primary', 'question_id': 'real-stop'}}})
        before = self.store.detail(combined)
        with self.assertRaises(Conflict): start(self.store, self.body)
        self.assertEqual(before, self.store.detail(combined))

    def test_blocked_scope_is_server_owned_and_preserved_through_reclaim(self):
        self.complete_dependency(); ident = start(self.store, self.body)['task_ids'][0]
        claim = self.store.claim(STUDIO, ident)
        receipt = {'summary': 'Concrete Apple rejection retained', 'execution_stopped': True,
                   'release_scope': 'combined', 'release_authorization': {'message_id': 'forged'}}
        result = self.store.finish(STUDIO, ident, {**self.claim_body(claim), 'outcome': 'blocked', 'receipt': receipt})
        self.assertEqual(SCOPE, result['result']['release_scope'])
        self.assertEqual(self.body['authorization'], result['result']['release_authorization'])
        self.now += 901
        self.store.pulse(STUDIO, {'capacity': {'cpus': 12}})
        claim = self.store.claim(STUDIO, ident)
        self.assertEqual([TARGET], claim['task']['delivery_targets'])
        self.assertEqual(1, claim['task']['fix_attempts_used'])

    def test_combined_receipt_cannot_inject_upload_only_scope(self):
        ident = self.store.tick_releases(NOVA)[0]; claim = self.store.claim(STUDIO, ident)
        self.store.finish(STUDIO, ident, {**self.claim_body(claim), 'outcome': 'blocked', 'receipt': {
            'summary': 'Failure retains combined scope', 'execution_stopped': True, 'release_scope': SCOPE}})
        self.assertNotIn('release_scope', self.store.detail(ident)['result'])


if __name__ == '__main__':
    unittest.main()
