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

    def test_bounded_attempt_exception_authenticated_and_single_use_over_http(self):
        self.request('/api/worker/pulse', 'oliver-mini', {'capacity': {'cpus': 12}})
        _, raw, _ = self.request('/api/tasks', 'oliver-mini', {
            'request_key': 'extra-attempt-http', 'title': 'One explicit extra attempt',
            'requirements': {'worker': 'oliver-mini'}})
        ident = json.loads(raw)['id']
        _, raw, _ = self.request('/api/worker/claim', 'oliver-mini', {'task_id': ident})
        claim = json.loads(raw)
        self.request('/api/tasks/' + ident + '/finish', 'oliver-mini', {
            'claim_token': claim['claim_token'], 'generation': 1, 'step': 0,
            'outcome': 'pending_external_approval', 'receipt': {
                'summary': 'Ten attempts settled', 'execution_stopped': True, 'fix_attempts_used': 10}})
        source = {'thread_id': 'main', 'question_id': 'one-extra', 'message_id': 'actual-yes'}
        _, raw, _ = self.request('/api/approvals/apply', 'oliver-mini', {
            'source': source, 'decision': 'approved', 'decision_verified': True,
            'response': 'Approve attempt 11', 'scope': 'One attempt on Mini', 'tasks': [{
                'id': ident, 'generation': 1, 'claim_token': claim['claim_token'],
                'execution_stopped': True, 'external_state_checked': True, 'scope_evidence': 'Actual human permits attempt 11'}]})
        approval = json.loads(raw)
        body = {'approval_id': approval['approval_id'], 'source': source, 'task': ident, 'generation': 1,
                'worker': 'oliver-mini', 'expected_used': 10, 'attempt_number': 11,
                'scope_evidence': 'Verified actual human exception', 'exception_verified': True,
                'execution_stopped': True, 'external_state_checked': True}
        url = '/api/attempt-exceptions/apply'
        self.assertEqual(403, self.request(url, body=body)[0])
        self.assertEqual(409, self.request(url, 'oliver-mac', body)[0])
        self.assertEqual(400, self.request(url, 'oliver-mini', {**body, 'attempt_number': 12})[0])
        code, raw, _ = self.request(url, 'oliver-mini', body)
        self.assertEqual(200, code); self.assertTrue(json.loads(raw)['task']['ready'])
        _, raw, _ = self.request('/api/worker/claim', 'oliver-mini', {'task_id': ident})
        current = json.loads(raw)
        self.assertEqual(11, current['task']['fix_attempts_used'])
        self.assertEqual(2, current['task']['generation'])
        code, raw, _ = self.request(url, 'oliver-mini', body)
        self.assertEqual(200, code); self.assertTrue(json.loads(raw)['replayed'])
        self.assertEqual('running', json.loads(raw)['task']['status'])
        self.assertNotIn(claim['claim_token'].encode(), raw)
        self.assertNotIn(current['claim_token'].encode(), raw)

    def test_admin_can_cancel_queued_task_through_reconcile(self):
        _,raw,_=self.request('/api/tasks',body={'request_key':'cancel-http','title':'Resolved in 0.2.21'})
        ident=json.loads(raw)['id'];url='/api/tasks/'+ident+'/reconcile'
        body={'action':'cancel','generation':0,'execution_stopped':True,
              'external_state_checked':True,'evidence':'User confirms resolved in 0.2.21'}
        for actor in (None,'oliver-mac','oliver-mini'):
            self.assertEqual(403,self.request(url,actor,body)[0])
        self.assertEqual(409,self.request(url,'admin',{**body,'generation':1})[0])
        code,raw,_=self.request(url,'admin',body)
        self.assertEqual(200,code);self.assertEqual('cancelled',json.loads(raw)['status'])
        _,raw,_=self.request('/api/tasks/'+ident)
        detail=json.loads(raw)
        self.assertEqual(0,detail['generation']);self.assertEqual([],detail['receipts'])
        self.assertEqual(body['evidence'],detail['progress'])

    def test_workflow_through_http(self):
        code,body,_=self.request("/api/tasks","oliver-mac",{"request_key":"http-test","title":"HTTP task","steps":[{"name":"Check","instructions":"Verify end-to-end"}]})
        self.assertEqual(200,code);task=json.loads(body)
        self.request("/api/worker/pulse","oliver-mini",{"capacity":{"memory_gb":24,"cpus":12,"disk_gb":100}})
        code,body,_=self.request("/api/worker/claim","oliver-mini",{})
        claim=json.loads(body);self.assertEqual(task["id"],claim["task"]["id"])
        code,body,_=self.request(f"/api/tasks/{task['id']}/finish","oliver-mini",{"claim_token":claim["claim_token"],"generation":1,"step":0,"receipt":{"summary":"HTTP checks passed","execution_stopped":True}})
        self.assertEqual(200,code);self.assertEqual("completed",json.loads(body)["status"])

    def test_pending_external_approval_finish_and_classification_over_http(self):
        _,body,_=self.request('/api/tasks',body={'request_key':'approval-http','title':'Apple approval'})
        ident=json.loads(body)['id']
        self.request('/api/worker/pulse','oliver-mac',{'capacity':{'cpus':8}})
        _,body,_=self.request('/api/worker/claim','oliver-mac',{})
        claim=json.loads(body)
        finish={'claim_token':claim['claim_token'],'generation':1,'step':0,
                'outcome':'pending_external_approval','receipt':{'summary':'Awaiting Apple','execution_stopped':True}}
        code,raw,_=self.request('/api/tasks/'+ident+'/finish','oliver-mac',finish)
        self.assertEqual(200,code);self.assertEqual('pending_external_approval',json.loads(raw)['status'])
        resume={'action':'retry','generation':1,'execution_stopped':True,'external_state_checked':True,'evidence':'Approval inspected'}
        url='/api/tasks/'+ident+'/reconcile'
        self.assertEqual(403,self.request(url,'oliver-mini',resume)[0])
        self.assertEqual(400,self.request(url,'admin',resume)[0])
        self.assertEqual(200,self.request(url,'admin',{**resume,'approval_received':True})[0])

    def test_batch_approval_authenticated_atomic_and_read_back_over_http(self):
        self.request('/api/worker/pulse','oliver-mini',{'capacity':{'cpus':12}})
        covered=[]
        for name in ('hover','modifier'):
            _,raw,_=self.request('/api/tasks',body={'request_key':name,'title':name})
            ident=json.loads(raw)['id'];_,raw,_=self.request('/api/worker/claim','oliver-mini',{'task_id':ident})
            claim=json.loads(raw)
            self.request('/api/tasks/'+ident+'/finish','oliver-mini',{'claim_token':claim['claim_token'],
                'generation':1,'step':0,'outcome':'pending_external_approval',
                'receipt':{'summary':'Choose tool','execution_stopped':True,
                           'approval_request':{'thread_id':'thread','question_id':'question'}}})
            covered.append({'id':ident,'generation':1,'claim_token':claim['claim_token'],
                'scope_evidence':'Explicitly covered '+name,'execution_stopped':True,'external_state_checked':True})
        body={'source':{'thread_id':'thread','question_id':'question','message_id':'human-message'},
            'decision':'approved','decision_verified':True,'response':'Use installed Peekaboo',
            'scope':'Isolated native tests only','tasks':covered}
        self.assertEqual(403,self.request('/api/approvals/apply',body=body)[0])
        self.assertEqual(409,self.request('/api/approvals/apply','oliver-mac',body)[0])
        code,raw,_=self.request('/api/approvals/apply','oliver-mini',body)
        self.assertEqual(200,code);result=json.loads(raw);ident=result['approval_id']
        self.assertEqual(['queued','queued'],[t['status'] for t in result['tasks']])
        _,raw,_=self.request('/api/approvals/apply','oliver-mini',body)
        self.assertTrue(json.loads(raw)['replayed'])
        self.assertEqual(403,self.request('/api/approvals/'+ident)[0])
        code,raw,_=self.request('/api/approvals/'+ident,'oliver-mini')
        self.assertEqual(200,code);self.assertTrue(json.loads(raw)['recorded'])
        _,raw,_=self.request('/api/state')
        state=json.loads(raw);self.assertEqual(1,state['capabilities']['approval_receipts'])
        self.assertTrue(all(t['approval']['state']=='approved' for t in state['tasks']))

    def test_worker_approval_response_uses_existing_retry_endpoint(self):
        _,raw,_=self.request('/api/tasks',body={'request_key':'approval-resume','title':'Consent'})
        ident=json.loads(raw)['id'];self.request('/api/worker/pulse','oliver-mini',{'capacity':{'cpus':12}})
        _,raw,_=self.request('/api/worker/claim','oliver-mini',{})
        claim=json.loads(raw);identity={'claim_token':claim['claim_token'],'generation':1,'step':0}
        self.request('/api/tasks/'+ident+'/finish','oliver-mini',{**identity,'outcome':'pending_external_approval',
            'receipt':{'summary':'Waiting for explicit consent','execution_stopped':True}})
        body={**identity,'execution_stopped':True,'external_state_checked':True,'evidence':'User response verified',
              'approval_received':True,'approval_evidence':'Human message123: approved and resume'}
        url='/api/tasks/'+ident+'/retry'
        self.assertEqual(403,self.request(url,body=body)[0])
        self.assertEqual(409,self.request(url,'oliver-mac',body)[0])
        code,raw,_=self.request(url,'oliver-mini',body)
        self.assertEqual(200,code);self.assertEqual('queued',json.loads(raw)['status'])

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
