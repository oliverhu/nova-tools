import sys
import time
import unittest
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from coordinator.client import Client,supervised,publish_permit,observe_main


class HeartbeatClient:
    def __init__(self,fail_after=999):self.calls=0;self.fail_after=fail_after
    def heartbeat(self,ident,progress=None):
        self.calls+=1
        if self.calls>self.fail_after:raise RuntimeError('offline')


class ClientTests(unittest.TestCase):
    def test_finish_cli_omission_preserves_receipt_choice_and_admin_cli_does_not_claim(self):
        from coordinator.client import main
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'receipt.json'
            path.write_text(json.dumps({'outcome': 'blocked', 'acceptance_complete': False}))
            with patch('coordinator.client.Client') as constructor, patch('sys.stdout', new_callable=io.StringIO):
                client = constructor.return_value
                client.finish.return_value = {'status': 'blocked'}
                with patch.object(sys, 'argv', ['nova-coord', 'finish', 'task', '--receipt', str(path)]): main()
                self.assertEqual(None, client.finish.call_args.args[2])
                client.request.return_value = {'task': {'status': 'blocked'}}
                with patch.object(sys, 'argv', ['nova-coord', 'correct-outcome', 'task', '--receipt', str(path)]): main()
                self.assertEqual('/api/tasks/task/correct-outcome', client.request.call_args.args[0])
                client.claim.assert_not_called()

    def test_finish_uses_explicit_receipt_outcome_and_rejects_mismatch_before_request(self):
        client = Client.__new__(Client)
        client.claim_body = lambda ident: {'generation': 3, 'step': 0, 'claim_token': 'private-test-token'}
        with patch.object(client, 'request', return_value={'status': 'blocked'}) as request:
            receipt = {'outcome': 'blocked', 'acceptance_complete': False}
            client.finish('task', receipt)
            self.assertEqual('blocked', request.call_args.args[1]['outcome'])
            request.reset_mock()
            for evidence, outcome in ((receipt, 'completed'), ({'acceptance_complete': False}, None)):
                with self.assertRaises(ValueError): client.finish('task', evidence, outcome)
            request.assert_not_called()
            client.finish('task', {'summary': 'Legacy completion'})
            self.assertEqual('completed', request.call_args.args[1]['outcome'])

    def test_supervisor_returns_command_exit_status(self):
        self.assertEqual(7,supervised(HeartbeatClient(),'test',[sys.executable,'-c','raise SystemExit(7)']))

    def test_heartbeat_failure_terminates_owned_process(self):
        client=HeartbeatClient(fail_after=1)
        started=time.monotonic()
        with self.assertRaisesRegex(RuntimeError,'Lost coordination'):
            supervised(client,'test',[sys.executable,'-c','import time; time.sleep(30)'],heartbeat_interval=.05)
        self.assertLess(time.monotonic()-started,3)

    def test_initial_lease_failure_prevents_start(self):
        with self.assertRaisesRegex(RuntimeError,'offline'):
            supervised(HeartbeatClient(fail_after=0),'test',['/does-not-exist'])

    def test_normal_work_cannot_obtain_publish_permit(self):
        class Client:
            config={'worker':'oliver-mac'}
            def claim_body(self,ident):return {'generation':1}
            def request(self,path):
                return {'status':'running','owner':'oliver-mac','generation':1,'current_step':{'kind':'work'}} if '/tasks/' in path else {'locks':[]}
        with self.assertRaisesRegex(RuntimeError,'hourly publish'):
            publish_permit(Client(),'x')

    def test_publish_permit_needs_no_global_lock(self):
        class Client:
            config={'worker':'oliver-mini'}
            def claim_body(self,ident):return {'generation':3}
            def request(self,path):
                assert path=='/api/tasks/x'
                return {'status':'running','owner':'oliver-mini','generation':3,'current_step':{'kind':'publish'}}
            def heartbeat(self,*args):pass
        self.assertFalse(publish_permit(Client(),'x')['exclusive'])

    def test_remote_observation_is_exact_and_failure_is_not_reported(self):
        class Client:
            config={'repository':{'name':'owner/repo','path':'/checkout'}}
            def request(self,path,body):self.body=body;return body
        c=Client()
        with patch('coordinator.client.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='b'*40+'\trefs/heads/main\n')) as run:
            self.assertEqual('b'*40,observe_main(c)['commit'])
            self.assertIn('ls-remote',run.call_args.args[0])
        c.body=None
        with patch('coordinator.client.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout='')):
            with self.assertRaises(RuntimeError):observe_main(c)
        self.assertIsNone(c.body)


    def test_reporter_reserves_guest_capacity_without_inflating_hardware(self):
        from coordinator.client import Client
        c = Client.__new__(Client)
        c.config = {'capacity_limits': {'cpus': 14, 'memory_gb': 48}}
        c.request = lambda path, body: body['capacity']
        with patch('coordinator.client.sys.platform', 'darwin'), \
             patch('coordinator.client.subprocess.check_output', return_value=str(96 * 1024**3).encode()), \
             patch('coordinator.client.os.cpu_count', return_value=30), \
             patch('coordinator.client.shutil.disk_usage', return_value=SimpleNamespace(free=500 * 1024**3)):
            capacity = c.pulse()
            self.assertEqual(14, capacity['cpus'])
            self.assertEqual(48, capacity['memory_gb'])
            c.config['capacity_limits'] = {'cpus': 100, 'memory_gb': 200}
            capacity = c.pulse()
            self.assertEqual(30, capacity['cpus'])
            self.assertEqual(96, capacity['memory_gb'])


if __name__=='__main__':unittest.main()
