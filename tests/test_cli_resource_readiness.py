import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from coordinator import cli_dispatch as adapter
from coordinator.client import Client, LocalResourceDenied


def worker():
    return {'id': 'mini2', 'online': True, 'paused': False, 'seen': 995.0,
            'capacity': {'cpus': 5, 'memory_gb': 8, 'disk_gb': 60, 'capabilities': ['macos']}}


def client():
    c = Client.__new__(Client)
    c.config = {'worker': 'mini2', 'capacity_limits': {'cpus': 5, 'memory_gb': 8}, 'capabilities': ['macos']}
    c.recover_approvals = Mock(return_value=[])
    c.pulse = Mock(side_effect=LocalResourceDenied('sysctl denied'))
    c.request = Mock(side_effect=[{'workers': [worker()]}, {'task': None}])
    return c


class ReporterReadinessTests(unittest.TestCase):
    def test_permission_denial_uses_only_fresh_own_reporter_then_normal_service_claim(self):
        c = client()
        with patch('coordinator.client.time.time', return_value=1000.0):
            result = c.claim('a' * 16)
        self.assertEqual('fresh_existing_reporter', result['resource_readiness']['source'])
        self.assertEqual([('/api/state',), ('/api/worker/claim', {'task_id': 'a' * 16})],
                         [call.args for call in c.request.call_args_list])

    def test_stale_foreign_unknown_overstated_or_paused_resources_never_claim(self):
        cases = [('worker', {'id': 'other'}), ('worker', {'seen': 910.0}),
                 ('worker', {'seen': 1006.0}), ('worker', {'seen': float('nan')}),
                 ('worker', {'online': False}), ('worker', {'paused': True}),
                 ('capacity', {'memory_gb': 0}), ('capacity', {'memory_gb': 9}),
                 ('capacity', {'cpus': 6}), ('capacity', {'cpus': True}),
                 ('capacity', {'disk_gb': -1}), ('capacity', {'disk_gb': float('inf')}),
                 ('capacity', {'capabilities': ['signing']})]
        for target, delta in cases:
            c = client(); w = worker()
            (w if target == 'worker' else w['capacity']).update(delta)
            c.request.side_effect = [{'workers': [w]}]
            with self.subTest(target=target, delta=delta), patch('coordinator.client.time.time', return_value=1000.0):
                with self.assertRaises((ValueError, RuntimeError)): c.claim('a' * 16)
            c.request.assert_called_once_with('/api/state')
        for workers in ([], [worker(), worker()]):
            c = client(); c.request.side_effect = [{'workers': workers}]
            with self.assertRaises(RuntimeError): c.claim('a' * 16)
            c.request.assert_called_once_with('/api/state')

    def test_only_explicit_sysctl_denial_activates_fallback(self):
        for stderr, error_type in ((b'Operation not permitted', LocalResourceDenied),
                                   (b'Permission denied', LocalResourceDenied),
                                   (b'unknown oid', subprocess.CalledProcessError)):
            c = client()
            with patch('coordinator.client.sys.platform', 'darwin'), \
                 patch('coordinator.client.subprocess.check_output', side_effect=
                       subprocess.CalledProcessError(1, ['/usr/sbin/sysctl'], stderr=stderr)):
                with self.assertRaises(error_type): Client.pulse(c)
            c.request.assert_not_called()
        for error in (RuntimeError('network failure'), ValueError('bad configuration'),
                      subprocess.CalledProcessError(1, ['sysctl'], stderr=b'unknown oid')):
            c = client(); c.pulse.side_effect = error
            with self.assertRaises(type(error)): c.claim('a' * 16)
            c.request.assert_not_called()


def unclaimed_fixture():
    run = {'run_id': 'b' * 32, 'task': 'a' * 16, 'worker': 'mini2', 'created_at': 1.0,
           'queued_generation': 0, 'status': 'needs_attention', 'cli_exit_code': 0,
           'ended_at': 10.0, 'completion_verified': False}
    task = {'id': run['task'], 'generation': 0, 'step': 0, 'status': 'queued', 'owner': None,
            'last_owner': None, 'requirements': {'worker': 'mini2'}, 'dependencies': [],
            'result': {}, 'receipts': [], 'approval': None, 'fix_attempts_used': 0,
            'total_fix_attempts': 0, 'attempt_base': 0, 'reassignment_count': 0,
            'events': [{'kind': 'submitted', 'task_id': run['task']}]}
    marker = {'task_id': task['id'], 'generation': 0, 'worker': 'mini2', 'finish_invoked': False,
              'finish_response': None, 'reason': 'Local pulse failed before service claim'}
    report = {'task_id': task['id'], 'generation': 0, 'worker': 'mini2', 'outcome': 'unclaimed_stopped',
              'api_status': 'queued', 'acceptance_complete': False,
              'claim_attempt': {'exit_code': 1, 'service_refusal': False, 'rerun': False},
              'cleanup': {'execution_stopped': True, 'external_state_checked': True,
                          'owned_app_or_simulator_operations_started': False, 'source_changes': False,
                          'owned_processes': 'All short inventory and failed claim commands exited'}}
    note = {'run_id': run['run_id'], 'task': task['id'], 'generation': 0, 'worker': 'mini2',
            'execution_stopped': True, 'external_state_checked': True, 'input_settled': True,
            'unknown_operations': [], 'cleanup_evidence': report['cleanup']['owned_processes']}
    return run, task, marker, note, 'mini2', report, None, copy.deepcopy(task)


class UnclaimedSettlementTests(unittest.TestCase):
    def test_never_claimed_positive_stop_is_not_service_completion(self):
        args = unclaimed_fixture(); before = copy.deepcopy(args)
        adapter.validate_settlement(*args)
        self.assertEqual(before, args)
        for target, delta in [
            (0, {'ended_at': None}), (0, {'completion_verified': True}),
            (0, {'queued_generation': 1}), (1, {'generation': 1}), (1, {'owner': 'mini2'}),
            (1, {'last_owner': 'mini2'}), (1, {'result': {'execution_stopped': True}}),
            (1, {'receipts': [{}]}), (1, {'events': [{'kind': 'claimed'}]}),
            (1, {'fix_attempts_used': 1}), (1, {'requirements': {'worker': 'other'}}),
            (2, {'finish_response': {}}), (2, {'worker': 'other'}),
            (3, {'unknown_operations': ['unknown effect']}), (5, {'outcome': 'completed'}),
            (5, {'acceptance_complete': True}), (7, {'id': 'foreign'})]:
            candidate = list(copy.deepcopy(unclaimed_fixture())); candidate[target].update(delta)
            with self.subTest(target=target, delta=delta), self.assertRaises(ValueError):
                adapter.validate_settlement(*candidate)
        for target, delta in [('cleanup', {'owned_app_or_simulator_operations_started': True}),
                              ('cleanup', {'source_changes': True}), ('cleanup', {'execution_stopped': False}),
                              ('cleanup', {'unknown_operations': ['input']}),
                              ('claim_attempt', {'service_refusal': True}), ('claim_attempt', {'rerun': True})]:
            args = list(copy.deepcopy(unclaimed_fixture())); args[5][target].update(delta)
            with self.subTest(target=target, delta=delta), self.assertRaises(ValueError):
                adapter.validate_settlement(*args)

    def test_command_retains_truthful_no_finish_marker_and_all_original_evidence(self):
        for bad in (None, 'missing-readback', 'changed-history', 'runtime-started'):
            args = unclaimed_fixture(); run, task, marker, note, worker_id, report, _, readback = args
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); run_dir = root/run['run_id']; run_dir.mkdir()
                config = root/'config.json'; adapter.save(config, {'worker': worker_id, 'hostname': 'fixture'})
                adapter.save(run_dir/'state.json', run); adapter.save(run_dir/'finish-response.json', marker)
                if bad == 'runtime-started': report['cleanup']['owned_app_or_simulator_operations_started'] = True
                adapter.save(run_dir/'worker-report.json', report)
                if bad != 'missing-readback': adapter.save(run_dir/'task-readback.json', readback)
                if bad == 'changed-history': task['events'].append({'kind': 'claimed'})
                note_path = root/'note.json'; adapter.save(note_path, note)
                originals = {p.name: p.read_bytes() for p in run_dir.iterdir()}
                c = Mock(); c.config = {'worker': worker_id}; c.request.return_value = task
                with patch.object(adapter, 'ROOT', root), patch.object(adapter, 'CONFIG', config), \
                     patch.object(adapter.socket, 'gethostname', return_value='fixture'), \
                     patch.object(adapter, 'Client', return_value=c), patch.object(adapter, 'spawn') as spawn, \
                     patch.object(adapter, 'rpc_account') as account, patch.object(adapter.subprocess, 'Popen') as process, \
                     patch('sys.argv', ['nova-cli-worker', 'settle', '--run-id', run['run_id'], '--note-file', str(note_path)]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if bad:
                        with self.assertRaises(ValueError): adapter.main()
                    else: adapter.main()
                c.request.assert_called_once_with('/api/tasks/' + task['id'])
                spawn.assert_not_called(); account.assert_not_called(); process.assert_not_called()
                for name, data in originals.items():
                    if name != 'state.json' or bad: self.assertEqual(data, (run_dir/name).read_bytes())
                if bad: self.assertFalse((run_dir/'dispatch-settlement.json').exists())
                else:
                    after = json.loads((run_dir/'state.json').read_text())
                    self.assertEqual({**run, 'status': 'settled', 'prior_status': 'needs_attention'}, after)
                    self.assertFalse(after['completion_verified'])


if __name__ == '__main__':
    unittest.main()
