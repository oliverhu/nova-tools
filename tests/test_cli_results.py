"""Contract cases for end-of-child reduction, immutability and crash recovery."""
import copy
import contextlib
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import subprocess
import sys
import fcntl
import os

from coordinator import cli_dispatch as adapter
from coordinator.cli_results import evaluate, reconcile
from test_cli_wait_settlement import fixture as wait_fixture


def fixture(outcome='completed'):
    run = {'run_id': 'b'*32, 'task': 'a'*16, 'worker': 'mini', 'queued_generation': 0,
           'cli_exit_code': 0, 'ended_at': 10, 'status': 'needs_attention'}
    report = {'result_schema': 1, 'run_id': run['run_id'], 'task': run['task'], 'generation': 1,
              'worker': 'mini', 'outcome': outcome, 'acceptance_complete': outcome == 'completed',
              'execution_stopped': True, 'cleanup': {'execution_stopped': True,
                  'external_state_checked': True, 'input_settled': True, 'unknown_operations': [],
                  'unsettled_operations': [], 'evidence': 'Owned child reaped; owned fixture closed with ACK'}}
    task = {'id': run['task'], 'generation': 1, 'owner': 'mini', 'status': outcome,
            'step': 1 if outcome == 'completed' else 0, 'steps': [{}], 'result': copy.deepcopy(report),
            'fix_attempts_used': 7, 'total_fix_attempts': 10, 'reassignment_count': 1}
    task['receipts'] = [{'task_id': run['task'], 'generation': 1, 'step': 0,
                         'receipt': {'outcome': outcome, 'receipt': copy.deepcopy(report)}}]
    docs = {'worker-report.json': report, 'finish-response.json': copy.deepcopy(task),
            'task-readback.json': copy.deepcopy(task)}
    docs['finish-response.json'].pop('receipts', None)
    return run, task, docs


def reduce(run, task, docs):
    return evaluate(run, task, docs, {}, adapter.validate_settlement)


class ResultTests(unittest.TestCase):
    def test_success_known_failure_and_human_approval_release_capacity_independently(self):
        for outcome in ('completed', 'blocked', 'pending_external_approval'):
            run, task, docs = fixture(outcome)
            if outcome == 'pending_external_approval':
                task['approval'] = {'pending': True}
                docs['finish-response.json'] = copy.deepcopy(task)
                docs['task-readback.json'] = copy.deepcopy(task)
            before = copy.deepcopy((run, task, docs))
            result = reduce(run, task, docs)
            self.assertEqual(result.outcome, outcome)
            self.assertEqual(result.acceptance_complete, outcome == 'completed')
            self.assertEqual(result.availability, 'available')
            self.assertEqual(before, (run, task, docs))
            # Another eligible task can pass unchanged service admission. This
            # does not retry the blocked/approval task or change its counters.
            next_task = {'status': 'queued', 'owner': None, 'requirements': {}, 'dependencies': []}
            adapter.validate_task(next_task, {'tasks': [task], 'workers': [{'id': 'mini',
                'online': True, 'capacity': {}}]}, {'worker': 'mini', 'admission': 'enabled'})

    def test_real_dependency_wait_reuses_audited_contract_without_claiming_completion(self):
        run, task, response, note, worker, report, wait = wait_fixture()
        report.update(result_schema=1, run_id=run['run_id'], outcome='waiting',
                      cleanup={'execution_stopped': True, 'external_state_checked': True,
                               'input_settled': True, 'unknown_operations': [],
                               'unsettled_operations': [], 'evidence': note['cleanup_evidence']})
        docs = {'worker-report.json': report, 'finish-response.json': task,
                'task-readback.json': task, 'wait-receipt.json': wait}
        result = reduce(run, task, docs)
        self.assertEqual(result.outcome, 'waiting')
        self.assertFalse(result.acceptance_complete)
        self.assertEqual(result.availability, 'available')
        bad = copy.deepcopy(docs); bad['wait-receipt.json']['evidence'] = 'invented'
        with self.assertRaises(ValueError): reduce(run, task, bad)

    def test_stale_conflicting_unknown_and_interrupted_results_fail_closed(self):
        changes = [('run', {'cli_exit_code': None}), ('run', {'queued_generation': 1}),
                   ('run', {'unreceived_message_ids': ['unread']}), ('task', {'owner': 'other'}),
                   ('task', {'generation': 2}), ('task', {'status': 'running'}),
                   ('report', {'generation': 2}), ('report', {'worker': 'other'}),
                   ('report', {'result_schema': 2}), ('report', {'result_schema': True}),
                   ('report', {'run_id': 'foreign'}), ('report', {'acceptance_complete': False}),
                   ('report', {'outcome': 'failed'}), ('cleanup', {'input_settled': False}),
                   ('cleanup', {'execution_stopped': False}), ('cleanup', {'unknown_operations': ['input']}),
                   ('cleanup', {'unsettled_operations': ['server']}), ('cleanup', {'evidence': ''})]
        for target, delta in changes:
            run, task, docs = fixture()
            values = {'run': run, 'task': task, 'report': docs['worker-report.json'],
                      'cleanup': docs['worker-report.json']['cleanup']}
            values[target].update(delta)
            with self.subTest(target=target, delta=delta), self.assertRaises(ValueError):
                reduce(run, task, docs)

    def test_contradictory_authoritative_result_blocks_even_with_good_report(self):
        for where in ('result', 'cleanup'):
            run, task, docs = fixture('blocked')
            target = task['result'] if where == 'result' else task['result']['cleanup']
            target['unknown_operations'] = ['remote outcome unknown']
            docs['finish-response.json'] = copy.deepcopy(task)
            docs['task-readback.json'] = copy.deepcopy(task)
            with self.assertRaises(ValueError): reduce(run, task, docs)

    def test_one_legacy_boundary_requires_actual_proof_and_never_rewrites(self):
        run, task, docs = fixture()
        for value in (docs['worker-report.json'], task['result'], task['receipts'][0]['receipt']['receipt']):
            del value['result_schema']; del value['run_id']
        docs['finish-response.json'] = copy.deepcopy(task)
        docs['task-readback.json'] = copy.deepcopy(task)
        before = copy.deepcopy(docs)
        self.assertTrue(reduce(run, task, docs).acceptance_complete)
        self.assertEqual(before, docs)
        del docs['worker-report.json']['cleanup']['input_settled']
        with self.assertRaises(ValueError): reduce(run, task, docs)

    def test_additive_result_recovery_and_evidence_preservation(self):
        run, task, docs = fixture('blocked')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name, value in docs.items(): adapter.save(root/name, value)
            originals = {name: (root/name).read_bytes() for name in docs}
            client = Mock(); client.request.return_value = task
            adapter.finalize_result(root, run, task, client)
            self.assertEqual(run['status'], 'settled')
            self.assertFalse(run['completion_verified'])
            audit = (root/'run-result.json').read_bytes()
            # Crash after journal write, before state write: deterministic replay.
            prior, _, _ = fixture('blocked')
            adapter.finalize_result(root, prior, task, client)
            self.assertEqual(audit, (root/'run-result.json').read_bytes())
            self.assertEqual(originals, {n: (root/n).read_bytes() for n in docs})
            adapter.save(root/'run-result.json', {'schema': 99})
            with self.assertRaises(ValueError): adapter.finalize_result(root, prior, task, client)

    def test_next_wake_uses_settled_local_result_but_keeps_service_and_account_gates(self):
        for outcome in ('completed', 'blocked', 'pending_external_approval', 'unknown'):
            run, task, docs = fixture('blocked' if outcome == 'unknown' else outcome)
            run['created_at'] = 1.0
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); run_dir = root/run['run_id']; run_dir.mkdir()
                for name, value in docs.items(): adapter.save(run_dir/name, value)
                client = Mock(); client.config = {'worker': 'mini'}; client.request.return_value = task
                if outcome != 'unknown': adapter.finalize_result(run_dir, run, task, client)
                adapter.save(run_dir/'state.json', run)
                next_task = {'id': 'c'*16, 'status': 'queued', 'generation': 0,
                             'owner': None, 'requirements': {}, 'dependencies': [],
                             'title': 'fixture next task', 'description': 'no model invocation'}
                config = {'worker': 'mini', 'hostname': 'fixture', 'admission': 'enabled',
                          'account_email_sha256': hashlib.sha256(b'fixture@example.test').hexdigest(),
                          'model': 'selected-model', 'reasoning': 'medium', 'codex': 'fixture'}
                adapter.save(root/'config.json', config)
                snapshot = {'tasks': [task, next_task], 'workers': [{'id': 'mini', 'online': True, 'capacity': {}}]}
                client.request.side_effect = lambda path: snapshot if path == '/api/state' else next_task
                account = ({'type': 'chatgpt', 'email': 'fixture@example.test'},
                           [{'model': 'selected-model', 'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]}],
                           {'ordinaryUsageAllowed': True})
                with patch.object(adapter, 'ROOT', root), patch.object(adapter, 'CONFIG', root/'config.json'), \
                     patch.object(adapter.socket, 'gethostname', return_value='fixture'), \
                     patch.object(adapter, 'Client', return_value=client), \
                     patch.object(adapter, 'rpc_account', return_value=account) as account_check, \
                     patch.object(adapter, 'spawn') as spawn, \
                     patch('sys.argv', ['worker', 'wake', '--task', next_task['id']]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if outcome == 'unknown':
                        with self.assertRaises(ValueError): adapter.main()
                        account_check.assert_not_called(); spawn.assert_not_called()
                    else:
                        adapter.main()
                        account_check.assert_called_once_with('fixture')
                        self.assertEqual(spawn.call_args.args[0]['task'], next_task['id'])
                        self.assertEqual(spawn.call_args.args[0]['account_email_sha256'], config['account_email_sha256'])

    def test_real_supervisor_child_exit_and_additive_result(self):
        for outcome in ('completed', 'blocked', 'pending_external_approval'):
            run, task, docs = fixture(outcome)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                for name, value in docs.items(): adapter.save(root/name, value)
                adapter.save(root/'state.json', run)
                (root/'prompt.txt').write_text('owned isolated fixture')
                client = Mock(); client.request.return_value = task
                real_popen = subprocess.Popen
                def owned_child(*args, **kwargs):
                    return real_popen([sys.executable, '-c',
                        'import sys; sys.stdin.buffer.read(); print("owned child exited")'], **kwargs)
                with (root/'worker.lock').open('w') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with patch.object(adapter, 'Client', return_value=client), \
                         patch.object(adapter.subprocess, 'Popen', side_effect=owned_child):
                        adapter.execute({'codex': sys.executable, 'reasoning': 'medium',
                            'model': 'fixture', 'repository': str(root), 'worker': 'mini'}, root, lock.fileno())
                final = json.loads((root/'state.json').read_text())
                self.assertEqual(final['status'], 'completed' if outcome == 'completed' else 'settled', final)
                self.assertEqual(final['cli_exit_code'], 0)
                self.assertEqual(final['availability'], 'available')
                self.assertEqual(json.loads((root/'run-result.json').read_text())['outcome'], outcome)

    def test_reaped_child_without_receipt_stays_unavailable(self):
        run, task, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            adapter.save(root/'state.json', run)
            (root/'prompt.txt').write_text('fixture')
            client = Mock(); client.request.return_value = task
            real_popen = subprocess.Popen
            with (root/'worker.lock').open('w') as lock:
                with patch.object(adapter, 'Client', return_value=client), \
                     patch.object(adapter.subprocess, 'Popen', side_effect=lambda *a, **kw:
                         real_popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], **kw)):
                    adapter.execute({'codex': sys.executable, 'reasoning': 'medium', 'model': 'fixture',
                                     'repository': str(root), 'worker': 'mini'}, root, lock.fileno())
            final = json.loads((root/'state.json').read_text())
            self.assertEqual(final['status'], 'needs_attention')
            self.assertEqual(final['cli_exit_code'], 0)
            self.assertNotIn('availability', final)
            self.assertFalse((root/'run-result.json').exists())

    def test_symlink_and_changed_api_refuse_commit(self):
        run, task, docs = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name, value in docs.items(): adapter.save(root/name, value)
            client = Mock(); client.request.return_value = {**task, 'generation': 2}
            with self.assertRaises(ValueError): adapter.finalize_result(root, run, task, client)
            self.assertFalse((root/'run-result.json').exists())
            (root/'worker-report.json').rename(root/'other.json')
            (root/'worker-report.json').symlink_to(root/'other.json')
            with self.assertRaises(ValueError): reconcile(root, run, task, adapter.validate_settlement)


if __name__ == '__main__': unittest.main()
