import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from coordinator import app_client
from coordinator.client import Client, ServiceRejected
from coordinator.server import Server
from coordinator.store import Store


class AppHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.worker = 'oliverhu-mac-studio'
        self.server = Server(('127.0.0.1', 0), Store(self.root / 'db'),
                             {'admin': 'a' * 40, self.worker: 'b' * 40}, Path(__file__).resolve().parents[1] / 'web')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.admin = self.client('admin', 'a' * 40)
        self.client1 = self.client(self.worker, 'b' * 40)
        self.session = self.root / 'session.json'
        self.policy = {'expected_revision': 0, 'enabled': True, 'source_commit': 'a' * 40, 'inventory_sha256': 'b' * 64, 'evidence': 'Tested installation'}
        self.admin.request('/api/workers/' + self.worker + '/app/policy', self.policy)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def client(self, worker, token):
        p = self.root / worker / 'client.json'; p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps({'url': self.url, 'worker': worker, 'token': token, 'build_path': str(self.root), 'disk_reserve_gb': 0, 'capabilities': ['development']}))
        with patch.dict('os.environ', {}, clear=True): return Client(p)

    def register(self):
        return app_client.register(self.client1, self.session, 'chat-one', 'host-one', 'f' * 64, 0)

    def ready(self):
        self.register()
        self.client1.request('/api/worker/app/adopt', {'binding': app_client.binding(self.client1), 'policy_revision': 1, 'source_commit': 'a' * 40, 'inventory_sha256': 'b' * 64, 'instructions_read': True})
        self.admin.request('/api/workers/' + self.worker + '/app/qualify', {'session_id': 'chat-one', 'epoch': 1, 'policy_revision': 1, 'capabilities': ['development'], 'valid_until': time.time() + 1000, 'evidence_sha256': 'e' * 64})

    def test_actual_client_http_claim_finish_and_service_handoff(self):
        self.ready()
        task = self.admin.request('/api/tasks', {'request_key': 'http-test', 'title': 'HTTP acceptance', 'requirements': {'worker': self.worker}})
        # Capacity is real test-fixture data, not host resource inspection.
        self.client1.request('/api/worker/pulse', {'capacity': {'cpus': 8, 'memory_gb': 16, 'disk_gb': 500, 'capabilities': ['development']}})
        with patch.object(self.client1, 'pulse'):
            claim = self.client1.claim(task['id'])
        self.assertIsNotNone(claim['task'])
        self.assertIn('app-claims', str(self.client1.state))
        self.assertFalse((self.client1.path.parent / 'claims' / (task['id'] + '.json')).exists())
        self.client1.heartbeat(task['id'], progress='Actual loopback acceptance')
        self.client1.finish(task['id'], {'summary': 'HTTP acceptance passed', 'execution_stopped': True}, 'completed')
        self.assertEqual('completed', self.client1.request('/api/tasks/' + task['id'])['status'])
        result = self.client1.request('/api/inbox/post', {'request_id': '1' * 32, 'recipient': 'coordinator', 'task_id': task['id'], 'generation': 1, 'kind': 'handoff', 'summary': 'Completed; inspect API receipt', 'artifact_refs': []})
        inbox = self.admin.request('/api/inbox/read', {'after': 0, 'limit': 10})
        self.assertEqual(result['id'], inbox['messages'][0]['id'])
        self.assertNotIn('app_binding', json.dumps(inbox))
        self.assertNotIn(app_client.binding(self.client1)['token'], json.dumps(inbox))

    def test_lost_registration_response_recovers_same_token_and_epoch(self):
        request = self.client1.request
        def lost(path, body=None):
            request(path, body)
            raise OSError('response lost after commit')
        with patch.object(self.client1, 'request', side_effect=lost):
            with self.assertRaises(OSError): self.register()
        before = json.loads(self.session.read_text())
        self.assertEqual('pending', before['state'])
        result = self.register(); after = json.loads(self.session.read_text())
        self.assertTrue(result['duplicate'])
        self.assertEqual(before['binding'], after['binding'])
        self.assertEqual('registered', after['state'])
        self.assertEqual(0o600, self.session.stat().st_mode & 0o777)

    def test_worker_cannot_enable_or_self_qualify_and_unregistered_cannot_claim(self):
        with self.assertRaises(ServiceRejected) as error:
            self.client1.request('/api/workers/' + self.worker + '/app/policy', self.policy)
        self.assertEqual(403, error.exception.status)
        with self.assertRaises(ServiceRejected) as error:
            self.client1.request('/api/worker/claim', {'task_id': None})
        self.assertEqual(409, error.exception.status)

    def test_session_file_does_not_overwrite_other_enrollment(self):
        self.register(); original = self.session.read_bytes()
        with self.assertRaises(ValueError):
            app_client.register(self.client1, self.session, 'different-chat', 'host-one', 'f' * 64, 0)
        self.assertEqual(original, self.session.read_bytes())


if __name__ == '__main__': unittest.main()
