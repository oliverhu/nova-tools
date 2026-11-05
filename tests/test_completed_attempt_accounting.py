"""Closed-work corrections must preserve acceptance and every noncounter field."""
import concurrent.futures
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import test_attempt_allowances as fixtures
from test_allowance_amendments import amendment_request
from coordinator.attempt_accounting import BINDING, COUNTERS
from coordinator.store import Store, Conflict, Invalid, encoded


VM1, VM2 = fixtures.VM1, fixtures.VM2


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def reference(name):
    return {'path': '/private/verified/' + name, 'sha256': hashlib.sha256(name.encode()).hexdigest()}


def completed_request(store, ident, used):
    row = store.detail(ident)
    final = next(r['receipt'] for r in row['receipts']
                 if r['generation'] == row['generation'] and r['step'] == row['step'] - 1)
    return {'request_id': 'verified-postcompletion-omissions', 'generation': row['generation'],
        'expected': {key: row[key] for key in BINDING},
        'corrected': {'fix_attempts_used': used, 'total_fix_attempts': row['attempt_base'] + used,
                      'attempt_base': row['attempt_base']},
        'owner_proof': {'task': ident, 'worker': row['attempts_host'], 'generation': row['generation'],
            'step': row['step'], 'fix_attempts_used': used, 'verified': True,
            'execution_stopped': True, 'external_state_checked': True, 'unsettled_operations': [],
            'receipt': reference('owner-failure-ledger-supplement.json'),
            'source': reference('original-source-binding.json'),
            'evidence': 'Owning worker verified three omitted failed qualifications; acceptance preserved'},
        'historical_proof': [],
        'completion_proof': {'receipt': reference('immutable-original-completion.json'),
            'expected_result_sha256': digest(row['result']), 'expected_receipt_sha256': digest(final)},
        'evidence': 'Accounting only: retain completed acceptance, source, grants and unrelated running work'}


class CompletedAttemptAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name) / 'state'
        self.store = Store(self.path, clock=lambda: 1000.0, heavy_worker=VM1)
        for worker in (VM1, VM2):
            self.store.pulse(worker, {'capacity': {'cpus': 12}})

    def tearDown(self):
        self.temp.cleanup()

    def completed(self, allowance=True, steps=None):
        if allowance:
            ident = fixtures.prepared_task(self.store)
            self.store.apply_attempt_allowance('admin', fixtures.allowance_request(self.store, ident))
            self.store.amend_attempt_allowance('admin', amendment_request(self.store, ident))
        else:
            ident = self.store.submit('admin', {'request_key': 'closed', 'title': 'Qualified feature',
                **({'steps': steps} if steps else {})})['id']
        claim = self.store.claim(VM1, ident)
        while True:
            result = self.store.finish(VM1, ident, {**fixtures.claim_identity(claim),
                'outcome': 'completed', 'receipt': {
                    'summary': 'Qualified feature landed; all operations settled',
                    'outcome': 'completed', 'acceptance_complete': True,
                    'execution_stopped': True, 'external_state_checked': True, 'unsettled_operations': [],
                    'fix_attempts_used': 25 if allowance else 2,
                    'landed_commit': 'a' * 40, 'validation': {'native': 'PASS', 'models': 'PASS'}}})
            if result['status'] == 'completed':
                return ident, claim
            claim = self.store.claim(VM1, ident)

    def raw(self, ident):
        with self.store.connection() as db:
            return dict(db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone())

    def test_25_to_28_with_forty_cap_preserves_completed_feature_and_active_work(self):
        ident, claim = self.completed()
        dependent = self.store.submit('admin', {'request_key': 'dependent', 'title': 'Dependent',
            'dependencies': [ident]})['id']
        active = self.store.claim(VM1, dependent)
        before = self.store.detail(ident); raw = self.raw(ident)
        other_raw = self.raw(dependent); other_detail = self.store.detail(dependent)
        body = completed_request(self.store, ident, 28)
        result = self.store.correct_attempts('admin', ident, body)
        after = self.store.detail(ident); raw_after = self.raw(ident)
        self.assertEqual(set(COUNTERS) - {'attempt_base'}, {k for k in raw if raw[k] != raw_after[k]})
        for key in raw.keys() - set(COUNTERS):
            self.assertEqual(raw[key], raw_after[key], key)
        self.assertEqual(('completed', 1, 28, 28, 0, 40, 12), tuple(after[k] for k in
            ('status', 'step', 'fix_attempts_used', 'total_fix_attempts', 'attempt_base',
             'fix_attempt_limit', 'fix_attempts_remaining')))
        for key in ('result', 'receipts', 'attempt_allowance', 'attempt_allowances',
                    'attempt_allowance_amendments', 'approval', 'ready', 'waiting_reason'):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(before['events'], after['events'][:-1])
        self.assertEqual('attempts-corrected', after['events'][-1]['kind'])
        self.assertEqual(body, result['correction']['request'])
        self.assertEqual([result['correction']], after['attempt_corrections'])
        self.assertEqual(other_raw, self.raw(dependent)); self.assertEqual(other_detail, self.store.detail(dependent))
        self.store.heartbeat(VM1, dependent, fixtures.claim_identity(active))
        with self.assertRaises(Conflict):
            self.store.heartbeat(VM1, ident, fixtures.claim_identity(claim))
        self.assertNotIn(claim['claim_token'], json.dumps(result))
        self.assertEqual(1, self.store.snapshot()['capabilities']['completed_attempt_correction'])

    def test_concurrent_replay_restart_and_later_correction_never_restore_old_counters(self):
        ident, _ = self.completed(); body = completed_request(self.store, ident, 28)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.correct_attempts('admin', ident, body), range(4)))
        self.assertEqual(1, sum(not r['replayed'] for r in results))
        self.store = Store(self.path, clock=lambda: 1000.0)
        second = completed_request(self.store, ident, 29); second['request_id'] += '-later'
        self.store.correct_attempts('admin', ident, second)
        before = self.store.detail(ident)
        replay = self.store.correct_attempts('admin', ident, body)
        self.assertTrue(replay['replayed']); self.assertEqual(29, replay['task']['fix_attempts_used'])
        self.assertEqual(before, self.store.detail(ident))
        with self.assertRaises(Conflict):
            self.store.correct_attempts('admin', ident, {**body, 'evidence': 'Changed duplicate'})
        stale = copy.deepcopy(body); stale['request_id'] += '-stale'
        with self.assertRaises(Conflict): self.store.correct_attempts('admin', ident, stale)
        self.assertEqual(before, self.store.detail(ident))

    def test_rejects_missing_stale_foreign_or_unsettled_proofs_and_limits_atomically(self):
        ident, _ = self.completed(); body = completed_request(self.store, ident, 28)
        before = self.store.detail(ident)
        for actor in (VM1, VM2, 'dashboard'):
            with self.assertRaises(Conflict): self.store.correct_attempts(actor, ident, body)
        variants = []
        def changed(section, key, value):
            b = copy.deepcopy(body); (b[section] if section else b)[key] = value; variants.append(b)
        b = copy.deepcopy(body); del b['completion_proof']; variants.append(b)
        for key, value in (('receipt', reference('original.json') | {'path': 'relative'}),
                           ('expected_result_sha256', 'b' * 64), ('expected_receipt_sha256', 'b' * 64),
                           ('expected_receipt_sha256', 'bad'), ('extra', True)):
            changed('completion_proof', key, value)
        for key, value in (('generation', 99), ('execution_stopped', False), ('verified', False),
                           ('external_state_checked', False), ('unsettled_operations', ['active']),
                           ('worker', VM2), ('task', 'foreign'), ('step', 0)):
            changed('owner_proof', key, value)
        changed(None, 'generation', 99)
        for key, value in (('owner', VM2), ('owner', None), ('step', 0), ('reassignment_count', 1)):
            changed('expected', key, value)
        for used in (24, 41):
            b = copy.deepcopy(body); b['corrected'].update(fix_attempts_used=used, total_fix_attempts=used)
            b['owner_proof']['fix_attempts_used'] = used; variants.append(b)
        for b in variants:
            with self.subTest(body=b), self.assertRaises((Conflict, Invalid)):
                self.store.correct_attempts('admin', ident, b)
            self.assertEqual(before, self.store.detail(ident))

    def test_default_twenty_limit_and_multistep_final_receipt(self):
        ident, _ = self.completed(False, [
            {'name': 'first', 'kind': 'work', 'instructions': 'Perform first acceptance'},
            {'name': 'last', 'kind': 'work', 'instructions': 'Perform final acceptance'}])
        self.assertEqual(2, self.store.detail(ident)['step'])
        body = completed_request(self.store, ident, 21); before = self.store.detail(ident)
        with self.assertRaises(Invalid): self.store.correct_attempts('admin', ident, body)
        self.assertEqual(before, self.store.detail(ident))
        body = completed_request(self.store, ident, 3)
        after = self.store.correct_attempts('admin', ident, body)['task']
        self.assertEqual(('completed', 2, 3, 20), tuple(after[k] for k in
                         ('status', 'step', 'fix_attempts_used', 'fix_attempt_limit')))

    def test_legacy_missing_or_contradictory_acceptance_cannot_be_qualified_by_addendum(self):
        ident, _ = self.completed(); original = self.store.detail(ident)
        final_step = original['step'] - 1
        for field, value in (('outcome', 'blocked'), ('outcome', None),
                             ('acceptance_complete', False), ('acceptance_complete', None),
                             ('execution_stopped', False), ('external_state_checked', None),
                             ('unsettled_operations', None), ('unsettled_operations', ['unknown'])):
            # Model persisted legacy evidence; no live fixture/server is mutated.
            result = {**original['result'], field: value}
            with self.store.connection() as db:
                db.execute('UPDATE tasks SET result=? WHERE id=?', (encoded(result), ident))
                db.execute('UPDATE receipts SET receipt=? WHERE task_id=? AND generation=? AND step=?',
                    (encoded({'outcome': 'completed', 'receipt': result}), ident,
                     original['generation'], final_step))
            body = completed_request(self.store, ident, 28); before = self.store.detail(ident)
            with self.subTest(field=field, value=value), self.assertRaises(Conflict):
                self.store.correct_attempts('admin', ident, body)
            self.assertEqual(before, self.store.detail(ident))

    def test_missing_or_mismatched_service_receipt_rejected_without_changes(self):
        ident, _ = self.completed(); body = completed_request(self.store, ident, 28)
        with self.store.connection() as db:
            db.execute('UPDATE receipts SET receipt=? WHERE task_id=? AND generation=? AND step=?',
                (encoded({'outcome': 'blocked', 'receipt': self.store.detail(ident)['result']}),
                 ident, body['generation'], body['expected']['step'] - 1))
        before = self.store.detail(ident)
        with self.assertRaises(Conflict): self.store.correct_attempts('admin', ident, body)
        self.assertEqual(before, self.store.detail(ident))
        with self.store.connection() as db:
            db.execute('DELETE FROM receipts WHERE task_id=? AND generation=? AND step=?',
                       (ident, body['generation'], body['expected']['step'] - 1))
        before = self.store.detail(ident)
        with self.assertRaises(Conflict): self.store.correct_attempts('admin', ident, body)
        self.assertEqual(before, self.store.detail(ident))

    def test_cancelled_engine_publish_and_retained_resources_are_rejected(self):
        ident, _ = self.completed(); body = completed_request(self.store, ident, 28)
        for field, value in (('status', 'cancelled'), ('task_type', 'engine_rebuild'),
                             ('task_type', 'publish'), ('steps', encoded([{'kind': 'publish'}])),
                             ('step', 0)):
            original = self.raw(ident)[field]
            with self.store.connection() as db:
                db.execute('UPDATE tasks SET ' + field + '=? WHERE id=?', (value, ident))
            b = copy.deepcopy(body)
            if field in BINDING: b['expected'][field] = value
            if field == 'step': b['owner_proof']['step'] = value
            before = self.store.detail(ident)
            with self.subTest(field=field), self.assertRaises((Conflict, Invalid)):
                self.store.correct_attempts('admin', ident, b)
            self.assertEqual(before, self.store.detail(ident))
            with self.store.connection() as db:
                db.execute('UPDATE tasks SET ' + field + '=? WHERE id=?', (original, ident))
        with self.store.connection() as db:
            db.execute('INSERT INTO locks VALUES(?,?,?,?,?)', ('legacy-resource', ident, body['generation'], VM1, 1000.0))
        before = self.store.detail(ident)
        with self.assertRaisesRegex(Conflict, 'retained task resource'):
            self.store.correct_attempts('admin', ident, body)
        self.assertEqual(before, self.store.detail(ident))


if __name__ == '__main__':
    unittest.main()
