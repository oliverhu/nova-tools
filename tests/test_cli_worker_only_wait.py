"""Owner and destination stay distinct in an immutable worker-only defer."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coordinator import cli_dispatch as adapter
from test_cli_wait_settlement import fixture as dependency_fixture


def fixture():
    args = list(dependency_fixture())
    run, task, response, note, worker, report, wait = args
    run['started_at'] = 2.0
    task.update(requirements={'worker': 'destination'}, dependencies=[], approval=None,
                attempts_host=worker, reassignment_count=0, attempt_exception={}, attempt_allowance={},
                steps=[{'name': 'work'}], request_key='generic-worker-handoff')
    task['events'] = [
        {'id': 10, 'at': 8.0, 'task_id': task['id'], 'actor': worker, 'kind': 'claimed'},
        {'id': 11, 'at': 9.1, 'task_id': task['id'], 'actor': worker, 'kind': 'waiting',
         'detail': json.dumps({'worker': 'destination', 'dependencies': [], 'evidence': wait['evidence']})}]
    response.clear(); response.update({k: copy.deepcopy(v) for k, v in task.items() if k not in ('receipts', 'events', 'approval')})
    report.update(external_state_checked=True, next_worker='destination', cleanup={
        'task': task['id'], 'generation': 1, 'worker': worker,
        'execution_stopped': True, 'external_state_checked': True, 'unknown_operations': []})
    wait.update(worker='destination'); del wait['dependencies']
    return [*args, copy.deepcopy(task)]


class WorkerOnlyWaitTests(unittest.TestCase):
    def test_exact_owner_destination_and_omitted_or_empty_dependencies(self):
        args = fixture(); before = copy.deepcopy(args)
        adapter.validate_settlement(*args)
        self.assertEqual(before, args)
        args[6]['dependencies'] = []
        adapter.validate_settlement(*args)
        # Existing nonempty dependency wait path remains independently valid.
        adapter.validate_settlement(*dependency_fixture())

    def test_wrong_owner_destination_unknown_effects_and_holds(self):
        cases = [
            (0, {'worker': 'destination'}), (0, {'completion_verified': True}),
            (0, {'cli_exit_code': 1}), (0, {'queued_generation': 1}), (0, {'ended_at': float('nan')}),
            (1, {'owner': 'destination'}), (1, {'last_owner': 'destination'}),
            (1, {'requirements': {'worker': 'foreign'}}), (1, {'requirements': {'worker': 'mini'}}),
            (1, {'dependencies': ['fake']}), (1, {'result': {'outcome': 'completed'}}),
            (1, {'approval': {'pending': True}}), (1, {'generation': 2}),
            (2, {'last_owner': 'foreign'}), (2, {'fix_attempts_used': 99}),
            (3, {'human_stop': True}), (3, {'approval_request': {'question': 'original'}}),
            (3, {'unknown_operations': ['remote input']}), (3, {'input_settled': False}),
            (5, {'worker': 'destination'}), (5, {'worker_id': 'destination'}),
            (5, {'next_worker': 'foreign'}), (5, {'generation': 2}),
            (5, {'acceptance_complete': True}), (5, {'external_state_checked': False}),
            (5, {'human_stop': True}), (5, {'unknown_operations': ['signing']}),
            (6, {'worker': 'mini'}), (6, {'worker': 'foreign'}), (6, {'worker_id': 'foreign'}),
            (6, {'dependencies': ['fake']}), (6, {'dependencies': None}),
            (6, {'external_state_checked': False}), (6, {'execution_stopped': False}),
            (6, {'approval_request': {'question': 'original'}}), (6, {'evidence': 'unbound'}),
            (7, {'events': []}), (7, {'requirements': {'worker': 'foreign'}}),
            (7, {'fix_attempts_used': 99})]
        for index, delta in cases:
            args = fixture(); args[index].update(delta)
            with self.subTest(index=index, delta=delta), self.assertRaises(ValueError):
                adapter.validate_settlement(*args)
        for key, value in [('execution_stopped', False), ('external_state_checked', False),
                           ('unknown_operations', ['helper']), ('worker', 'destination')]:
            args = fixture(); args[5]['cleanup'][key] = value
            with self.subTest(cleanup=key), self.assertRaises(ValueError): adapter.validate_settlement(*args)
        args = fixture(); args[7] = None
        with self.assertRaises(ValueError): adapter.validate_settlement(*args)

    def test_matching_originals_do_not_make_forged_or_later_history_safe(self):
        for target, delta in [
            ('event', {'actor': 'destination'}), ('event', {'kind': 'claimed'}),
            ('event', {'id': 9}), ('event', {'at': float('inf')}), ('event', {'at': 11.0}),
            ('event', {'detail': '{}'}), ('event', {'task_id': 'foreign'}),
            ('receipt', {'generation': 2}), ('receipt', {'at': 9.9}),
            ('body', {'execution_stopped': False}), ('body', {'summary': 'unbound'}),
            ('claim', {'actor': 'destination'}), ('claim', {'kind': 'waiting'})]:
            args = fixture(); t=args[1]
            obj={'event': t['events'][-1], 'claim':t['events'][-2],
                 'receipt': t['receipts'][-1], 'body':t['receipts'][-1]['receipt']['receipt']}[target]
            obj.update(delta); args[7]=copy.deepcopy(t)
            with self.subTest(target=target, delta=delta), self.assertRaises(ValueError): adapter.validate_settlement(*args)
        args = fixture(); args[1]['events'].append({'kind':'claimed', 'actor':'destination'})
        with self.assertRaises(ValueError): adapter.validate_settlement(*args)

    def test_public_check_and_apply_preserve_originals_and_reject_newer_state(self):
        for bad in (None, 'later-api', 'later-local', 'symlink', 'missing-readback', 'changed-input'):
            args=fixture(); run,task,response,note,worker,report,wait,readback=args
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve(); d=root/run['run_id']; d.mkdir()
                config=root/'config.json'; adapter.save(config, {'worker':worker,'hostname':'fixture'})
                for name,value in [('state.json',run),('finish-response.json',response),
                                   ('worker-report.json',report),('wait-receipt.json',wait),('task-readback.json',readback)]:
                    adapter.save(d/name,value)
                note_path=root/'note.json';adapter.save(note_path,note)
                originals={p.name:p.read_bytes() for p in d.iterdir()}
                client=Mock();client.config={'worker':worker};client.request.return_value=task
                argv=['nova-cli-worker','settle','--run-id',run['run_id'],'--note-file',str(note_path)]
                with patch.object(adapter,'ROOT',root),patch.object(adapter,'CONFIG',config), \
                     patch.object(adapter.socket,'gethostname',return_value='fixture'), \
                     patch.object(adapter,'Client',return_value=client),patch.object(adapter,'spawn') as spawn, \
                     patch.object(adapter,'rpc_account') as account,patch.object(adapter.subprocess,'Popen') as process:
                    with patch('sys.argv',argv+['--check-only']),contextlib.redirect_stdout(io.StringIO()) as output:
                        adapter.main()
                    self.assertTrue(json.loads(output.getvalue())['settlement_ready'])
                    self.assertEqual(originals,{p.name:p.read_bytes() for p in d.iterdir()})
                    if bad=='later-api':task['events'].append({'kind':'claimed','actor':'destination'})
                    if bad=='later-local':adapter.save(root/('c'*32)/'state.json',{**run,'run_id':'c'*32,'created_at':20.0})
                    if bad=='symlink':
                        q=root/'elsewhere.json';q.write_bytes((d/'wait-receipt.json').read_bytes());(d/'wait-receipt.json').unlink();(d/'wait-receipt.json').symlink_to(q)
                    if bad=='missing-readback':(d/'task-readback.json').unlink()
                    if bad=='changed-input':adapter.save(d/'wait-receipt.json',{**wait,'worker':'foreign'})
                    before={p.name:p.read_bytes() for p in d.iterdir()}
                    with patch('sys.argv',argv),contextlib.redirect_stdout(io.StringIO()):
                        if bad:
                            with self.assertRaises(ValueError):adapter.main()
                        else:adapter.main()
                    spawn.assert_not_called();account.assert_not_called();process.assert_not_called()
                self.assertTrue(all(call.args[0]=='/api/tasks/'+task['id'] for call in client.request.call_args_list))
                if bad:self.assertEqual(before,{p.name:p.read_bytes() for p in d.iterdir()})
                else:
                    self.assertEqual(originals['state.json'],(d/'worker-only-wait-state-before.json').read_bytes())
                    self.assertEqual({**run,'status':'settled','prior_status':'needs_attention'},json.loads((d/'state.json').read_text()))
                    self.assertTrue(all((d/k).read_bytes()==v for k,v in originals.items() if k!='state.json'))
                    audit=json.loads((d/'dispatch-settlement.json').read_text());self.assertEqual('worker_only_wait',audit['operation'])
                    self.assertEqual(set(originals),set(audit['original_evidence_sha256']))
