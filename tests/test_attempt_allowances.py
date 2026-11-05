import concurrent.futures
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from coordinator.attempt_accounting import BINDING
from coordinator.store import Store, Conflict, Invalid, encoded


VM1 = 'oliverhu-mac-studio-vm1'
VM2 = 'oliverhu-mac-studio-vm2'


def claim_identity(claim):
    return {'claim_token': claim['claim_token'], 'generation': claim['task']['generation'], 'step': claim['task']['step']}


def finish(store, claim, outcome='blocked', used=None, **receipt):
    return store.finish(claim['task']['owner'], claim['task']['id'], {
        **claim_identity(claim), 'outcome': outcome, 'receipt': {
            'summary': 'Actual owned operation settled', 'execution_stopped': True,
            'external_state_checked': True, **({'fix_attempts_used': used} if used is not None else {}), **receipt}})


def wait(store, claim, worker):
    return store.defer(claim['task']['owner'], claim['task']['id'], {
        **claim_identity(claim), 'worker': worker, 'execution_stopped': True,
        'external_state_checked': True, 'evidence': 'Settled handoff; keep all failed-attempt history'})


def prepared_task(store, name='feature', reassignments=0, task_type='work'):
    ident = store.submit('admin', {'request_key': name, 'title': name,
        'task_type': task_type, 'requirements': {'worker': VM1}})['id']
    claim = store.claim(VM1, ident)
    for index in range(reassignments):
        worker = VM2 if index % 2 == 0 else VM1
        wait(store, claim, worker); claim = store.claim(worker, ident)
    finish(store, claim, used=20)
    wait(store, claim, claim['task']['owner'])
    return ident


def allowance_request(store, ident, name='feature'):
    row = store.detail(ident)
    expected = {key: row[key] for key in BINDING}
    for key in ('requirements', 'attempt_allowance', 'attempt_exception'):
        expected[key + '_sha256'] = hashlib.sha256(encoded(row[key]).encode()).hexdigest()
    return {'request_id': name + '-ten-more', 'task': ident, 'generation': row['generation'],
        'worker': row['attempts_host'], 'source': {'thread_id': 'root', 'message_id': name + '-human-ten'},
        'response': 'add 10 more attempts', 'additional_attempts': 10, 'expected': expected,
        'decision_verified': True, 'execution_stopped': True, 'external_state_checked': True,
        'evidence': 'Independently verified primary human response for this task, current host and ten additional attempts'}


class AttemptAllowanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name) / 'db'
        self.now = 1000.0
        self.store = Store(self.path, clock=lambda: self.now, heavy_worker=VM1)
        self.pulse()

    def tearDown(self):
        self.temp.cleanup()

    def pulse(self):
        for worker in (VM1, VM2): self.store.pulse(worker, {'capacity': {'cpus': 12}})

    def raw(self, ident):
        with self.store.connection() as db:
            return dict(db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone())

    def grant(self, ident):
        return self.store.apply_attempt_allowance('admin', allowance_request(self.store, ident))

    def test_grant_preserves_all_counters_claim_metadata_and_history(self):
        ident = prepared_task(self.store); before = self.store.detail(ident); raw = self.raw(ident)
        other = prepared_task(self.store, 'other')
        body = allowance_request(self.store, ident); result = self.store.apply_attempt_allowance('admin', body)
        after = self.store.detail(ident); raw_after = self.raw(ident)
        self.assertEqual((20, 20, 0, 30, 10), tuple(after[k] for k in
            ('fix_attempts_used', 'total_fix_attempts', 'attempt_base', 'fix_attempt_limit', 'fix_attempts_remaining')))
        self.assertTrue(after['attempt_allowance_active']); self.assertTrue(after['ready'])
        self.assertEqual(before['receipts'], after['receipts'])
        self.assertEqual(before['events'], after['events'][:-1])
        self.assertEqual(['attempt_allowance'], [k for k in raw if raw[k] != raw_after[k]])
        self.assertEqual(body, result['grant']['request'])
        self.assertEqual(result['grant'], after['attempt_allowances'][0])
        self.assertIsNone(self.store.claim(VM2, ident)['task'])
        self.assertEqual(20, self.store.detail(other)['fix_attempt_limit'])
        self.assertIsNone(self.store.claim(VM1, other)['task'])
        self.assertEqual(1, self.store.snapshot()['capabilities']['attempt_allowances'])

    def test_exactly_ten_failed_slots_and_no_budget_replenishment(self):
        ident = prepared_task(self.store); body = allowance_request(self.store, ident)
        self.store.apply_attempt_allowance('admin', body)
        for used in range(21, 31):
            claim = self.store.claim(VM1, ident)
            self.assertEqual(used - 1, claim['task']['fix_attempts_used'])
            # Older clients omitting a count still consume a genuine failure.
            row = finish(self.store, claim, used=used if used % 2 else None)
            self.assertEqual((used, used, 30 - used), tuple(row[k] for k in
                ('fix_attempts_used', 'total_fix_attempts', 'fix_attempts_remaining')))
            self.store.retry(VM1, ident, {**claim_identity(claim), 'execution_stopped': True,
                'external_state_checked': True, 'evidence': 'Concrete correction completed'})
        before = self.store.detail(ident)
        self.assertIsNone(self.store.claim(VM1, ident)['task'])
        replay = self.store.apply_attempt_allowance('admin', body)
        self.assertTrue(replay['replayed']); self.assertEqual(0, replay['task']['fix_attempts_remaining'])
        self.assertEqual(before, self.store.detail(ident))

    def test_same_host_attendance_and_approval_cycles_preserve_grant(self):
        ident = prepared_task(self.store); grant = self.grant(ident)['task']['attempt_allowance']
        claim = self.store.claim(VM1, ident)
        finish(self.store, claim, 'pending_external_approval', 20,
               approval_request={'thread_id': 'attendance', 'question_id': 'fresh-watch'})
        pending = self.store.detail(ident)
        self.assertEqual(grant, pending['attempt_allowance']); self.assertEqual(30, pending['fix_attempt_limit'])
        self.assertFalse(pending['ready']); self.assertIsNone(self.store.claim(VM1, ident)['task'])
        self.store.apply_approval('admin', {'source': {'thread_id': 'attendance', 'question_id': 'fresh-watch', 'message_id': 'actual-ready'},
            'decision': 'approved', 'decision_verified': True, 'response': 'Ready now', 'scope': 'Fresh attendance only',
            'tasks': [{'id': ident, 'generation': claim['task']['generation'], 'execution_stopped': True,
                       'external_state_checked': True, 'scope_evidence': 'Actual separate attendance response'}]})
        resumed = self.store.claim(VM1, ident)
        self.assertEqual(grant, resumed['task']['attempt_allowance'])
        self.assertEqual(20, resumed['task']['fix_attempts_used'])
        self.assertEqual(21, finish(self.store, resumed)['fix_attempts_used'])

    def test_real_host_transfer_expires_and_return_does_not_reactivate(self):
        ident = prepared_task(self.store); body = allowance_request(self.store, ident)
        self.store.apply_attempt_allowance('admin', body)
        claim = self.store.claim(VM1, ident); wait(self.store, claim, VM2)
        other = self.store.claim(VM2, ident)
        self.assertFalse(other['task']['attempt_allowance_active'])
        self.assertIsNotNone(other['task']['attempt_allowance']['expired_at'])
        self.assertEqual((0, 20, 20, 20), tuple(other['task'][k] for k in
            ('fix_attempts_used', 'total_fix_attempts', 'attempt_base', 'fix_attempt_limit')))
        wait(self.store, other, VM1); returned = self.store.claim(VM1, ident)
        self.assertEqual(20, returned['task']['fix_attempt_limit'])
        self.assertFalse(returned['task']['attempt_allowance_active'])
        before = self.store.detail(ident); self.assertTrue(self.store.apply_attempt_allowance('admin', body)['replayed'])
        self.assertEqual(before, self.store.detail(ident))

    def test_concurrent_restart_and_primary_source_cannot_duplicate_or_expand(self):
        ident = prepared_task(self.store); body = allowance_request(self.store, ident)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.apply_attempt_allowance('admin', body), range(8)))
        self.assertEqual(1, sum(not r['replayed'] for r in results))
        self.store = Store(self.path, clock=lambda: self.now, heavy_worker=VM1)
        self.assertTrue(self.store.apply_attempt_allowance('admin', body)['replayed'])
        before = self.store.detail(ident)
        for changes in ({'response': 'Different words'}, {'request_id': 'different-request'},
                        {'additional_attempts': 9}, {'source': {'thread_id': 'root', 'message_id': 'another-human'}}):
            with self.assertRaises(Conflict): self.store.apply_attempt_allowance('admin', {**body, **changes})
        self.assertEqual(before, self.store.detail(ident))
        other = prepared_task(self.store, 'other'); foreign = allowance_request(self.store, other, 'other')
        foreign['source'] = body['source']
        with self.assertRaises(Conflict): self.store.apply_attempt_allowance('admin', foreign)
        self.assertEqual({}, self.store.detail(other)['attempt_allowance'])

    def test_stale_foreign_unverified_malformed_or_stacked_grants_are_atomic(self):
        ident = prepared_task(self.store); body = allowance_request(self.store, ident); before = self.store.detail(ident)
        for actor in (VM1, VM2, 'dashboard'):
            with self.assertRaises(Conflict): self.store.apply_attempt_allowance(actor, body)
        variants = [{**body, key: value} for key, value in (
            ('additional_attempts', 0), ('additional_attempts', 11), ('additional_attempts', True),
            ('generation', 99), ('generation', True), ('worker', VM2), ('source', {}),
            ('decision_verified', False), ('execution_stopped', False), ('external_state_checked', False))]
        for key, value in (('step', 1), ('fix_attempts_used', True), ('requirements_sha256', 'a' * 64),
                           ('attempt_exception_sha256', 'a' * 64), ('attempt_allowance_sha256', 'a' * 64)):
            b = copy.deepcopy(body); b['expected'][key] = value; variants.append(b)
        for b in variants:
            with self.subTest(body=b), self.assertRaises((Conflict, Invalid)):
                self.store.apply_attempt_allowance('admin', b)
            self.assertEqual(before, self.store.detail(ident))
        self.grant(ident)
        stacked = allowance_request(self.store, ident, 'second-human')
        with self.assertRaises(Conflict): self.store.apply_attempt_allowance('admin', stacked)

    def test_automatic_and_explicit_retry_respect_allowance_at_six_reassignments(self):
        ident = prepared_task(self.store, reassignments=6); self.grant(ident)
        claim = self.store.claim(VM1, ident); finish(self.store, claim, used=21)
        self.now += 901; self.pulse()
        self.assertTrue(self.store.detail(ident)['ready'])
        retried = self.store.claim(VM1, ident); self.assertIsNotNone(retried['task'])
        finish(self.store, retried, used=29)
        self.store.retry(VM1, ident, {**claim_identity(retried), 'execution_stopped': True,
            'external_state_checked': True, 'evidence': 'Concrete fix at attempt29'})
        last = self.store.claim(VM1, ident); finish(self.store, last, used=30)
        self.now += 901; self.pulse(); row = self.store.detail(ident)
        self.assertFalse(row['ready']); self.assertIn('30 attempts', row['waiting_reason'])
        self.assertIsNone(self.store.claim(VM1, ident)['task'])
        self.assertIsNone(self.store.claim(VM2, ident)['task'])
        with self.assertRaises(Conflict):
            self.store.retry(VM1, ident, {**claim_identity(last), 'execution_stopped': True,
                'external_state_checked': True, 'evidence': 'Cannot invent attempt31'})

    def test_finish_and_admin_accounting_do_not_extend_grant_above_thirty(self):
        ident = prepared_task(self.store); self.grant(ident); claim = self.store.claim(VM1, ident)
        before = self.store.detail(ident)
        with self.assertRaises(Invalid): finish(self.store, claim, used=31)
        self.assertEqual(before, self.store.detail(ident))
        body = self.accounting_body(claim, 24)
        self.store.correct_attempts('admin', ident, body)
        after = self.store.detail(ident); self.assertEqual((24, 30, 6), tuple(after[k] for k in
            ('fix_attempts_used', 'fix_attempt_limit', 'fix_attempts_remaining')))
        over = self.accounting_body(claim, 31)
        with self.assertRaises(Invalid): self.store.correct_attempts('admin', ident, over)
        self.assertEqual(after, self.store.detail(ident))

    def accounting_body(self, claim, used):
        row = self.store.detail(claim['task']['id']); ref = {'path': '/private/verified/evidence.json', 'sha256': 'a' * 64}
        return {'request_id': 'accounting-omission-' + str(used), 'generation': row['generation'],
            'expected': {k: row[k] for k in BINDING},
            'corrected': {'fix_attempts_used': used, 'attempt_base': row['attempt_base'], 'total_fix_attempts': row['attempt_base'] + used},
            'owner_proof': {'task': row['id'], 'worker': row['owner'], 'generation': row['generation'], 'step': row['step'],
                'fix_attempts_used': used, 'verified': True, 'execution_stopped': True, 'external_state_checked': True,
                'unsettled_operations': [], 'receipt': ref, 'source': ref, 'evidence': 'Verified truthful missed usage'},
            'historical_proof': [], 'evidence': 'Only correct omitted truthful counters within the existing scoped allowance'}

    def test_closed_assignment_accounting_uses_its_historical_grant_not_current_host_limit(self):
        ident = prepared_task(self.store); self.grant(ident); old = self.store.claim(VM1, ident)
        wait(self.store, old, VM2); current = self.store.claim(VM2, ident)
        body = self.accounting_body(current, 0)
        body['corrected'].update(attempt_base=27, total_fix_attempts=27)
        body['historical_proof'] = [{'task': ident, 'worker': VM1, 'generation': old['task']['generation'], 'step': 0,
            'recorded_attempts': 20, 'fix_attempts_used': 27, 'omitted_attempts': 7, 'verified': True,
            'receipt': body['owner_proof']['receipt'], 'source': body['owner_proof']['source'], 'evidence': 'Closed granted host omitted seven'}]
        forged = copy.deepcopy(body); forged['historical_proof'][0].update(fix_attempts_used=31, omitted_attempts=11)
        forged['corrected'].update(attempt_base=31, total_fix_attempts=31)
        with self.assertRaises(Invalid): self.store.correct_attempts('admin', ident, forged)
        self.store.correct_attempts('admin', ident, body)
        row = self.store.detail(ident); self.assertEqual((0, 27, 27, 20), tuple(row[k] for k in
            ('fix_attempts_used', 'total_fix_attempts', 'attempt_base', 'fix_attempt_limit')))

    def test_schema_migration_preserves_old_service_history_and_default_limit(self):
        ident = prepared_task(self.store); before = self.raw(ident); before.pop('attempt_allowance')
        events = self.store.detail(ident)['events']
        with self.store.connection() as db:
            db.execute('DROP TABLE attempt_allowances'); db.execute('ALTER TABLE tasks DROP COLUMN attempt_allowance')
        migrated = Store(self.path, clock=lambda: self.now, heavy_worker=VM1)
        with migrated.connection() as db:
            row = dict(db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone())
        self.assertEqual('{}', row.pop('attempt_allowance')); self.assertEqual(before, row)
        self.assertEqual(events, migrated.detail(ident)['events'])
        self.assertEqual(20, migrated.detail(ident)['fix_attempt_limit'])
        self.assertEqual([], migrated.detail(ident)['attempt_allowances'])

    def test_truthful_base_correction_preserves_same_assignment_authority(self):
        ident = prepared_task(self.store, reassignments=2); self.grant(ident)
        claim = self.store.claim(VM1, ident); grant = claim['task']['attempt_allowance']
        body = self.accounting_body(claim, 24)
        body['corrected'].update(attempt_base=5, total_fix_attempts=29)
        body['historical_proof'] = [{'task': ident, 'worker': VM2, 'generation': 2, 'step': 0,
            'recorded_attempts': 0, 'fix_attempts_used': 5, 'omitted_attempts': 5, 'verified': True,
            'receipt': body['owner_proof']['receipt'], 'source': body['owner_proof']['source'], 'evidence': 'Earlier host omitted five'}]
        self.store.correct_attempts('admin', ident, body)
        row = self.store.detail(ident)
        self.assertTrue(row['attempt_allowance_active'])
        self.assertEqual((30, 6, 5), (row['fix_attempt_limit'], row['fix_attempts_remaining'], row['attempt_allowance']['attempt_base']))
        self.assertEqual(grant['request'], row['attempt_allowance']['request'])
        self.assertEqual(grant['id'], row['attempt_allowance']['id'])
        self.assertEqual(0, row['attempt_allowance']['request']['expected']['attempt_base'])
        self.assertEqual(25, finish(self.store, claim)['fix_attempts_used'])

    def test_engine_work_cannot_receive_an_ordinary_work_allowance(self):
        ident = prepared_task(self.store, task_type='engine_rebuild')
        with self.assertRaises(Conflict): self.grant(ident)


if __name__ == '__main__':
    unittest.main()
