"""An actual approval clears local admission without inventing a defer or replay."""
import contextlib
import copy
import fcntl
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coordinator import cli_dispatch as adapter
from coordinator.store import Store


def fixture(later_claim=False):
    worker = 'oliver-mini'
    with tempfile.TemporaryDirectory() as directory:
        now = [1000.0]
        store = Store(Path(directory)/'db', clock=lambda: now[0])
        store.pulse(worker, {'capacity': {'cpus': 12, 'memory_gb': 24}})
        task = store.submit(worker, {'request_key': 'approval-settlement', 'title': 'Isolated qualification',
                                     'requirements': {'worker': worker}})
        claim = store.claim(worker, task['id'])
        finish = store.finish(worker, task['id'], {'claim_token': claim['claim_token'],
            'generation': 1, 'step': 0, 'outcome': 'pending_external_approval', 'receipt': {
                'summary': 'Stopped at the actual actor permission boundary; no input or unknown operations',
                'acceptance_complete': False, 'execution_stopped': True, 'external_state_checked': True,
                'approval_request': {'thread_id': 'thread', 'question_id': 'question'}}})
        readback = store.detail(task['id'])
        now[0] = 1200.0
        store.apply_approval('admin', {'source': {'thread_id': 'thread', 'question_id': 'question',
            'message_id': 'message'}, 'decision': 'approved', 'decision_verified': True,
            'response': 'Accès accordé → exact actor', 'scope': 'Only this actor; fresh readiness still required',
            'tasks': [{'id': task['id'], 'generation': 1, 'execution_stopped': True,
                       'external_state_checked': True, 'scope_evidence': 'Exact pending actor and question'}]})
        if later_claim:
            store.pulse(worker, {'capacity': {'cpus': 12, 'memory_gb': 24}})
            assert store.claim(worker, task['id'])['task'] is not None
        task = store.detail(task['id'])
    run = {'run_id': 'b'*32, 'task': task['id'], 'worker': worker, 'status': 'needs_attention',
           'queued_generation': 0, 'created_at': 900.0, 'started_at': 950.0, 'ended_at': 1100.0,
           'cli_exit_code': 0, 'task_status': 'pending_external_approval', 'completion_verified': False}
    report = {'task': task['id'], 'worker': worker, 'generation': 1, 'outcome': 'pending_external_approval',
              'acceptance_complete': False, 'summary': finish['result']['summary'],
              'cleanup': {'execution_stopped': True, 'external_state_checked': True}}
    note = {'run_id': run['run_id'], 'task': task['id'], 'generation': 1, 'worker': worker,
            'execution_stopped': True, 'external_state_checked': True, 'input_settled': True,
            'unknown_operations': [], 'cleanup_evidence': 'Original stopped actor and input census checked'}
    return [run, task, finish, note, worker, report, None, readback]


class ApprovedWaitSettlementTests(unittest.TestCase):
    def test_service_generated_approval_and_semantic_event_json_preserve_inputs(self):
        args = fixture(); original = copy.deepcopy(args)
        adapter.validate_settlement(*args)
        self.assertEqual(original, args)
        # The event is JSON, not an incidental whitespace/ASCII serialization contract.
        args[1]['events'][-1]['detail'] = json.dumps(args[1]['approval']['evidence'], ensure_ascii=False, indent=2)
        adapter.validate_settlement(*args)
        self.assertEqual(args[4], args[2]['last_owner'])
        self.assertEqual(args[4], args[1]['last_owner'])

    def test_authenticated_question_link_matches_original_without_rebinding_it(self):
        import hashlib
        args = fixture()
        task = args[1]; evidence = task['approval']['evidence']
        source = evidence['source']; source['thread_id'] = 'later-coordinator'
        evidence['approval_id'] = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        original = task['result']['approval_request']
        identity = {'task': task['id'], 'generation': 1, 'original_question': original, 'source': source}
        evidence['question_link'] = {'link_id': hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'task': task['id'], 'generation': 1, 'original_question': original,
            'result_sha256': hashlib.sha256(json.dumps(task['result'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        task['events'][-1]['detail'] = json.dumps(evidence)
        adapter.validate_settlement(*args)
        evidence['question_link']['result_sha256'] = '0'*64
        task['events'][-1]['detail'] = json.dumps(evidence)
        with self.assertRaises(ValueError): adapter.validate_settlement(*args)

    def test_real_service_later_claim_cannot_settle_the_original_wait(self):
        with self.assertRaises(ValueError): adapter.validate_settlement(*fixture(later_claim=True))

    def test_missing_proof_and_contradictory_identity_cleanup_or_counters_reject(self):
        base = fixture()
        changes = [
            (0, {'run_id': 'bad'}), (0, {'task': 'foreign'}), (0, {'worker': 'other'}),
            (0, {'queued_generation': 1}), (0, {'status': 'running'}), (0, {'cli_exit_code': None}),
            (0, {'cli_exit_code': 1}), (0, {'ended_at': float('nan')}), (0, {'started_at': 1200.0}),
            (0, {'completion_verified': True}), (0, {'task_status': 'completed'}),
            (1, {'owner': base[4]}), (1, {'last_owner': 'other'}), (1, {'generation': 2}),
            (1, {'status': 'running'}), (1, {'requirements': {'worker': 'other'}}),
            (1, {'dependencies': ['foreign']}), (1, {'fix_attempts_used': 1}),
            (1, {'total_fix_attempts': 1}), (1, {'step': 1}), (1, {'receipts': []}),
            (1, {'events': []}), (1, {'approval': None}), (2, {'status': 'completed'}),
            (2, {'owner': 'other'}), (3, {'input_settled': False}),
            (3, {'unsettled_operations': ['input']}), (3, {'unknown_operations': ['server']}),
            (3, {'human_stop': True}), (5, {'task': 'foreign'}), (5, {'generation': 2}),
            (5, {'worker': 'other'}), (5, {'outcome': 'completed'}),
            (5, {'acceptance_complete': True}), (5, {'execution_stopped': False}),
            (5, {'external_state_checked': False}), (5, {'unknown_operations': ['unresolved']}),
            (5, {'cleanup': {'execution_stopped': False, 'external_state_checked': True}}),
            (7, {'status': 'queued'}), (7, {'owner': None}), (7, {'events': []}),
            (7, {'receipts': []}), (7, {'result': {}}), (7, {'approval': None})]
        for index, change in changes:
            args = copy.deepcopy(base); args[index].update(change)
            with self.subTest(index=index, change=change), self.assertRaises(ValueError):
                adapter.validate_settlement(*args)
        for index in (5, 7):
            args = copy.deepcopy(base); args[index] = None
            with self.assertRaises(ValueError): adapter.validate_settlement(*args)

    def test_approval_source_event_and_original_receipt_must_all_match(self):
        base = fixture()
        for change in ('source', 'source-hash', 'scope', 'question', 'event-detail', 'event-actor',
                       'event-generation', 'event-time', 'event-id', 'recorded-time', 'extra-transition',
                       'later-claim', 'duplicate-receipt', 'pending-actor', 'pending-time', 'receipt-body'):
            args = copy.deepcopy(base); t = args[1]; approval = t['approval']; e = t['events'][-1]
            if change == 'source': approval['evidence']['source']['message_id'] = 'different'
            elif change == 'source-hash': approval['evidence']['approval_id'] = '0'*64
            elif change == 'scope': approval['evidence']['scope'] = ''
            elif change == 'question':
                # Even consistent local copies cannot rebind the original structured question.
                for x in (t['result'], args[2]['result'], args[7]['result']):
                    x['approval_request']['question_id'] = 'different'
                t['receipts'][-1]['receipt']['receipt'] = copy.deepcopy(t['result'])
                args[7]['receipts'] = copy.deepcopy(t['receipts'])
            elif change == 'event-detail': e['detail'] = 'not-json'
            elif change == 'event-actor': e['actor'] = 'foreign'
            elif change == 'event-generation':
                d = json.loads(e['detail']); d['generation'] = 2; e['detail'] = json.dumps(d)
            elif change == 'event-time': e['at'] = 1.0
            elif change == 'event-id': e['id'] = t['events'][-2]['id']
            elif change == 'recorded-time': approval['recorded_at'] += 1
            elif change == 'extra-transition': t['events'].append({**e, 'kind': 'reconciled'})
            elif change == 'later-claim': t['events'].append({**e, 'kind': 'claimed'})
            elif change == 'duplicate-receipt':
                t['receipts'].append(copy.deepcopy(t['receipts'][-1])); args[7]['receipts'] = copy.deepcopy(t['receipts'])
            elif change in ('pending-actor', 'pending-time'):
                for events in (t['events'], args[7]['events']):
                    events[-2 if events is t['events'] else -1]['actor' if change == 'pending-actor' else 'at'] = 'other' if change == 'pending-actor' else 2000.0
            elif change == 'receipt-body': t['receipts'][-1]['receipt']['receipt']['execution_stopped'] = False
            with self.subTest(change=change), self.assertRaises(ValueError): adapter.validate_settlement(*args)

    def test_locked_command_preserves_bytes_and_writes_only_local_audit(self):
        for bad in (None, 'busy', 'later-run', 'missing-readback', 'symlink-finish', 'existing-audit', 'fake-wait'):
            args = fixture(); run, task, finish, note, worker, report, _, readback = args
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); run_dir = root/run['run_id']; run_dir.mkdir()
                config = root/'config.json'; adapter.save(config, {'worker': worker, 'hostname': 'fixture'})
                for name, value in [('state.json', run), ('finish-response.json', finish),
                                    ('worker-report.json', report), ('task-readback.json', readback)]:
                    adapter.save(run_dir/name, value)
                if bad == 'later-run': adapter.save(root/('c'*32)/'state.json', {**run, 'run_id': 'c'*32, 'created_at': 2000.0})
                if bad == 'missing-readback': (run_dir/'task-readback.json').unlink()
                if bad == 'symlink-finish':
                    (run_dir/'finish-response.json').rename(root/'foreign.json')
                    (run_dir/'finish-response.json').symlink_to(root/'foreign.json')
                if bad == 'existing-audit': adapter.save(run_dir/'dispatch-settlement.json', {'original': True})
                if bad == 'fake-wait': adapter.save(run_dir/'wait-receipt.json', {'invented': True})
                originals = {p.name: p.read_bytes() for p in run_dir.iterdir()}
                note_path = root/'note.json'; adapter.save(note_path, note)
                client = Mock(); client.config = {'worker': worker}; client.request.return_value = task
                with (root/'worker.lock').open('a+') as lock, \
                     patch.object(adapter, 'ROOT', root), patch.object(adapter, 'CONFIG', config), \
                     patch.object(adapter.socket, 'gethostname', return_value='fixture'), \
                     patch.object(adapter, 'Client', return_value=client), patch.object(adapter, 'spawn') as spawn, \
                     patch.object(adapter, 'rpc_account') as account, patch.object(adapter.subprocess, 'Popen') as process, \
                     patch('sys.argv', ['nova-cli-worker', 'settle', '--run-id', run['run_id'], '--note-file', str(note_path)]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if bad == 'busy': fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if bad:
                        with self.assertRaises(ValueError): adapter.main()
                    else: adapter.main()
                spawn.assert_not_called(); account.assert_not_called(); process.assert_not_called()
                for name, raw in originals.items():
                    if bad or name != 'state.json': self.assertEqual(raw, (run_dir/name).read_bytes())
                if not bad:
                    client.request.assert_called_once_with('/api/tasks/'+task['id'])
                    self.assertEqual(originals['state.json'], (run_dir/'approved-wait-state-before.json').read_bytes())
                    audit = json.loads((run_dir/'dispatch-settlement.json').read_text())
                    self.assertEqual('approved_wait', audit['operation'])
                    self.assertEqual({k: hashlib.sha256(v).hexdigest() for k, v in originals.items()}, audit['original_evidence_sha256'])
                    after = json.loads((run_dir/'state.json').read_text())
                    self.assertEqual({**run, 'status': 'settled', 'prior_status': 'needs_attention'}, after)
                    self.assertFalse(after['completion_verified'])
                    self.assertFalse((run_dir/'wait-receipt.json').exists())
                    self.assertFalse(adapter.verify_completion(task, run_dir))

    def test_changed_evidence_before_audit_is_refused_without_writing(self):
        run, task, finish, note, worker, report, _, readback = fixture()
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory).resolve()
            for name, value in [('state.json', run), ('finish-response.json', finish),
                                ('worker-report.json', report), ('task-readback.json', readback)]:
                adapter.save(run_dir/name, value)
            adapter.save(run_dir/'worker-report.json', {**report, 'unknown_operations': ['changed after validation']})
            with self.assertRaises(ValueError):
                adapter.settle_approved_wait(run_dir, run, note, finish, report, readback)
            self.assertEqual(run, json.loads((run_dir/'state.json').read_text()))
            self.assertFalse((run_dir/'approved-wait-state-before.json').exists())
            self.assertFalse((run_dir/'dispatch-settlement.json').exists())


if __name__ == '__main__':
    unittest.main()
