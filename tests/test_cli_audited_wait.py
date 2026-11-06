import copy
import json
import unittest
from test_cli_wait_settlement import fixture
from coordinator import cli_dispatch as adapter


def audited():
    args = list(fixture())
    run, task, response, note, worker, report, wait = args
    task['approval'] = None
    for k in ('attempts_host', 'reassignment_count', 'attempt_allowance', 'attempt_exception', 'steps', 'request_key'):
        task[k] = {'attempts_host': worker, 'reassignment_count': 0, 'attempt_allowance': {}, 'attempt_exception': {}, 'steps': [], 'request_key': 'fixture'}[k]
    task['events'][-1]['id'] = 1
    original = copy.deepcopy(task)
    task['dependencies'] = []
    task['events'].append({'id': 2, 'task_id': task['id'], 'actor': 'admin', 'kind': 'waiting', 'at': 12.0,
                          'detail': json.dumps({'worker': worker, 'dependencies': [], 'evidence': 'Reviewed scoped dependency disposition'})})
    return [*args, original]


class AuditedWaitTests(unittest.TestCase):
    def test_exact_admin_transition_preserves_inputs(self):
        args = audited(); before = copy.deepcopy(args)
        adapter.validate_settlement(*args)
        self.assertEqual(before, args)
        args[6]["worker_id"] = args[6].pop("worker")
        adapter.validate_settlement(*args)
        args[6]["worker"] = "conflicting"
        with self.assertRaises(ValueError): adapter.validate_settlement(*args)

    def test_claim_conflicts_and_missing_proof_refuse(self):
        for index, delta in [(1, {'generation': 2}), (1, {'owner': 'mini'}), (1, {'fix_attempts_used': 13}),
                             (1, {'receipts': []}), (1, {'requirements': {'worker': 'foreign'}}),
                             (3, {'unknown_operations': ['input']}), (3, {'input_settled': False}),
                             (5, {'human_stop': True}), (6, {'external_state_checked': False})]:
            args = copy.deepcopy(audited()); args[index].update(delta)
            with self.subTest(delta=delta), self.assertRaises(ValueError): adapter.validate_settlement(*args)
        args = audited(); args[7] = None
        with self.assertRaises(ValueError): adapter.validate_settlement(*args)

    def test_admin_event_binding_and_single_transition(self):
        for delta in ({'actor': 'other'}, {'kind': 'claimed'}, {'id': 0}, {'at': 1}, {'detail': 'bad'}, {'task_id': 'foreign'}):
            args = audited(); args[1]['events'][-1].update(delta)
            with self.subTest(delta=delta), self.assertRaises(ValueError): adapter.validate_settlement(*args)
        for delta in ({'worker': 'other'}, {'dependencies': ['other']}, {'evidence': ''}, {'approval': True}):
            args = audited(); e = args[1]['events'][-1]; d = json.loads(e['detail']); d.update(delta); e['detail'] = json.dumps(d)
            with self.subTest(delta=delta), self.assertRaises(ValueError): adapter.validate_settlement(*args)
        args = audited(); args[1]['events'].append(copy.deepcopy(args[1]['events'][-1]))
        with self.assertRaises(ValueError): adapter.validate_settlement(*args)
