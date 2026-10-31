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

    def test_dashboard_has_no_login_and_workers_keep_ownership(self):
        self.assertEqual(200,self.request("/api/state")[0])
        self.assertEqual(200,self.request("/api/state","oliver-mini")[0])
        self.assertEqual(403,self.request("/api/worker/claim","admin",{})[0])
        self.assertEqual(403,self.request("/api/worker/claim",body={})[0])
        code,body,_=self.request('/api/tasks',body={'request_key':'dashboard-create','title':'Browser submission'})
        self.assertEqual(200,code)
        self.assertEqual('dashboard',json.loads(body)['submitted_by'])

    def test_unknown_host_and_cross_site_fetch_rejected(self):
        self.assertEqual(403,self.request('/api/state',extra={'Host':'evil.invalid'})[0])
        self.assertEqual(403,self.request('/api/tasks',body={'title':'X'},extra={'Sec-Fetch-Site':'cross-site'})[0])

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

    def test_defer_endpoint_requires_owner_and_returns_scheduling(self):
        _,body,_=self.request('/api/tasks',body={'request_key':'waiting-http','title':'Wait'})
        ident=json.loads(body)['id']
        self.request('/api/worker/pulse','oliver-mac',{'capacity':{'memory_gb':16,'cpus':8,'disk_gb':100}})
        _,body,_=self.request('/api/worker/claim','oliver-mac',{})
        claim=json.loads(body)
        body={'claim_token':claim['claim_token'],'generation':claim['task']['generation'],
              'worker':'oliver-mini','execution_stopped':True,'external_state_checked':True,'evidence':'All operations stopped; requires mini'}
        url='/api/tasks/'+ident+'/defer'
        self.assertEqual(403,self.request(url,body=body)[0])
        self.assertEqual(409,self.request(url,'oliver-mini',body)[0])
        self.assertEqual(409,self.request(url,'admin',body)[0])
        code,raw,_=self.request(url,'oliver-mac',body)
        self.assertEqual(200,code);self.assertEqual('queued',json.loads(raw)['status'])
        self.assertEqual('oliver-mini',json.loads(raw)['next_worker'])

    def test_manual_release_requires_admin_and_repository(self):
        for actor in (None,'oliver-mini'):
            self.assertEqual(403,self.request('/api/releases/start',actor,{'repository':'oliverhu/nova-browser'})[0])
        self.assertEqual(400,self.request('/api/releases/start','admin',{})[0])
        import time
        self.request('/api/repositories/configure','admin',{'repository':'oliverhu/nova-browser','published_commit':'a'*40})
        self.request('/api/repositories/observe','oliver-mini',{'repository':'oliverhu/nova-browser','commit':'b'*40,'observed_at':time.time()})
        code,body,_=self.request('/api/releases/start','admin',{'repository':'oliverhu/nova-browser'})
        self.assertEqual(200,code);self.assertEqual(1,len(json.loads(body)['created_ids']))

    def test_repository_configuration_and_observations_require_identity(self):
        body={'repository':'owner/repo','published_commit':'a'*40}
        self.assertEqual(403,self.request('/api/repositories/configure','oliver-mini',body)[0])
        self.assertEqual(200,self.request('/api/repositories/configure','admin',body)[0])
        import time
        observation={'repository':'owner/repo','commit':'b'*40,'observed_at':time.time()}
        self.assertEqual(403,self.request('/api/repositories/observe',body=observation)[0])
        self.assertEqual(200,self.request('/api/repositories/observe','oliver-mini',observation)[0])


if __name__=="__main__":unittest.main()
