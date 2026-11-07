import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import uuid
import urllib.request
import urllib.error
from coordinator.server import Server
from coordinator.store import Store
from coordinator.client import Client, supervised, ServiceRejected


class ReadinessHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.worker = 'oliverhu-mac-studio'
        self.server = Server(('127.0.0.1',0), Store(self.root/'db'),
            {'admin':'a'*40, self.worker:'b'*40}, Path(__file__).resolve().parents[1]/'web')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)
        config = self.root/'client.json'
        config.write_text(json.dumps({'worker':self.worker,'url':self.url,'token':'b'*40}))
        self.client = Client(config)
        self.client.request('/api/worker/pulse', {'capacity':{'cpus':2}})
        self.task = self.client.request('/api/tasks', {'request_key':'http','title':'HTTP fixture',
            'requirements':{'readiness':{'checks':['source-cli'],'decisions':{}}}})['id']

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def request(self, route, actor, body):
        request = urllib.request.Request(self.url+route,data=json.dumps(body).encode(),
            headers={'Content-Type':'application/json', **({'Authorization':'Bearer '+('a'*40 if actor=='admin' else 'b'*40)} if actor else {})})
        try:
            with urllib.request.urlopen(request, timeout=3) as response: return response.status,json.load(response)
        except urllib.error.HTTPError as error: return error.code,json.load(error)

    def update(self, state='ready'):
        task = self.client.request('/api/tasks/'+self.task)
        return self.request('/api/tasks/'+self.task+'/readiness','admin',{
            'request_id':uuid.uuid4().hex,'generation':task['generation'],
            'expected_revision':task['task_readiness']['revision'],'context_sha256':task['task_readiness']['context_sha256'],
            'worker':self.worker,'state':state,'valid_until':time.time()+300,
            'checks':[{'name':'source-cli','state':'qualified','evidence_sha256':'c'*64}],
            'decisions':[],'evidence':'Actual scoped fixture qualification'})

    def claim(self):
        claim = self.client.request('/api/worker/claim',{'task_id':self.task})
        (self.client.state/(self.task+'.json')).write_text(json.dumps(claim))
        return claim

    def test_admin_only_typed_mutations_and_missing_readiness_denies_http_claim(self):
        for actor in (None,self.worker):
            self.assertEqual(403,self.request('/api/tasks/'+self.task+'/readiness',actor,{})[0])
            self.assertEqual(403,self.request('/api/task-decisions/revise',actor,{})[0])
        self.assertEqual(400,self.request('/api/task-decisions/revise','admin',{})[0])
        self.assertEqual(400,self.request('/api/tasks/'+self.task+'/readiness','admin',{})[0])
        self.assertIsNone(self.client.request('/api/worker/claim',{'task_id':self.task})['task'])
        self.assertEqual(200,self.update()[0]); self.assertIsNotNone(self.claim()['task'])

    def test_actual_watchdog_refuses_before_spawn_and_allows_truthful_cleanup(self):
        self.update(); self.claim(); self.update('waiting')
        marker = self.root/'must-not-exist'
        with self.assertRaises(ServiceRejected): supervised(self.client,self.task,[sys.executable,'-c',f'from pathlib import Path; Path({str(marker)!r}).touch()'])
        self.assertFalse(marker.exists())
        self.client.heartbeat(self.task,phase='cleanup')
        self.assertEqual(0,supervised(self.client,self.task,[sys.executable,'-c','pass']))
        with self.assertRaises(ServiceRejected): self.client.finish(self.task,{'summary':'Incomplete','execution_stopped':True},'completed')
        result = self.client.finish(self.task,{'summary':'Actual process exited; effects refused','execution_stopped':True,'acceptance_complete':False},'blocked')
        self.assertEqual('blocked',result['status'])

    def test_actual_watchdog_reaps_running_command_after_revision_changes(self):
        self.update(); self.claim()
        started = self.root/'started'
        result = []
        def run():
            try:
                supervised(self.client,self.task,[sys.executable,'-c',
                    f'from pathlib import Path; import time; Path({str(started)!r}).touch(); time.sleep(30)'],heartbeat_interval=.02)
            except RuntimeError as error: result.append(str(error))
        thread = threading.Thread(target=run); thread.start()
        try:
            deadline=time.monotonic()+4
            while not started.exists() and time.monotonic()<deadline: time.sleep(.01)
            self.assertTrue(started.exists())
            self.assertEqual(200,self.update('waiting')[0])
            thread.join(timeout=7)
            self.assertFalse(thread.is_alive())
            self.assertTrue(result and 'owned process stopped' in result[0])
            task=self.client.request('/api/tasks/'+self.task)
            self.assertEqual('running',task['status']); self.assertEqual(self.worker,task['owner'])
            self.assertEqual([],task['receipts'])
            self.client.heartbeat(self.task,phase='cleanup')
            self.client.finish(self.task,{'summary':'Owned watchdog invocation reaped; no remote input','execution_stopped':True},'blocked')
        finally:
            thread.join(timeout=35)
