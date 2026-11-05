import concurrent.futures
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import test_attempt_allowances as fixtures
from coordinator.attempt_accounting import BINDING
from coordinator.store import Store, Conflict, Invalid, encoded


VM1, VM2 = fixtures.VM1, fixtures.VM2


def amendment_request(store, ident, name='second-human'):
    body = fixtures.allowance_request(store, ident, name)
    row = store.detail(ident)
    body.update(previous_grant_id=row['attempt_allowance']['id'], unsettled_operations=[],
                settlement_receipt={'path': '/private/verified/owning-quiet.json', 'sha256': 'a' * 64},
                response='10 more attempts for VM1')
    body['expected']['fix_attempt_limit'] = row['fix_attempt_limit']
    return body


class AllowanceAmendmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name) / 'db'
        self.now = 1000.0; self.store = Store(self.path, clock=lambda: self.now, heavy_worker=VM1)
        for worker in (VM1, VM2): self.store.pulse(worker, {'capacity': {'cpus': 12}})

    def tearDown(self):
        self.temp.cleanup()

    def running(self):
        ident = fixtures.prepared_task(self.store)
        first = self.store.apply_attempt_allowance('admin', fixtures.allowance_request(self.store, ident))
        claim = self.store.claim(VM1, ident); fixtures.finish(self.store, claim, used=24)
        self.retry(claim)
        return self.store.claim(VM1, ident), first

    def retry(self, claim):
        self.store.retry(VM1, claim['task']['id'], {**fixtures.claim_identity(claim),
            'execution_stopped': True, 'external_state_checked': True, 'evidence': 'Actual failure corrected'})

    def raw(self, ident):
        with self.store.connection() as db:
            return dict(db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone())

    def test_running_amendment_preserves_claim_counters_history_and_original_grant(self):
        claim, first = self.running(); ident = claim['task']['id']; before = self.store.detail(ident); raw = self.raw(ident)
        body = amendment_request(self.store, ident); result = self.store.amend_attempt_allowance('admin', body)
        after = self.store.detail(ident); raw_after = self.raw(ident); grant = after['attempt_allowance']
        self.assertEqual((24, 24, 0, 40, 16), tuple(after[k] for k in
            ('fix_attempts_used', 'total_fix_attempts', 'attempt_base', 'fix_attempt_limit', 'fix_attempts_remaining')))
        self.assertEqual(['attempt_allowance'], [k for k in raw if raw[k] != raw_after[k]])
        self.assertEqual(before['receipts'], after['receipts']); self.assertEqual(before['events'], after['events'][:-1])
        self.assertEqual(before['attempt_allowances'], after['attempt_allowances'])
        self.assertEqual(first['grant'], after['attempt_allowances'][0])
        self.assertEqual(before['attempt_allowance']['id'], grant['previous_grant_id'])
        self.assertEqual(grant['root_grant_id'], grant['previous_grant_id'])
        self.assertEqual(hashlib.sha256(encoded(body).encode()).hexdigest(), grant['id'])
        self.assertNotEqual(before['attempt_allowance']['id'], grant['id'])  # Old candidate identity is fenced.
        self.assertEqual(result['grant'], after['attempt_allowance_amendments'][0])
        self.assertNotIn(claim['claim_token'], json.dumps(result))
        self.store.heartbeat(VM1, ident, fixtures.claim_identity(claim))
        other = self.store.submit('admin', {'request_key': 'other', 'title': 'Other'})
        self.assertEqual(20, self.store.detail(other['id'])['fix_attempt_limit'])

    def test_distinct_authorizations_form_a_chain_and_replay_never_replenishes(self):
        claim, original = self.running(); ident = claim['task']['id']; first_body = amendment_request(self.store, ident)
        first = self.store.amend_attempt_allowance('admin', first_body)
        second_body = amendment_request(self.store, ident, 'third-human')
        second = self.store.amend_attempt_allowance('admin', second_body)
        self.assertEqual(50, second['task']['fix_attempt_limit'])
        self.assertEqual(first['grant']['id'], second['task']['attempt_allowance']['previous_grant_id'])
        self.assertEqual(original['grant']['id'], second['task']['attempt_allowance']['root_grant_id'])
        before = self.store.detail(ident)
        self.assertTrue(self.store.amend_attempt_allowance('admin', first_body)['replayed'])
        self.assertTrue(self.store.apply_attempt_allowance('admin', original['grant']['request'])['replayed'])
        self.assertEqual(before, self.store.detail(ident))
        reused = amendment_request(self.store, ident, 'new-request-same-source'); reused['source'] = first_body['source']
        with self.assertRaises(Conflict): self.store.amend_attempt_allowance('admin', reused)
        self.assertEqual(before, self.store.detail(ident)); self.assertEqual(2, len(before['attempt_allowance_amendments']))

    def test_concurrent_duplicate_restart_and_competing_parents_are_fenced(self):
        claim, _ = self.running(); ident = claim['task']['id']; body = amendment_request(self.store, ident)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.amend_attempt_allowance('admin', body), range(8)))
        self.assertEqual(1, sum(not r['replayed'] for r in results))
        self.store = Store(self.path, clock=lambda: self.now, heavy_worker=VM1)
        self.assertTrue(self.store.amend_attempt_allowance('admin', body)['replayed'])
        before = self.store.detail(ident)
        stale_parent = copy.deepcopy(body); stale_parent['source']['message_id'] = 'distinct-but-stale'; stale_parent['request_id'] += '-new'
        with self.assertRaises(Conflict): self.store.amend_attempt_allowance('admin', stale_parent)
        with self.assertRaises(Conflict): self.store.amend_attempt_allowance('admin', {**body, 'response': 'Changed duplicate'})
        self.assertEqual(before, self.store.detail(ident))
        next_requests = [amendment_request(self.store, ident, name) for name in ('human-race-a', 'human-race-b')]
        def apply(request):
            try: return self.store.amend_attempt_allowance('admin', request)
            except Conflict: return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            competing = list(pool.map(apply, next_requests))
        self.assertEqual(1, sum(r is not None for r in competing))
        self.assertEqual(50, self.store.detail(ident)['fix_attempt_limit'])
        self.assertEqual(2, len(self.store.detail(ident)['attempt_allowance_amendments']))

    def test_rejects_foreign_stale_unverified_and_unsettled_requests_atomically(self):
        claim, original = self.running(); ident = claim['task']['id']; body = amendment_request(self.store, ident)
        before = self.store.detail(ident)
        for actor in (VM1, VM2, 'dashboard'):
            with self.assertRaises(Conflict): self.store.amend_attempt_allowance(actor, body)
        variants = [{**body, k: v} for k, v in (
            ('generation', 99), ('generation', True), ('worker', VM2), ('previous_grant_id', 'b' * 64),
            ('additional_attempts', 11), ('additional_attempts', True), ('additional_attempts', 0),
            ('decision_verified', False), ('execution_stopped', False), ('external_state_checked', False),
            ('unsettled_operations', ['active-native-input']), ('unsettled_operations', None),
            ('settlement_receipt', {'path': 'relative', 'sha256': 'a' * 64}),
            ('source', original['grant']['request']['source']))]
        for key, value in (('fix_attempt_limit', 29), ('fix_attempts_used', 23), ('step', 1),
                           ('reassignment_count', 1), ('attempt_allowance_sha256', 'a' * 64),
                           ('attempt_exception_sha256', 'a' * 64), ('owner', VM2)):
            b = copy.deepcopy(body); b['expected'][key] = value; variants.append(b)
        for b in variants:
            with self.subTest(body=b), self.assertRaises((Conflict, Invalid)):
                self.store.amend_attempt_allowance('admin', b)
            self.assertEqual(before, self.store.detail(ident))

    def test_sources_cannot_cross_original_and_amendment_endpoints(self):
        other = fixtures.prepared_task(self.store, 'other')
        claim, original = self.running(); ident = claim['task']['id']; body = amendment_request(self.store, ident)
        self.store.amend_attempt_allowance('admin', body)
        foreign = fixtures.allowance_request(self.store, other, 'foreign'); foreign['source'] = body['source']
        with self.assertRaises(Conflict): self.store.apply_attempt_allowance('admin', foreign)
        self.assertEqual({}, self.store.detail(other)['attempt_allowance'])
        stale_initial_source = amendment_request(self.store, ident, 'reuse-initial')
        stale_initial_source['source'] = original['grant']['request']['source']
        with self.assertRaises(Conflict): self.store.amend_attempt_allowance('admin', stale_initial_source)

    def test_same_host_approval_cycles_preserve_latest_authority_and_forty_cap(self):
        claim, _ = self.running(); ident = claim['task']['id']
        amended = self.store.amend_attempt_allowance('admin', amendment_request(self.store, ident))
        fixtures.finish(self.store, claim, 'pending_external_approval', 24,
                        approval_request={'thread_id': 'attendance', 'question_id': 'new-watch'})
        self.assertIsNone(self.store.claim(VM1, ident)['task'])
        self.store.apply_approval('admin', {'source': {'thread_id': 'attendance', 'question_id': 'new-watch', 'message_id': 'actual-ready'},
            'decision': 'approved', 'decision_verified': True, 'response': 'Ready now', 'scope': 'Attendance only',
            'tasks': [{'id': ident, 'generation': claim['task']['generation'], 'execution_stopped': True,
                       'external_state_checked': True, 'scope_evidence': 'Actual separate attendance response'}]})
        next_claim = self.store.claim(VM1, ident)
        self.assertEqual(amended['grant']['id'], next_claim['task']['attempt_allowance']['id'])
        with self.assertRaises(Invalid): fixtures.finish(self.store, next_claim, used=41)
        fixtures.finish(self.store, next_claim, used=39); self.retry(next_claim)
        last = self.store.claim(VM1, ident)
        self.assertEqual(40, fixtures.finish(self.store, last)['fix_attempts_used']); self.retry(last)
        self.assertIsNone(self.store.claim(VM1, ident)['task'])

    def test_expiry_and_replay_after_host_change_do_not_restore_chain(self):
        claim, _ = self.running(); ident = claim['task']['id']; body = amendment_request(self.store, ident)
        self.store.amend_attempt_allowance('admin', body); fixtures.wait(self.store, claim, VM2)
        new = self.store.claim(VM2, ident)
        self.assertEqual(20, new['task']['fix_attempt_limit']); self.assertFalse(new['task']['attempt_allowance_active'])
        self.assertIsNotNone(new['task']['attempt_allowance']['expired_at'])
        fixtures.wait(self.store, new, VM1); returned = self.store.claim(VM1, ident)
        self.assertEqual(20, returned['task']['fix_attempt_limit'])
        before = self.store.detail(ident); self.assertTrue(self.store.amend_attempt_allowance('admin', body)['replayed'])
        self.assertEqual(before, self.store.detail(ident))
        with self.assertRaises((Conflict, Invalid)):
            self.store.amend_attempt_allowance('admin', amendment_request(self.store, ident, 'new-after-expiry'))

    def test_queued_amendment_preserves_state_and_requires_existing_authority(self):
        ident = fixtures.prepared_task(self.store)
        no_grant = fixtures.allowance_request(self.store, ident, 'no-grant')
        no_grant.update(previous_grant_id='a' * 64, unsettled_operations=[],
                        settlement_receipt={'path': '/private/quiet.json', 'sha256': 'a' * 64})
        no_grant['expected']['fix_attempt_limit'] = 30
        with self.assertRaises(Conflict): self.store.amend_attempt_allowance('admin', no_grant)
        self.store.apply_attempt_allowance('admin', fixtures.allowance_request(self.store, ident))
        before = self.raw(ident); result = self.store.amend_attempt_allowance('admin', amendment_request(self.store, ident))
        self.assertEqual(('queued', None, 40, 20), tuple(result['task'][k] for k in
            ('status', 'owner', 'fix_attempt_limit', 'fix_attempts_remaining')))
        self.assertEqual(['attempt_allowance'], [k for k in before if before[k] != self.raw(ident)[k]])

    def test_migration_preserves_original_grant_and_running_claim(self):
        claim, original = self.running(); ident = claim['task']['id']; before = self.raw(ident)
        with self.store.connection() as db: db.execute('DROP TABLE attempt_allowance_amendments')
        self.store = Store(self.path, clock=lambda: self.now, heavy_worker=VM1)
        self.assertEqual(before, self.raw(ident)); self.assertEqual([], self.store.detail(ident)['attempt_allowance_amendments'])
        self.assertEqual(original['grant'], self.store.detail(ident)['attempt_allowances'][0])
        self.store.heartbeat(VM1, ident, fixtures.claim_identity(claim))
        self.assertEqual(40, self.store.amend_attempt_allowance('admin', amendment_request(self.store, ident))['task']['fix_attempt_limit'])

    def assert_settled_amendment_preserves_outcome(self, outcome):
        claim, _ = self.running(); ident = claim['task']['id']
        fixtures.finish(self.store, claim, outcome, 25, acceptance_complete=False,
                        operator_observation_pending=True)
        before = self.store.detail(ident); raw = self.raw(ident)
        result = self.store.amend_attempt_allowance('admin', amendment_request(self.store, ident))
        after = self.store.detail(ident)
        self.assertEqual(['attempt_allowance'], [k for k in raw if raw[k] != self.raw(ident)[k]])
        self.assertEqual((outcome, VM1, claim['task']['generation'], 25, 25, 40, 15),
                         tuple(after[k] for k in ('status', 'owner', 'generation', 'fix_attempts_used',
                                                  'total_fix_attempts', 'fix_attempt_limit', 'fix_attempts_remaining')))
        self.assertEqual(before['result'], after['result']); self.assertEqual(before['approval'], after['approval'])
        self.assertEqual(before['receipts'], after['receipts']); self.assertEqual(before['events'], after['events'][:-1])
        self.assertEqual(before['ready'], after['ready']); self.assertEqual(before['waiting_reason'], after['waiting_reason'])
        self.assertFalse(after['result']['acceptance_complete'])
        self.assertTrue(after['result']['operator_observation_pending'])
        self.assertFalse(result['replayed']); self.assertIsNone(self.store.claim(VM1, ident)['task'])
        with self.assertRaises(Conflict): self.store.heartbeat(VM1, ident, fixtures.claim_identity(claim))

    def test_settled_failed_amendment_preserves_failure_and_does_not_resume(self):
        self.assert_settled_amendment_preserves_outcome('failed')

    def test_settled_blocked_amendment_preserves_cooldown_and_does_not_queue(self):
        self.assert_settled_amendment_preserves_outcome('blocked')

    def test_current_and_historical_accounting_honor_amended_limit_only_on_its_assignment(self):
        claim, _ = self.running(); ident = claim['task']['id']
        self.store.amend_attempt_allowance('admin', amendment_request(self.store, ident))
        body = fixtures.AttemptAllowanceTests.accounting_body(self, claim, 35)
        self.store.correct_attempts('admin', ident, body)
        self.assertEqual((35, 40, 5), tuple(self.store.detail(ident)[k] for k in
            ('fix_attempts_used', 'fix_attempt_limit', 'fix_attempts_remaining')))
        # A later truthful closed-assignment omission must use forty, not the
        # original thirty and never the current different host's default twenty.
        fixtures.wait(self.store, claim, VM2); current = self.store.claim(VM2, ident)
        body = fixtures.AttemptAllowanceTests.accounting_body(self, current, 0)
        body['corrected'].update(attempt_base=38, total_fix_attempts=38)
        body['historical_proof'] = [{'task': ident, 'worker': VM1, 'generation': claim['task']['generation'], 'step': 0,
            'recorded_attempts': 35, 'fix_attempts_used': 38, 'omitted_attempts': 3, 'verified': True,
            'receipt': body['owner_proof']['receipt'], 'source': body['owner_proof']['source'], 'evidence': 'Verified three omitted under amended limit'}]
        forged = copy.deepcopy(body); forged['historical_proof'][0].update(fix_attempts_used=41, omitted_attempts=6)
        forged['corrected'].update(attempt_base=41, total_fix_attempts=41)
        with self.assertRaises(Invalid): self.store.correct_attempts('admin', ident, forged)
        self.store.correct_attempts('admin', ident, body)
        self.assertEqual((0, 38, 38, 20), tuple(self.store.detail(ident)[k] for k in
            ('fix_attempts_used', 'total_fix_attempts', 'attempt_base', 'fix_attempt_limit')))


if __name__ == '__main__': unittest.main()
