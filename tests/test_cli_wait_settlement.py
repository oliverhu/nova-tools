"""A deferred queue task can release local capacity only with owning wait proof."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coordinator import cli_dispatch as adapter


def fixture():
    ident = 'a' * 16; run_id = 'b' * 32; worker = 'mini'
    evidence = 'All owned commands reaped; no GUI, server, device or unknown effect remains'
    run = {'run_id': run_id, 'task': ident, 'worker': worker, 'status': 'needs_attention',
           'queued_generation': 0, 'created_at': 1.0, 'cli_exit_code': 0, 'ended_at': 10.0,
           'completion_verified': False}
    wait = {'task': ident, 'generation': 1, 'worker': worker, 'outcome': 'queued',
            'dependencies': ['c' * 16], 'acceptance_complete': False,
            'execution_stopped': True, 'external_state_checked': True, 'evidence': evidence}
    report = {'task': ident, 'generation': 1, 'worker': worker, 'outcome': 'queued',
              'acceptance_complete': False, 'execution_stopped': True,
              'settlement_operation': 'nova-coord wait (defer), not finish/completed'}
    receipt = {'task_id': ident, 'generation': 1, 'step': 0, 'at': 9.0,
               'receipt': {'outcome': 'waiting', 'receipt': {'summary': evidence,
                   'execution_stopped': True, 'external_state_checked': True}}}
    task = {'id': ident, 'generation': 1, 'step': 0, 'status': 'queued', 'owner': None,
            'last_owner': worker, 'requirements': {'worker': worker}, 'dependencies': wait['dependencies'][:],
            'result': {}, 'fix_attempts_used': 12, 'total_fix_attempts': 17, 'attempt_base': 5,
            'receipts': [receipt], 'events': [{'task_id': ident, 'actor': worker, 'kind': 'waiting', 'at': 9.1,
                'detail': json.dumps({'worker': worker, 'dependencies': wait['dependencies'], 'evidence': evidence})}]}
    response = {key: copy.deepcopy(value) for key, value in task.items() if key not in ('receipts', 'events')}
    note = {'run_id': run_id, 'task': ident, 'generation': 1, 'worker': worker,
            'execution_stopped': True, 'external_state_checked': True, 'input_settled': True,
            'unknown_operations': [], 'cleanup_evidence': evidence}
    return run, task, response, note, worker, report, wait


class QueuedWaitSettlementTests(unittest.TestCase):
    def test_owning_defer_with_empty_result_is_not_a_completion_or_service_mutation(self):
        args = fixture(); before = copy.deepcopy(args)
        adapter.validate_settlement(*args)
        self.assertEqual(before, args)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter.save(root/'finish-response.json', args[2])
            adapter.save(root/'worker-report.json', args[5])
            self.assertFalse(adapter.verify_completion(args[1], root))

    def test_queued_or_process_exit_alone_cannot_settle(self):
        args = fixture()
        for report, wait in ((None, None), (None, args[6]), (args[5], None)):
            with self.assertRaises(ValueError):
                adapter.validate_settlement(*args[:5], report, wait)
        for index, delta in [
            (0, {'task': 'foreign'}), (0, {'worker': 'other'}), (0, {'cli_exit_code': None}),
            (0, {'ended_at': float('nan')}), (0, {'ended_at': float('inf')}),
            (0, {'status': 'running'}), (1, {'owner': 'mini'}), (1, {'last_owner': 'other'}),
            (1, {'dependencies': []}), (1, {'receipts': []}), (1, {'events': []}),
            (2, {'generation': 2}), (2, {'step': 1}), (2, {'owner': 'other'}),
            (2, {'status': 'completed'}), (2, {'dependencies': ['other']}),
            (3, {'unknown_operations': ['unresolved input']}), (3, {'input_settled': False}),
            (3, {'generation': 2}), (3, {'external_state_checked': False}),
            (5, {'task': 'foreign'}), (5, {'worker': 'other'}), (5, {'generation': 2}),
            (5, {'outcome': 'completed'}), (5, {'acceptance_complete': True}),
            (5, {'execution_stopped': False}), (5, {'unsettled_operations': ['server']}),
            (6, {'external_state_checked': False}), (6, {'execution_stopped': False}),
            (6, {'evidence': 'unbound claim'}), (6, {'dependencies': ['foreign']}),
            (6, {'unknown_operations': ['unknown remote result']})]:
            candidate = list(copy.deepcopy(fixture())); candidate[index].update(delta)
            with self.subTest(index=index, delta=delta), self.assertRaises(ValueError):
                adapter.validate_settlement(*candidate)

    def test_fresh_waiting_receipt_and_owning_audit_must_match(self):
        cases = [
            ('receipt', {'task_id': 'foreign'}), ('receipt', {'generation': 2}),
            ('receipt', {'step': 1}), ('envelope', {'outcome': 'completed'}),
            ('body', {'execution_stopped': False}), ('body', {'external_state_checked': False}),
            ('body', {'summary': 'different wait'}), ('event', {'actor': 'other'}),
            ('event', {'kind': 'reconciled'}), ('event', {'task_id': 'foreign'}),
            ('event', {'at': 8.0}), ('event', {'detail': 'not-json'}),
            ('detail', {'dependencies': ['foreign']}), ('detail', {'worker': 'other'}),
            ('detail', {'evidence': 'different wait'})]
        for target, delta in cases:
            args = list(copy.deepcopy(fixture())); task = args[1]
            receipt = task['receipts'][0]; event = task['events'][0]
            if target == 'receipt': receipt.update(delta)
            elif target == 'envelope': receipt['receipt'].update(delta)
            elif target == 'body': receipt['receipt']['receipt'].update(delta)
            elif target == 'event': event.update(delta)
            else:
                detail = json.loads(event['detail']); detail.update(delta); event['detail'] = json.dumps(detail)
            with self.subTest(target=target, delta=delta), self.assertRaises(ValueError):
                adapter.validate_settlement(*args)

    def test_command_updates_only_local_admission_and_preserves_original_evidence(self):
        for bad in (None, 'missing-wait', 'unknown-effect', 'stale-api', 'symlink-response'):
            args = fixture(); run, task, response, note, worker, report, wait = args
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); run_dir = root/run['run_id']; run_dir.mkdir()
                config = root/'config.json'; adapter.save(config, {'worker': worker, 'hostname': 'fixture'})
                state_path = run_dir/'state.json'; adapter.save(state_path, run)
                adapter.save(run_dir/'finish-response.json', response)
                adapter.save(run_dir/'worker-report.json', report)
                adapter.save(run_dir/'wait-receipt.json', wait)
                if bad == 'missing-wait': (run_dir/'wait-receipt.json').unlink()
                if bad == 'unknown-effect': note['unknown_operations'] = ['remote operation']
                if bad == 'stale-api': task['generation'] = 2
                if bad == 'symlink-response':
                    external = root/'foreign-response.json'; adapter.save(external, response)
                    (run_dir/'finish-response.json').unlink()
                    (run_dir/'finish-response.json').symlink_to(external)
                note_path = root/'note.json'; adapter.save(note_path, note)
                originals = {p.name: p.read_bytes() for p in run_dir.iterdir() if p.is_file()}
                client = Mock(); client.config = {'worker': worker}; client.request.return_value = task
                with patch.object(adapter, 'ROOT', root), patch.object(adapter, 'CONFIG', config), \
                     patch.object(adapter.socket, 'gethostname', return_value='fixture'), \
                     patch.object(adapter, 'Client', return_value=client), patch.object(adapter, 'spawn') as spawn, \
                     patch.object(adapter, 'rpc_account') as account, patch.object(adapter.subprocess, 'Popen') as process, \
                     patch('sys.argv', ['nova-cli-worker', 'settle', '--run-id', run['run_id'], '--note-file', str(note_path)]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if bad:
                        with self.assertRaises(ValueError): adapter.main()
                    else:
                        adapter.main()
                client.request.assert_called_once_with('/api/tasks/' + task['id'])
                spawn.assert_not_called(); account.assert_not_called(); process.assert_not_called()
                for name, data in originals.items():
                    if name != 'state.json' or bad:
                        self.assertEqual(data, (run_dir/name).read_bytes())
                if bad:
                    self.assertFalse((run_dir/'dispatch-settlement.json').exists())
                else:
                    after = json.loads(state_path.read_text())
                    self.assertEqual({**run, 'status': 'settled', 'prior_status': 'needs_attention'}, after)
                    self.assertFalse(after['completion_verified'])
                    self.assertEqual(note['unknown_operations'], json.loads((run_dir/'dispatch-settlement.json').read_text())['unknown_operations'])


if __name__ == '__main__':
    unittest.main()
