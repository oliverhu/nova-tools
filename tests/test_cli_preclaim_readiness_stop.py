import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coordinator import cli_dispatch as adapter


def fixture():
    run = {'run_id': 'b' * 32, 'task': 'a' * 16, 'worker': 'mini1', 'created_at': 1.0,
           'started_at': 2.0, 'supervisor_pid': 100, 'cli_pid': 101,
           'queued_generation': 0, 'status': 'needs_attention', 'cli_exit_code': 0,
           'ended_at': 10.0, 'completion_verified': False, 'task_status': 'queued'}
    task = {'id': run['task'], 'status': 'queued', 'generation': 0, 'step': 0,
            'owner': None, 'last_owner': None, 'requirements': {'worker': 'mini1'},
            'dependencies': [], 'result': {}, 'receipts': [], 'approval': None,
            'fix_attempts_used': 0, 'total_fix_attempts': 0, 'attempt_base': 0,
            'reassignment_count': 0, 'events': [{'kind': 'submitted', 'task_id': run['task']}]}
    report = {'task': task['id'], 'worker': 'mini1', 'generation': 0, 'queued_generation': 0,
              'outcome': 'queued_prerequisite_not_claimed', 'api_status': 'queued',
              'acceptance_complete': False, 'claim_attempted': False, 'finish_attempted': False,
              'finish_response_path': None, 'summary': 'Missing exact readiness plan; no operation attempted',
              'cleanup': {'execution_stopped': True, 'owned_runtime_operations': [], 'input_operations': [],
                          'owned_app_or_server_started': False, 'consent_requested': False,
                          'source_modified': False, 'inherited_files_staged_work_prior_receipts_preserved': True,
                          'unrelated_tasks_unchanged': True}}
    note = {'run_id': run['run_id'], 'task': task['id'], 'worker': 'mini1', 'generation': 0,
            'execution_stopped': True, 'external_state_checked': True, 'input_settled': True,
            'unknown_operations': [], 'cleanup_evidence': 'Actual owner stopped without claim/runtime/input; external state verified'}
    return run, task, report, copy.deepcopy(task), note, 'mini1'


class PreclaimReadinessStopTests(unittest.TestCase):
    def test_truthful_no_attempt_report_needs_no_invented_finish_or_failed_claim(self):
        args = fixture(); before = copy.deepcopy(args)
        adapter.validate_preclaim_readiness_stop(*args)
        self.assertEqual(before, args)
        self.assertNotIn('claim_attempt', args[2])
        self.assertFalse(args[2]['claim_attempted'])
        self.assertFalse(args[2]['finish_attempted'])

    def test_changed_claim_history_wrong_worker_or_uncertain_run_is_rejected(self):
        cases = [(0, {'status': 'running'}), (0, {'cli_exit_code': None}),
                 (0, {'cli_exit_code': 1}), (0, {'cli_exit_code': True}),
                 (0, {'ended_at': float('nan')}), (0, {'ended_at': float('inf')}),
                 (0, {'cli_pid': None}), (0, {'supervisor_pid': 0}),
                 (0, {'started_at': 11.0}), (0, {'created_at': float('nan')}),
                 (0, {'queued_generation': 1}), (0, {'worker': 'foreign'}),
                 (0, {'completion_verified': True}), (1, {'status': 'blocked'}),
                 (1, {'status': 'pending_external_approval'}), (1, {'generation': 1}),
                 (1, {'owner': 'mini1'}), (1, {'last_owner': 'mini1'}),
                 (1, {'requirements': {'worker': 'foreign'}}), (1, {'result': {'execution_stopped': True}}),
                 (1, {'receipts': [{}]}), (1, {'approval': {'pending': True}}),
                 (1, {'events': [{'kind': 'claimed'}]}), (1, {'fix_attempts_used': 1}),
                 (1, {'total_fix_attempts': 1}), (1, {'attempt_base': 1}),
                 (1, {'reassignment_count': 1}), (3, {'dependencies': ['changed']}),
                 (3, {'generation': 1}), (4, {'run_id': 'foreign'}),
                 (4, {'external_state_checked': False}), (4, {'input_settled': False}),
                 (4, {'cleanup_evidence': ''}), (4, {'unknown_operations': ['unknown']})]
        for index, delta in cases:
            args = list(copy.deepcopy(fixture())); args[index].update(delta)
            with self.subTest(index=index, delta=delta), self.assertRaises(ValueError):
                adapter.validate_preclaim_readiness_stop(*args)

    def test_actual_holds_side_effects_and_contradictory_reports_are_rejected(self):
        for delta in [{'outcome': 'unclaimed_stopped'}, {'task': 'foreign'}, {'generation': 1},
                      {'acceptance_complete': True}, {'claim_attempted': True}, {'finish_attempted': True},
                      {'claim_attempt': {'exit_code': 1}}, {'finish_response_path': '/invented.json'},
                      {'human_stop': True}, {'approval_request': {'question_id': 'pending'}},
                      {'unsettled_operations': ['input']}, {'unknown_operations': ['server']},
                      {'execution_stopped': False}, {'external_state_checked': False}]:
            args = list(copy.deepcopy(fixture())); args[2].update(delta)
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                adapter.validate_preclaim_readiness_stop(*args)
        for delta in [{'execution_stopped': False}, {'owned_runtime_operations': ['server']},
                      {'input_operations': ['click']}, {'owned_app_or_server_started': True},
                      {'consent_requested': True}, {'source_modified': True},
                      {'inherited_files_staged_work_prior_receipts_preserved': False},
                      {'unrelated_tasks_unchanged': False}, {'human_stop': True},
                      {'unknown_operations': ['unknown']}, {'external_state_checked': False}]:
            args = list(copy.deepcopy(fixture())); args[2]['cleanup'].update(delta)
            with self.subTest(cleanup=delta), self.assertRaises(ValueError):
                adapter.validate_preclaim_readiness_stop(*args)

    def test_command_only_changes_local_admission_and_retains_byte_exact_history(self):
        failures = [None, 'busy', 'later-run', 'missing-readback', 'foreign-report', 'stale-api',
                    'finish-marker', 'claim-response', 'symlink-report', 'changed-during-api', 'unknown-note']
        for failure in failures:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                run, task, report, readback, note, worker = fixture()
                root = Path(directory).resolve(); run_dir = root/run['run_id']; run_dir.mkdir()
                config = root/'config.json'; adapter.save(config, {'worker': worker, 'hostname': 'fixture'})
                # Deliberately nonstandard formatting to prove the backup is byte-exact.
                state_bytes = (json.dumps(run, separators=(', ', ' : ')) + '\n\n').encode()
                (run_dir/'state.json').write_bytes(state_bytes)
                if failure == 'foreign-report': report['worker'] = 'foreign'
                adapter.save(run_dir/'worker-report.json', report)
                if failure != 'missing-readback': adapter.save(run_dir/'task-readback.json', readback)
                if failure == 'later-run': adapter.save(root/('c' * 32)/'state.json', {**run, 'run_id': 'c' * 32, 'created_at': 2.0})
                if failure == 'finish-marker': adapter.save(run_dir/'finish-response.json', {'finish_invoked': False})
                if failure == 'claim-response': adapter.save(run_dir/'claim-response.json', {'task': task})
                if failure == 'symlink-report':
                    outside = root/'outside.json'; outside.write_bytes((run_dir/'worker-report.json').read_bytes())
                    (run_dir/'worker-report.json').unlink(); (run_dir/'worker-report.json').symlink_to(outside)
                if failure == 'stale-api': task['generation'] = 1
                if failure == 'unknown-note': note['unknown_operations'] = ['unknown']
                np = root/'note.json'; adapter.save(np, note)
                before = {p.name: p.read_bytes() for p in run_dir.iterdir()}
                service = Mock(); service.config = {'worker': worker}; service.request.return_value = task
                if failure == 'changed-during-api':
                    def change_evidence(path):
                        (run_dir/'worker-report.json').write_bytes(before['worker-report.json'] + b'\n')
                        return task
                    service.request.side_effect = change_evidence
                patches = [patch.object(adapter, 'ROOT', root), patch.object(adapter, 'CONFIG', config),
                           patch.object(adapter.socket, 'gethostname', return_value='fixture'),
                           patch.object(adapter, 'Client', return_value=service),
                           patch.object(adapter, 'spawn'), patch.object(adapter, 'rpc_account'),
                           patch.object(adapter.subprocess, 'Popen'),
                           patch('sys.argv', ['nova-cli-worker', 'reconcile-preclaim', '--run-id', run['run_id'], '--note-file', str(np)])]
                with contextlib.ExitStack() as stack:
                    mocks = [stack.enter_context(p) for p in patches]
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    if failure == 'busy': stack.enter_context(patch.object(adapter.fcntl, 'flock', side_effect=BlockingIOError))
                    if failure:
                        with self.assertRaises(ValueError): adapter.main()
                    else: adapter.main()
                for mock in mocks[4:7]: mock.assert_not_called()
                for call in service.request.call_args_list:
                    self.assertEqual(('/api/tasks/' + task['id'],), call.args)
                if failure:
                    self.assertEqual(state_bytes, (run_dir/'state.json').read_bytes())
                    self.assertFalse((run_dir/'preclaim-reconciliation.json').exists())
                    self.assertFalse((run_dir/'preclaim-state-before.json').exists())
                else:
                    self.assertEqual(state_bytes, (run_dir/'preclaim-state-before.json').read_bytes())
                    audit = json.loads((run_dir/'preclaim-reconciliation.json').read_text())
                    self.assertEqual(run, audit['prior_state'])
                    self.assertEqual(hashlib.sha256(state_bytes).hexdigest(), audit['original_state_sha256'])
                    self.assertEqual('preclaim_readiness_stop', audit['operation'])
                    self.assertEqual({**run, 'status': 'settled', 'prior_status': 'needs_attention'},
                                     json.loads((run_dir/'state.json').read_text()))
                    for name, data in before.items():
                        if name != 'state.json': self.assertEqual(data, (run_dir/name).read_bytes())
                    service.request.assert_called_once_with('/api/tasks/' + task['id'])
                if failure != 'finish-marker': self.assertFalse((run_dir/'finish-response.json').exists())
                self.assertFalse((run_dir/'wait-receipt.json').exists())
                self.assertFalse((run_dir/'dispatch-settlement.json').exists())


if __name__ == '__main__':
    unittest.main()
