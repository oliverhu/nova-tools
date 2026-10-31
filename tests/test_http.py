import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from coordinator.server import Server
from coordinator.store import Store


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.tokens={"admin":"a"*40,"oliver-mac":"b"*40,"oliver-mini":"c"*40}
        self.server=Server(("127.0.0.1",0),Store(Path(self.temp.name)/"db"),self.tokens,Path(__file__).resolve().parents[1]/"web")
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def request(self,path,actor=None,body=None,extra=None):
        req=urllib.request.Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type":"application/json",**({"Authorization":"Bearer "+self.tokens[actor]} if actor else {}),**(extra or {})})
        try:
            with urllib.request.urlopen(req) as r:return r.status,r.read(),dict(r.headers)
        except urllib.error.HTTPError as e:return e.code,e.read(),dict(e.headers)

    def test_authentication_and_no_unauthenticated_state(self):
        self.assertEqual(401,self.request("/api/state")[0])
        self.assertEqual(200,self.request("/api/state","oliver-mini")[0])
        self.assertEqual(403,self.request("/api/worker/claim","admin",{})[0])

    def test_cross_origin_mutation_rejected(self):
        self.assertEqual(403,self.request("/api/tasks","oliver-mac",{"title":"X"},extra={"Origin":"https://evil.invalid"})[0])

    def test_health_and_dashboard_security_headers(self):
        self.assertEqual(200,self.request("/healthz")[0])
        code,body,headers=self.request("/")
        self.assertEqual(200,code);self.assertIn(b"Task queue",body)
        self.assertIn("frame-ancestors 'none'",headers["Content-Security-Policy"])
        self.assertEqual(404,self.request("/../coordinator/store.py","admin")[0])

    def test_worker_cannot_reconcile(self):
        self.assertEqual(403,self.request("/api/tasks/1234567890abcdef/reconcile","oliver-mini",{})[0])

    def test_workflow_through_http(self):
        code,body,_=self.request("/api/tasks","oliver-mac",{"request_key":"http-test","title":"HTTP task","steps":[{"name":"Check","instructions":"Verify end-to-end"}]})
        self.assertEqual(200,code);task=json.loads(body)
        self.request("/api/worker/pulse","oliver-mini",{"capacity":{"memory_gb":24,"cpus":12,"disk_gb":100}})
        code,body,_=self.request("/api/worker/claim","oliver-mini",{})
        claim=json.loads(body);self.assertEqual(task["id"],claim["task"]["id"])
        code,body,_=self.request(f"/api/tasks/{task['id']}/finish","oliver-mini",{"claim_token":claim["claim_token"],"generation":1,"step":0,"receipt":{"summary":"HTTP checks passed","execution_stopped":True}})
        self.assertEqual(200,code);self.assertEqual("completed",json.loads(body)["status"])


if __name__=="__main__":unittest.main()
