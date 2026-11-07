import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from coordinator.server import Server
from coordinator.store import Store
from coordinator.workers import MINI_VM_WORKERS


class EventHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.worker = MINI_VM_WORKERS[0]
        self.tokens = {'admin': 'a'*40, self.worker: 'b'*40, 'oliverhu-mac-studio': 'c'*40}
        self.server = Server(('127.0.0.1', 0), Store(Path(self.temp.name)/'db'), self.tokens,
                             Path(__file__).resolve().parents[1]/'web')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def request(self, route, actor, body):
        headers = {'Content-Type': 'application/json'}
        if actor: headers['Authorization'] = 'Bearer ' + self.tokens[actor]
        request = urllib.request.Request(self.url+route, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_read_only_dashboard_and_other_workers_cannot_operate_dispatch(self):
        for actor in (None, 'admin', 'oliverhu-mac-studio'):
            self.assertEqual(self.request('/api/worker/dispatch/next', actor, {'timeout': 0})[0], 403)
            self.assertEqual(self.request('/api/worker/dispatch/preclaim-settle', actor, {})[0], 403)
        for actor in (None, self.worker, 'oliverhu-mac-studio'):
            self.assertEqual(self.request('/api/workers/'+self.worker+'/dispatch', actor, {})[0], 403)
            self.assertEqual(self.request('/api/tasks/'+'f'*16+'/require-capabilities', actor, {})[0], 403)
        self.assertEqual(self.request('/api/worker/dispatch/next', self.worker,
                                     {'timeout': 0, 'private_prompt': 'must not upload'})[0], 400)

    def test_real_http_reservation_and_claim_require_the_same_private_token(self):
        store = self.server.store
        store.set_worker_state('admin', self.worker, {'paused': False, 'reason': 'qualified fixture'})
        store.pulse(self.worker, {'capacity': {'cpus': 5, 'memory_gb': 8, 'disk_gb': 100}})
        config = {'enabled': True, 'readiness_revision': 'a'*64, 'evidence': 'exact qualified fixture'}
        self.assertEqual(self.request('/api/workers/'+self.worker+'/dispatch', 'admin', config)[0], 200)
        report = {'readiness_revision': 'a'*64, 'sequence': 1, 'availability': 'available',
                  'latest_run_id': None, 'settlement_sha256': 'b'*64, 'issue': None}
        self.assertEqual(self.request('/api/worker/dispatch/report', self.worker, report)[0], 200)
        task = store.submit('admin', {'request_key': 'http-dispatch', 'title': 'HTTP fixture',
                                     'requirements': {'worker': self.worker}})
        self.assertEqual(self.request('/api/worker/dispatch/next', self.worker, {'timeout': 0})[1]['task_id'], task['id'])
        token = 'exact-secret-token-fixture-0000000001'
        binding = {'wake_id': 'c'*32, 'run_id': 'c'*32, 'token': token}
        reserve = {'wake_id': 'c'*32, 'run_id': 'c'*32, 'task_id': task['id'], 'queued_generation': 0,
                   'readiness_revision': 'a'*64, 'token_hash': hashlib.sha256(token.encode()).hexdigest()}
        self.assertEqual(self.request('/api/worker/dispatch/reserve', self.worker, reserve)[0], 200)
        self.assertIsNone(self.request('/api/worker/claim', self.worker, {'task_id': task['id']})[1]['task'])
        code, result = self.request('/api/worker/claim', self.worker,
                                    {'task_id': task['id'], 'dispatch_binding': binding})
        self.assertEqual(code, 200); self.assertEqual(result['task']['generation'], 1)


if __name__ == '__main__':
    unittest.main()
