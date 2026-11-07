import fcntl
import contextlib
import io
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from coordinator import cli_event_dispatch as events
from coordinator.cli_dispatch import save
from coordinator import cli_dispatch as launcher


class LocalEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root/'worker.lock').touch(mode=0o600)
        self.config = {'worker': 'oliverhu-mini-vm1', 'hostname': socket.gethostname(),
                       'model': 'selected', 'reasoning': 'medium', 'account_email_sha256': 'a'*64,
                       'admission': 'enabled'}
        source = self.root/'qualified-source.py'; source.write_text('qualified source')
        manifest = {**{k: self.config[k] for k in ('worker', 'hostname', 'model', 'reasoning', 'account_email_sha256')},
                    'schema': 1, 'files': {str(source): events.digest(source.read_bytes())}}
        path = self.root/'readiness.json'; save(path, manifest)
        self.config['event_dispatch'] = {'enabled': True, 'readiness_manifest': str(path),
                                        'readiness_revision': events.digest(path.read_bytes())}
        self.client = Mock()
        self.client.request.side_effect = lambda route, body: ({'accepted': True} if route.endswith('/report')
            else {'status': 'reserved', 'duplicate': False})

    def tearDown(self):
        self.temp.cleanup()

    def run_fixture(self):
        directory = self.root/('b'*32); directory.mkdir(mode=0o700)
        return directory, {'run_id': directory.name, 'task': 'c'*16, 'queued_generation': 0,
                           'worker': self.config['worker'], 'created_at': 10, 'status': 'starting'}

    def test_default_disabled_does_not_register_dispatch_or_start_models(self):
        directory, record = self.run_fixture()
        events.reserve_run(self.client, {**self.config, 'event_dispatch': {'enabled': False}},
                           self.root, directory, {'id': record['task'], 'generation': 0}, record)
        self.client.request.assert_not_called()
        self.assertEqual(list(directory.iterdir()), [])

    def test_actual_source_and_host_readiness_changes_refuse(self):
        self.assertEqual(events.readiness(self.config), self.config['event_dispatch']['readiness_revision'])
        (self.root/'qualified-source.py').write_text('changed')
        with self.assertRaises(ValueError): events.readiness(self.config)
        with self.assertRaises(ValueError): events.readiness({**self.config, 'hostname': 'another host'})

    def test_reservation_binds_exact_run_without_leaking_token_in_state(self):
        directory, record = self.run_fixture()
        events.reserve_run(self.client, self.config, self.root, directory,
                           {'id': record['task'], 'generation': 0}, record)
        auth = events.claim_binding(self.root, directory.name)
        state = json.loads((directory/'state.json').read_text())
        self.assertEqual(auth['wake_id'], directory.name)
        self.assertNotIn(auth['token'], str(state))
        self.assertEqual((directory/'wake-authorization.json').stat().st_mode & 0o777, 0o600)
        route, body = self.client.request.call_args.args
        self.assertTrue(route.endswith('/reserve'))
        self.assertEqual(body['token_hash'], events.digest(auth['token'].encode()))
        self.assertNotIn('token', body)

    def test_ambiguous_delivery_leaves_starting_intent_and_prevents_new_wake(self):
        directory, record = self.run_fixture()
        def unavailable(route, body):
            if route.endswith('/report'): return {'accepted': True}
            raise TimeoutError('unknown transport outcome')
        self.client.request.side_effect = unavailable
        with self.assertRaises(TimeoutError):
            events.reserve_run(self.client, self.config, self.root, directory,
                               {'id': record['task'], 'generation': 0}, record)
        self.assertTrue((directory/'wake-authorization.json').exists())
        self.assertEqual(json.loads((directory/'state.json').read_text())['status'], 'starting')
        with self.assertRaises(ValueError): events.admission_inventory(self.root)
        self.assertFalse((directory/'model-spawn-intent.json').exists())

    def settled_fixture(self):
        directory, record = self.run_fixture()
        record.update(status='settled', execution_stopped=True, unreceived_message_ids=[])
        save(directory/'state.json', record)
        documents = {n: {'original': n} for n in ('finish-response.json', 'worker-report.json', 'task-readback.json')}
        for name, value in documents.items(): save(directory/name, value)
        result = {'schema': 1, 'run_id': directory.name, 'task': record['task'], 'generation': 1,
                  'worker': self.config['worker'], 'outcome': 'blocked', 'availability': 'available',
                  'execution_stopped': True,
                  'evidence_sha256': {n: events.digest((directory/n).read_bytes()) for n in documents}}
        save(directory/'run-result.json', result)
        save(directory/'wake-authorization.json', {'wake_id': directory.name, 'run_id': directory.name, 'token': 'secret'*8})
        return directory, record

    def test_only_reaped_positive_result_and_released_lock_can_ack(self):
        directory, record = self.settled_fixture()
        with (self.root/'worker.lock').open('r') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ValueError): events.supervisor_settled(self.client, self.config, directory)
        self.client.request.assert_not_called()
        events.supervisor_settled(self.client, self.config, directory)
        route, body = self.client.request.call_args.args
        self.assertTrue(route.endswith('/settle')); self.assertTrue(body['local_lock_released'])
        self.assertEqual(body['generation'], 1)
        self.client.reset_mock()
        (directory/'worker-report.json').write_text('changed')
        with self.assertRaises(ValueError): events.supervisor_settled(self.client, self.config, directory)
        self.client.request.assert_not_called()

    def test_unread_message_and_unknown_result_preserve_reservation(self):
        directory, record = self.settled_fixture()
        record['unreceived_message_ids'] = ['unread']; save(directory/'state.json', record)
        with self.assertRaises(ValueError): events.supervisor_settled(self.client, self.config, directory)
        self.client.request.assert_not_called()

    def test_event_fallback_busy_race_returns_without_account_or_model_work(self):
        config_path = self.root/'config.json'; save(config_path, self.config)
        client = Mock(); client.config = {'worker': self.config['worker']}
        output = io.StringIO()
        with patch.object(launcher, 'CONFIG', config_path), patch.object(launcher, 'ROOT', self.root), \
             patch.object(launcher, 'Client', return_value=client), \
             patch.object(launcher.fcntl, 'flock', side_effect=BlockingIOError), \
             patch.object(launcher, 'rpc_account') as account, patch.object(launcher, 'spawn') as spawn, \
             patch('sys.argv', ['worker', 'wake', '--task', 'c'*16, '--event-triggered']), \
             contextlib.redirect_stdout(output):
            launcher.main()
        self.assertEqual(json.loads(output.getvalue()), {'disposition': 'busy', 'model_started': False})
        account.assert_not_called(); spawn.assert_not_called(); client.request.assert_not_called()

    def test_exclusive_spawn_intent_prevents_replaying_the_same_model_wake(self):
        directory, record = self.run_fixture(); record['wake_id'] = record['run_id']
        with (self.root/'worker.lock').open('r') as lock, patch.object(launcher.subprocess, 'Popen') as popen:
            launcher.spawn(record, directory, lock.fileno())
            with self.assertRaises(FileExistsError): launcher.spawn(record, directory, lock.fileno())
            self.assertEqual(popen.call_count, 1)


if __name__ == '__main__':
    unittest.main()
