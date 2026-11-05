import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch
import hashlib
from coordinator.server import Server
from coordinator.store import Store
from coordinator.store import encoded


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

    def test_finish_contradiction_rejected_and_outcome_correction_is_admin_only(self):
        self.request('/api/worker/pulse', 'oliver-mini', {'capacity': {'cpus': 12}})
        _, raw, _ = self.request('/api/tasks', 'admin', {'request_key': 'wrong-finish', 'title': 'Wrong finish'})
        ident = json.loads(raw)['id']
        _, raw, _ = self.request('/api/worker/claim', 'oliver-mini', {'task_id': ident})
        claim = json.loads(raw)
        body = {'generation': 1, 'step': 0, 'claim_token': claim['claim_token'], 'outcome': 'completed',
                'receipt': {'summary': 'Acceptance incomplete', 'outcome': 'blocked', 'acceptance_complete': False,
                            'execution_stopped': True, 'external_state_checked': True, 'fix_attempts_used': 17}}
        finish = '/api/tasks/' + ident + '/finish'
        self.assertEqual(400, self.request(finish, 'oliver-mini', body)[0])
        self.assertEqual('running', self.server.store.detail(ident)['status'])
        with patch('coordinator.store.finish_outcome', return_value='completed'):
            self.assertEqual(200, self.request(finish, 'oliver-mini', body)[0])
        detail = self.server.store.detail(ident)
        digest = lambda value: hashlib.sha256(encoded(value).encode()).hexdigest()
        correction = {'generation': 1, 'expected_status': 'completed', 'expected_step': 1,
                      'expected_result_sha256': digest(detail['result']),
                      'expected_receipt_sha256': digest(detail['receipts'][-1]['receipt']), 'outcome': 'blocked',
                      'execution_stopped': True, 'external_state_checked': True,
                      'evidence': 'Verified original receipt and erroneous completed envelope'}
        url = '/api/tasks/' + ident + '/correct-outcome'
        for actor in (None, 'oliver-mac', 'oliver-mini'):
            self.assertEqual(403, self.request(url, actor, correction)[0])
        code, raw, _ = self.request(url, 'admin', correction)
        self.assertEqual(200, code)
        result = json.loads(raw)
        self.assertEqual(('blocked', 0, 17), (result['task']['status'], result['task']['step'], result['task']['fix_attempts_used']))
        self.assertEqual(detail['receipts'][-1]['receipt'], result['correction']['prior_receipt'])
        _, raw, _ = self.request(url, 'admin', correction)
        self.assertTrue(json.loads(raw)['replayed'])
        _, raw, _ = self.request('/api/tasks/' + ident)
        self.assertEqual(1, len(json.loads(raw)['outcome_corrections']))
        self.assertEqual(409, self.request('/api/tasks/' + ident + '/heartbeat', 'oliver-mini', body)[0])

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
                'summary': 'Twenty attempts settled', 'execution_stopped': True, 'fix_attempts_used': 20}})
        source = {'thread_id': 'main', 'question_id': 'one-extra', 'message_id': 'actual-yes'}
        _, raw, _ = self.request('/api/approvals/apply', 'oliver-mini', {
            'source': source, 'decision': 'approved', 'decision_verified': True,
            'response': 'Approve attempt 21', 'scope': 'One attempt on Mini', 'tasks': [{
                'id': ident, 'generation': 1, 'claim_token': claim['claim_token'],
                'execution_stopped': True, 'external_state_checked': True, 'scope_evidence': 'Actual human permits attempt 21'}]})
        approval = json.loads(raw)
        body = {'approval_id': approval['approval_id'], 'source': source, 'task': ident, 'generation': 1,
                'worker': 'oliver-mini', 'expected_used': 20, 'attempt_number': 21,
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
        self.assertEqual(21, current['task']['fix_attempts_used'])
        self.assertEqual(2, current['task']['generation'])
        code, raw, _ = self.request(url, 'oliver-mini', body)
        self.assertEqual(200, code); self.assertTrue(json.loads(raw)['replayed'])
        self.assertEqual('running', json.loads(raw)['task']['status'])
        self.assertNotIn(claim['claim_token'].encode(), raw)
        self.assertNotIn(current['claim_token'].encode(), raw)

    def test_admin_finite_attempt_allowance_is_scoped_and_freshly_read_back(self):
        from test_attempt_allowances import VM1, VM2, prepared_task, allowance_request
        for worker in (VM1, VM2): self.server.store.pulse(worker, {'capacity': {'cpus': 12}})
        ident = prepared_task(self.server.store)
        body = allowance_request(self.server.store, ident)
        url = '/api/attempt-allowances/apply'
        for actor in (None, 'oliver-mini', 'oliver-mac'):
            self.assertEqual(403, self.request(url, actor, body)[0])
        self.assertEqual(409, self.request(url, 'admin', {**body, 'generation': 99})[0])
        code, raw, _ = self.request(url, 'admin', body)
        self.assertEqual(200, code)
        result = json.loads(raw); self.assertFalse(result['replayed'])
        self.assertEqual((20, 30, 10), tuple(result['task'][k] for k in
            ('fix_attempts_used', 'fix_attempt_limit', 'fix_attempts_remaining')))
        _, raw, _ = self.request('/api/tasks/' + ident)
        task = json.loads(raw)
        self.assertEqual(result['grant'], task['attempt_allowances'][0])
        self.assertTrue(task['attempt_allowance_active'])
        self.assertNotIn('claim_token', raw.decode())
        self.assertTrue(json.loads(self.request(url, 'admin', body)[1])['replayed'])
        _, raw, _ = self.request('/api/state')
        self.assertEqual(1, json.loads(raw)['capabilities']['attempt_allowances'])

    def test_admin_amendment_preserves_actual_running_claim_and_prior_audit(self):
        import test_attempt_allowances as fixtures
        from test_allowance_amendments import amendment_request
        store = self.server.store
        for worker in (fixtures.VM1, fixtures.VM2): store.pulse(worker, {'capacity': {'cpus': 12}})
        ident = fixtures.prepared_task(store)
        first = store.apply_attempt_allowance('admin', fixtures.allowance_request(store, ident))
        claim = store.claim(fixtures.VM1, ident); body = amendment_request(store, ident)
        body.update(additional_attempts=50, response='Assign another 50 attempts')
        url = '/api/attempt-allowances/amend'
        for actor in (None, 'oliver-mini', 'oliver-mac'):
            self.assertEqual(403, self.request(url, actor, body)[0])
        self.assertEqual(409, self.request(url, 'admin', {**body, 'previous_grant_id': 'b' * 64})[0])
        code, raw, _ = self.request(url, 'admin', body); self.assertEqual(200, code)
        result = json.loads(raw); self.assertFalse(result['replayed'])
        self.assertEqual(('running', fixtures.VM1, claim['task']['generation'], 20, 80, 60),
                         tuple(result['task'][k] for k in ('status', 'owner', 'generation', 'fix_attempts_used', 'fix_attempt_limit', 'fix_attempts_remaining')))
        _, raw, _ = self.request('/api/tasks/' + ident)
        fresh = json.loads(raw); self.assertEqual(first['grant'], fresh['attempt_allowances'][0])
        self.assertEqual(result['grant'], fresh['attempt_allowance_amendments'][0])
        self.assertNotIn(claim['claim_token'].encode(), raw)
        self.assertTrue(json.loads(self.request(url, 'admin', body)[1])['replayed'])
        _, raw, _ = self.request('/api/state')
        self.assertEqual(1, json.loads(raw)['capabilities']['attempt_allowance_amendments'])

    def test_attempt_accounting_correction_is_admin_only_and_preserves_live_claim(self):
        from coordinator.attempt_accounting import BINDING
        store = self.server.store
        for worker in ('oliver-mac', 'oliver-mini'):
            store.pulse(worker, {'capacity': {'cpus': 12}})
        ident = store.submit('admin', {'request_key': 'accounting-http', 'title': 'Retained parent'})['id']
        identity = lambda c: {'generation': c['task']['generation'], 'step': c['task']['step'],
                              'claim_token': c['claim_token']}
        settled = {'execution_stopped': True, 'external_state_checked': True, 'evidence': 'Verified settled handoff'}
        first = store.claim('oliver-mini', ident)
        store.finish('oliver-mini', ident, {**identity(first), 'outcome': 'blocked', 'receipt': {
            'summary': 'Original failures', 'execution_stopped': True, 'fix_attempts_used': 20}})
        store.defer('admin', ident, {'generation': 1, 'worker': 'oliver-mac', **settled})
        prior = store.claim('oliver-mac', ident)
        store.defer('oliver-mac', ident, {**identity(prior), 'worker': 'oliver-mini', **settled})
        current = store.claim('oliver-mini', ident)
        before = store.detail(ident)
        ref = {'path': '/private/verified/evidence.json', 'sha256': 'a' * 64}
        body = {'request_id': 'http-verified-accounting', 'generation': before['generation'],
            'expected': {k: before[k] for k in BINDING},
            'corrected': {'fix_attempts_used': 2, 'total_fix_attempts': 37, 'attempt_base': 35},
            'owner_proof': {'task': ident, 'worker': 'oliver-mini', 'generation': before['generation'],
                'step': 0, 'fix_attempts_used': 2, 'verified': True, **settled,
                'unsettled_operations': [], 'receipt': ref, 'source': ref},
            'historical_proof': [{'task': ident, 'worker': 'oliver-mac',
                'generation': prior['task']['generation'], 'step': 0,
                'recorded_attempts': 0, 'fix_attempts_used': 15, 'omitted_attempts': 15,
                'verified': True, 'receipt': ref, 'source': ref, 'evidence': 'Verified omitted failures'}],
            'evidence': 'Admin checked owner settlement and immutable historical ledgers'}
        url = '/api/tasks/' + ident + '/correct-attempts'
        for actor in (None, 'oliver-mac', 'oliver-mini'):
            self.assertEqual(403, self.request(url, actor, body)[0])
        stale = {**body, 'generation': 99, 'owner_proof': {**body['owner_proof'], 'generation': 99}}
        self.assertEqual(409, self.request(url, 'admin', stale)[0])
        self.assertEqual(before, store.detail(ident))
        code, raw, _ = self.request(url, 'admin', body)
        self.assertEqual(200, code)
        result = json.loads(raw)
        self.assertFalse(result['replayed'])
        self.assertEqual(('running', 2, 37, 35), tuple(result['task'][k] for k in
            ('status', 'fix_attempts_used', 'total_fix_attempts', 'attempt_base')))
        _, raw, _ = self.request('/api/tasks/' + ident)
        readback = json.loads(raw)
        self.assertEqual(result['correction'], readback['attempt_corrections'][0])
        self.assertEqual(before['receipts'], readback['receipts'])
        self.assertNotIn(current['claim_token'].encode(), raw)
        self.assertTrue(json.loads(self.request(url, 'admin', body)[1])['replayed'])
        self.assertEqual(200, self.request('/api/tasks/' + ident + '/heartbeat', 'oliver-mini', identity(current))[0])

    def test_completed_attempt_correction_uses_existing_admin_route_and_preserves_acceptance(self):
        from test_completed_attempt_accounting import completed_request
        store = self.server.store
        store.pulse('oliver-mini', {'capacity': {'cpus': 12}})
        ident = store.submit('admin', {'request_key': 'closed-http', 'title': 'Qualified feature'})['id']
        claim = store.claim('oliver-mini', ident)
        store.finish('oliver-mini', ident, {'generation': claim['task']['generation'],
            'step': claim['task']['step'], 'claim_token': claim['claim_token'], 'outcome': 'completed',
            'receipt': {'summary': 'Actual acceptance passed and operations settled',
                'outcome': 'completed', 'acceptance_complete': True, 'execution_stopped': True,
                'external_state_checked': True, 'unsettled_operations': [], 'fix_attempts_used': 2,
                'landed_commit': 'a' * 40}})
        before = store.detail(ident); body = completed_request(store, ident, 3)
        url = '/api/tasks/' + ident + '/correct-attempts'
        for actor in (None, 'oliver-mac', 'oliver-mini'):
            self.assertEqual(403, self.request(url, actor, body)[0])
        missing = {k: v for k, v in body.items() if k != 'completion_proof'}
        self.assertEqual(400, self.request(url, 'admin', missing)[0])
        self.assertEqual(before, store.detail(ident))
        code, raw, _ = self.request(url, 'admin', body)
        self.assertEqual(200, code); result = json.loads(raw)
        self.assertFalse(result['replayed'])
        _, raw, _ = self.request('/api/tasks/' + ident)
        after = json.loads(raw)
        self.assertEqual(('completed', 1, 3, 3), tuple(after[k] for k in
                         ('status', 'step', 'fix_attempts_used', 'total_fix_attempts')))
        for key in ('result', 'receipts', 'generation', 'owner', 'attempt_allowance'):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual([result['correction']], after['attempt_corrections'])
        self.assertTrue(json.loads(self.request(url, 'admin', body)[1])['replayed'])
        self.assertEqual(1, json.loads(self.request('/api/state')[1])['capabilities']['completed_attempt_correction'])
        self.assertNotIn(claim['claim_token'].encode(), raw)

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

    def test_setup_wait_correction_http_preserves_human_request_and_requires_admin(self):
        self.request('/api/worker/pulse','oliver-mini',{'capacity':{'cpus':8}})
        _,raw,_=self.request('/api/tasks','admin',{'request_key':'setup-prerequisite','title':'Setup prerequisite'})
        dep=json.loads(raw)['id']
        for suffix,receipt in (('nonhuman',{}),('human',{'approval_request':{'thread_id':'human','question_id':'question'}})):
            _,raw,_=self.request('/api/tasks','admin',{'request_key':'setup-wait-'+suffix,'title':suffix})
            ident=json.loads(raw)['id']
            _,raw,_=self.request('/api/worker/claim','oliver-mini',{'task_id':ident})
            claim=json.loads(raw)
            identity={'generation':claim['task']['generation'],'claim_token':claim['claim_token'],'step':0}
            self.request('/api/tasks/'+ident+'/finish','oliver-mini',{**identity,'outcome':'pending_external_approval',
                'receipt':{'summary':'Stopped setup wait','execution_stopped':True,**receipt}})
            body={'generation':identity['generation'],'execution_stopped':True,'external_state_checked':True,
                'evidence':'Owner verified this is a stopped setup wait, not a new human decision','dependencies':[dep],
                'wait_correction':{'category':'setup_dependency','approval_not_required':True,'reason':'Verified exact owner receipt and setup prerequisite'}}
            url='/api/tasks/'+ident+'/defer'
            self.assertEqual(403,self.request(url,body=body)[0])
            self.assertEqual(409,self.request(url,'oliver-mini',body)[0])
            code,raw,_=self.request(url,'admin',body)
            if suffix=='human':
                self.assertEqual(409,code)
            else:
                self.assertEqual(200,code)
                result=json.loads(raw);self.assertEqual('queued',result['status'])
                self.assertFalse(result['ready']);self.assertEqual([dep],result['dependencies'])

    def test_manual_release_requires_admin_and_repository(self):
        for actor in (None,'oliver-mini'):
            self.assertEqual(403,self.request('/api/releases/start',actor,{'repository':'oliverhu/nova-browser'})[0])
        self.assertEqual(400,self.request('/api/releases/start','admin',{})[0])
        import time
        self.request('/api/repositories/configure','admin',{'repository':'oliverhu/nova-browser','published_commit':'a'*40})
        self.request('/api/repositories/observe','oliver-mini',{'repository':'oliverhu/nova-browser','commit':'b'*40,'observed_at':time.time()})
        code,body,_=self.request('/api/releases/start','admin',{'repository':'oliverhu/nova-browser'})
        self.assertEqual(200,code);self.assertEqual(1,len(json.loads(body)['created_ids']))
        ident=json.loads(body)['created_ids'][0]
        _,raw,_=self.request('/api/tasks/'+ident)
        self.assertEqual({'worker':'oliverhu-mac-studio'},json.loads(raw)['requirements'])
        self.request('/api/worker/pulse','oliver-mini',{'capacity':{'cpus':12,'capabilities':['macos','development']}})
        _,raw,_=self.request('/api/worker/claim','oliver-mini',{'task_id':ident})
        self.assertIsNone(json.loads(raw)['task'])

    def test_repository_configuration_and_observations_require_identity(self):
        body={'repository':'owner/repo','published_commit':'a'*40}
        self.assertEqual(403,self.request('/api/repositories/configure','oliver-mini',body)[0])
        self.assertEqual(200,self.request('/api/repositories/configure','admin',body)[0])
        import time
        observation={'repository':'owner/repo','commit':'b'*40,'observed_at':time.time()}
        self.assertEqual(403,self.request('/api/repositories/observe',body=observation)[0])
        self.assertEqual(200,self.request('/api/repositories/observe','oliver-mini',observation)[0])

    def test_ios_only_route_requires_admin_preserves_mac_baseline_and_finishes_under_own_claim(self):
        import time
        from coordinator.ios_release import STUDIO, NOVA
        self.tokens[STUDIO] = 's' * 40
        self.request('/api/repositories/configure', 'admin', {'repository': NOVA, 'published_commit': 'a' * 40})
        self.request('/api/repositories/observe', STUDIO, {'repository': NOVA, 'commit': 'b' * 40, 'observed_at': time.time()})
        _, raw, _ = self.request('/api/tasks', 'admin', {'request_key': 'voice-ios', 'title': 'Voice'})
        dep = json.loads(raw)['id']
        body = {'scope': 'ios_upload_only', 'repository': NOVA, 'commit': 'b' * 40,
                'dependencies': [dep], 'authorization': {'thread_id': 'primary', 'message_id': 'actual-upload'}}
        for actor in (None, 'oliver-mini', STUDIO):
            self.assertEqual(403, self.request('/api/releases/start', actor, body)[0])
        self.assertEqual(400, self.request('/api/releases/start', 'admin', {**body, 'scope': 'ios_typo'})[0])
        baseline = self.server.store.snapshot()['repositories']
        code, raw, _ = self.request('/api/releases/start', 'admin', body)
        self.assertEqual(200, code); ident = json.loads(raw)['created_ids'][0]
        self.assertEqual(baseline, self.server.store.snapshot()['repositories'])
        self.request('/api/worker/pulse', STUDIO, {'capacity': {'cpus': 12}})
        _, raw, _ = self.request('/api/worker/claim', STUDIO, {'task_id': dep})
        claim = json.loads(raw)
        self.request('/api/tasks/' + dep + '/finish', STUDIO, {
            'generation': 1, 'step': 0, 'claim_token': claim['claim_token'], 'outcome': 'completed',
            'receipt': {'summary': 'Voice merged under revised acceptance', 'execution_stopped': True}})
        _, raw, _ = self.request('/api/worker/claim', STUDIO, {'task_id': ident}); claim = json.loads(raw)
        finish = {'generation': 1, 'step': 0, 'claim_token': claim['claim_token'], 'outcome': 'completed',
            'receipt': {'summary': 'Actual Apple upload accepted', 'execution_stopped': True,
                'external_state_checked': True, 'publication_ref': 'apple-upload-receipt',
                'ios_upload': {'status': 'uploaded', 'source_commit': 'b' * 40, 'version': '0.2.2',
                    'build_number': '52', 'upload_ref': 'apple-upload-receipt', 'testflight_distributed': False,
                    'submitted_for_review': False, 'testflight_settings_changed': False, 'ios_public_release': False}}}
        self.assertEqual(403, self.request('/api/tasks/' + ident + '/finish', 'admin', finish)[0])
        self.assertEqual(409, self.request('/api/tasks/' + ident + '/finish', 'oliver-mini', finish)[0])
        code, raw, _ = self.request('/api/tasks/' + ident + '/finish', STUDIO, finish)
        self.assertEqual(200, code); self.assertEqual('completed', json.loads(raw)['status'])
        self.assertEqual(baseline, self.server.store.snapshot()['repositories'])
        _, raw, _ = self.request('/api/releases/start', 'admin', body)
        self.assertEqual([ident], json.loads(raw)['task_ids']); self.assertTrue(json.loads(raw)['replayed'])


    def test_studio_worker_authentication_and_admin_only_retirement(self):
        studio = "oliverhu-mac-studio"
        self.tokens[studio] = "s" * 40
        self.assertEqual(200, self.request('/api/worker/pulse', studio, {'capacity': {'cpus': 14}})[0])
        state = '/api/workers/' + studio + '/state'
        body = {'paused': True, 'reason': 'Migration retirement test'}
        self.assertEqual(403, self.request(state, studio, body)[0])
        self.assertEqual(403, self.request(state, body=body)[0])
        self.assertEqual(200, self.request(state, 'admin', body)[0])
        self.assertEqual(400, self.request('/api/workers/unknown/state', 'admin', body)[0])
        _, raw, _ = self.request('/api/worker/claim', studio, {})
        self.assertIsNone(json.loads(raw)['task'])


if __name__=="__main__":unittest.main()
