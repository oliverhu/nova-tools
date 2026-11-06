import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock

from coordinator.gateway import create_server


class WorkerStatusGatewayTests(unittest.TestCase):
    def test_dashboard_module_uses_existing_loopback_security_and_no_credential_injection(self):
        client = Mock()
        server = create_server(client, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'

        def get(path, headers=None, body=None):
            request = urllib.request.Request(url+path, headers=headers or {}, data=body)
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    return response.status, response.read(), response.headers
            except urllib.error.HTTPError as response:
                with response:
                    return response.status, response.read(), response.headers

        try:
            code, body, headers = get('/worker-status.js')
            self.assertEqual(200, code)
            self.assertIn(b'NovaWorkerStatus', body)
            self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
            self.assertIn(b'/worker-status.js', get('/')[1])
            self.assertEqual(403, get('/worker-status.js', {'Host':'evil.invalid'})[0])
            self.assertEqual(403, get('/worker-status.js', {'Origin':'https://evil.invalid'})[0])
            self.assertEqual(403, get('/worker-status.js', {'Sec-Fetch-Site':'cross-site'})[0])
            self.assertEqual(403, get('/api/worker/pulse', {'Content-Type':'application/json'}, b'{}')[0])
            self.assertEqual(404, get('/coordinator/client.py')[0])
            client.request.assert_not_called()
        finally:
            server.shutdown(); server.server_close(); thread.join()
