import concurrent.futures
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from coordinator.attempt_accounting import BINDING
from coordinator.store import Conflict, Invalid, Store


VM2 = 'oliverhu-mac-studio-vm2'
STUDIO = 'oliverhu-mac-studio'


class AttemptAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = Store(Path(self.temp.name) / 'state', clock=lambda: self.now)
        for worker in (VM2, STUDIO):
            self.store.pulse(worker, {'capacity': {'cpus': 12, 'memory_gb': 24, 'disk_gb': 500}})

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def identity(claim):
        return {'generation': claim['task']['generation'], 'step': claim['task']['step'],
                'claim_token': claim['claim_token']}

    def wait(self, claim, worker):
        return self.store.defer(claim['task']['owner'], claim['task']['id'], {
            **self.identity(claim), 'worker': worker, 'execution_stopped': True,
            'external_state_checked': True, 'evidence': 'Actual operations settled; retain original failures'})

    def omitted_history(self, studio_retry=False):
        task = self.store.submit('admin', {'request_key': 'parent', 'title': 'Original catalog'})
        first = self.store.claim(VM2, task['id'])
        self.store.finish(VM2, task['id'], {**self.identity(first), 'outcome': 'blocked', 'receipt': {
            'summary': 'Original twenty VM2 failures retained', 'execution_stopped': True, 'fix_attempts_used': 20}})
        self.store.defer('admin', task['id'], {'generation': 1, 'worker': STUDIO,
            'execution_stopped': True, 'external_state_checked': True, 'evidence': 'Genuine host handoff'})
        studio = self.store.claim(STUDIO, task['id'])
        if studio_retry:
            # Several same-host claims still represent one closed assignment.
            self.wait(studio, STUDIO)
            studio = self.store.claim(STUDIO, task['id'])
        self.wait(studio, VM2)  # Legacy wait omits known Studio failures from canonical counts.
        current = self.store.claim(VM2, task['id'])
        return current, studio

    @staticmethod
    def reference(name):
        return {'path': '/private/evidence/' + name,
                'sha256': hashlib.sha256(name.encode()).hexdigest()}

    def request(self, claim, studio):
        row = self.store.detail(claim['task']['id'])
        return {'request_id': 'verified-omissions-01', 'generation': row['generation'],
            'expected': {key: row[key] for key in BINDING},
            'corrected': {'fix_attempts_used': 2, 'total_fix_attempts': 37, 'attempt_base': 35},
            'owner_proof': {'task': row['id'], 'worker': VM2, 'generation': row['generation'], 'step': row['step'],
                'fix_attempts_used': 2, 'verified': True, 'execution_stopped': True,
                'external_state_checked': True, 'unsettled_operations': [],
                'receipt': self.reference('current-failure-ledger.json'),
                'source': self.reference('current-source-binding.json'), 'evidence': 'Owner confirms two settled failures'},
            'historical_proof': [{'task': row['id'], 'worker': STUDIO,
                'generation': studio['task']['generation'], 'step': 0,
                'recorded_attempts': 0, 'fix_attempts_used': 15, 'omitted_attempts': 15,
                'verified': True, 'receipt': self.reference('historical-wait.json'),
                'source': self.reference('historical-source-binding.json'), 'evidence': 'Final host ledger proves omitted fifteen'}],
            'evidence': 'Reconcile omitted closed-host15 and current2; original acceptance remains incomplete'}

    def raw_task(self, ident):
        with self.store.connection() as db:
            return dict(db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone())

    def test_exact_correction_preserves_claim_history_and_future_accounting(self):
        claim, studio = self.omitted_history()
        ident = claim['task']['id']; body = self.request(claim, studio)
        before = self.store.detail(ident); raw_before = self.raw_task(ident)
        result = self.store.correct_attempts('admin', ident, body)
        after = self.store.detail(ident); raw_after = self.raw_task(ident)
        self.assertEqual((2, 37, 35), tuple(after[k] for k in
                         ('fix_attempts_used', 'total_fix_attempts', 'attempt_base')))
        for key in raw_before.keys() - {'fix_attempts_used', 'total_fix_attempts', 'attempt_base'}:
            self.assertEqual(raw_before[key], raw_after[key], key)
        self.assertEqual(before['receipts'], after['receipts'])
        self.assertEqual(before['events'], after['events'][:-1])
        self.assertEqual('attempts-corrected', after['events'][-1]['kind'])
        self.assertEqual(body, result['correction']['request'])
        self.assertEqual(result['correction'], after['attempt_corrections'][0])
        self.assertNotIn(claim['claim_token'], json.dumps(result))
        self.store.heartbeat(VM2, ident, self.identity(claim))  # Original claim remains valid.
        self.store.finish(VM2, ident, {**self.identity(claim), 'outcome': 'blocked', 'receipt': {
            'summary': 'Next genuine failure', 'execution_stopped': True, 'fix_attempts_used': 3}})
        row = self.store.detail(ident)
        self.assertEqual((3, 38, 35), tuple(row[k] for k in
                         ('fix_attempts_used', 'total_fix_attempts', 'attempt_base')))
        self.store.retry(VM2, ident, {**self.identity(claim), 'execution_stopped': True,
            'external_state_checked': True, 'evidence': 'Actual failure corrected'})
        same = self.store.claim(VM2, ident)
        self.assertEqual((3, 38, 35), tuple(same['task'][k] for k in
                         ('fix_attempts_used', 'total_fix_attempts', 'attempt_base')))
        self.wait(same, STUDIO)
        changed = self.store.claim(STUDIO, ident)
        self.assertEqual((0, 38, 38), tuple(changed['task'][k] for k in
                         ('fix_attempts_used', 'total_fix_attempts', 'attempt_base')))
        self.assertEqual(3, changed['task']['reassignment_count'])

    def test_idempotence_restart_concurrency_and_changed_duplicate(self):
        claim, studio = self.omitted_history(); ident = claim['task']['id']; body = self.request(claim, studio)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.store.correct_attempts('admin', ident, body), range(2)))
        self.assertEqual([False, True], sorted(r['replayed'] for r in results))
        restarted = Store(self.store.path, clock=lambda: self.now)
        self.assertTrue(restarted.correct_attempts('admin', ident, body)['replayed'])
        before = self.store.detail(ident)
        with self.assertRaises(Conflict):
            self.store.correct_attempts('admin', ident, {**body, 'evidence': 'Changed duplicate'})
        self.assertEqual(before, self.store.detail(ident))
        self.assertEqual(1, len(before['attempt_corrections']))
        self.assertEqual(1, sum(e['kind'] == 'attempts-corrected' for e in before['events']))

    def test_rejects_worker_stale_foreign_unsettled_and_malformed_atomically(self):
        claim, studio = self.omitted_history(); ident = claim['task']['id']; body = self.request(claim, studio)
        before = self.store.detail(ident)
        for actor in (VM2, STUDIO):
            with self.assertRaises(Conflict): self.store.correct_attempts(actor, ident, body)
        variants = []
        def changed(section, key, value):
            b = copy.deepcopy(body)
            (b[section] if section else b)[key] = value; variants.append(b)
        for key, value in [('generation', 99), ('generation', True), ('request_id', 'x'), ('extra', 1)]:
            changed(None, key, value)
        for key, value in [('owner', STUDIO), ('status', 'completed'), ('step', 1),
                           ('attempts_host', STUDIO), ('reassignment_count', 1), ('fix_attempts_used', True)]:
            changed('expected', key, value)
        for key, value in [('execution_stopped', False), ('external_state_checked', False), ('verified', False),
                           ('unsettled_operations', ['active Downloads']), ('unsettled_operations', None),
                           ('worker', STUDIO), ('task', 'foreign'), ('generation', 99), ('fix_attempts_used', 3),
                           ('receipt', {'path': 'relative', 'sha256': 'a'*64}),
                           ('source', {'path': '/proof', 'sha256': 'bad'})]:
            changed('owner_proof', key, value)
        for key, value in [('worker', VM2), ('task', 'foreign'), ('generation', 99), ('step', 9),
                           ('recorded_attempts', 1), ('verified', False), ('fix_attempts_used', 35),
                           ('omitted_attempts', -1)]:
            b = copy.deepcopy(body); b['historical_proof'][0][key] = value; variants.append(b)
        for b in variants:
            with self.subTest(body=b), self.assertRaises((Conflict, Invalid)):
                self.store.correct_attempts('admin', ident, b)
            self.assertEqual(before, self.store.detail(ident))

    def test_rejects_decreases_inconsistent_base_limits_and_reused_assignment(self):
        claim, studio = self.omitted_history(studio_retry=True); ident = claim['task']['id']; body = self.request(claim, studio)
        before = self.store.detail(ident)
        for counters in ({'fix_attempts_used': 2, 'total_fix_attempts': 37, 'attempt_base': 34},
                         {'fix_attempts_used': 2, 'total_fix_attempts': 19, 'attempt_base': 17},
                         {'fix_attempts_used': 21, 'total_fix_attempts': 56, 'attempt_base': 35}):
            b = copy.deepcopy(body); b['corrected'] = counters; b['owner_proof']['fix_attempts_used'] = counters['fix_attempts_used']
            with self.assertRaises(Invalid): self.store.correct_attempts('admin', ident, b)
            self.assertEqual(before, self.store.detail(ident))
        duplicate = copy.deepcopy(body)
        earlier = copy.deepcopy(duplicate['historical_proof'][0]); earlier['generation'] -= 1
        duplicate['historical_proof'].append(earlier)
        duplicate['corrected'].update(attempt_base=50, total_fix_attempts=52)
        with self.assertRaisesRegex(Conflict, 'already been accounted'):
            self.store.correct_attempts('admin', ident, duplicate)
        self.assertEqual(before, self.store.detail(ident))
        self.store.correct_attempts('admin', ident, body)
        after = self.store.detail(ident)
        b = copy.deepcopy(body); b['request_id'] = 'verified-omissions-02'
        b['expected'] = {key: after[key] for key in BINDING}
        b['corrected'].update(attempt_base=50, total_fix_attempts=52)
        with self.assertRaisesRegex(Conflict, 'already been accounted'):
            self.store.correct_attempts('admin', ident, b)
        self.assertEqual(after, self.store.detail(ident))
        b = copy.deepcopy(body); b['request_id'] = 'verified-current-only-02'
        b['expected'] = {key: after[key] for key in BINDING}; b['historical_proof'] = []
        b['corrected'].update(fix_attempts_used=3, total_fix_attempts=38)
        b['owner_proof']['fix_attempts_used'] = 3
        self.assertEqual(38, self.store.correct_attempts('admin', ident, b)['task']['total_fix_attempts'])

    def test_does_not_grant_twenty_first_attempt_or_change_dependencies(self):
        claim, studio = self.omitted_history(); ident = claim['task']['id']; body = self.request(claim, studio)
        dependent = self.store.submit('admin', {'request_key': 'dependent', 'title': 'Dependent', 'dependencies': [ident]})
        body['corrected'].update(fix_attempts_used=20, total_fix_attempts=55)
        body['owner_proof']['fix_attempts_used'] = 20
        self.store.correct_attempts('admin', ident, body)
        self.assertFalse(self.store.detail(dependent['id'])['ready'])
        self.wait(claim, VM2)
        self.assertIsNone(self.store.claim(VM2, ident)['task'])

    def test_replay_after_generation_advances_does_not_restore_old_state(self):
        claim, studio = self.omitted_history(); ident = claim['task']['id']; body = self.request(claim, studio)
        self.store.correct_attempts('admin', ident, body)
        self.wait(claim, VM2)
        newer = self.store.claim(VM2, ident)
        before = self.store.detail(ident)
        replay = self.store.correct_attempts('admin', ident, body)
        self.assertTrue(replay['replayed'])
        self.assertEqual(newer['task']['generation'], replay['task']['generation'])
        self.assertEqual(before, self.store.detail(ident))
        stale = copy.deepcopy(body); stale['request_id'] = 'another-stale-request'
        with self.assertRaisesRegex(Conflict, 'changed'):
            self.store.correct_attempts('admin', ident, stale)
        self.assertEqual(before, self.store.detail(ident))

    def test_consistent_but_false_historical_recorded_count_is_rejected(self):
        claim, studio = self.omitted_history(); ident = claim['task']['id']; body = self.request(claim, studio)
        body['historical_proof'][0].update(recorded_attempts=1, fix_attempts_used=16)
        before = self.store.detail(ident)
        with self.assertRaisesRegex(Conflict, 'actual host transfer'):
            self.store.correct_attempts('admin', ident, body)
        self.assertEqual(before, self.store.detail(ident))


if __name__ == '__main__':
    unittest.main()
