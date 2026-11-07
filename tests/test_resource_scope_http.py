"""Real loopback HTTP authentication and exact task admission, temporary DB only."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from coordinator.server import Server
from coordinator import resource_scope as scope
from coordinator.workers import MINI_VM_WORKERS
from test_resource_scope import Fixture


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.f=Fixture(Path(self.temp.name))
        self.tokens={'admin':'a'*40,self.f.worker:'b'*40,MINI_VM_WORKERS[1]:'c'*40,'oliverhu-mac-studio':'d'*40}
        self.server=Server(('127.0.0.1',0),self.f.store,self.tokens,Path(__file__).resolve().parents[1]/'web')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def request(self,path,actor=None,body=None):
        headers={'Content-Type':'application/json'}
        if actor:headers['Authorization']='Bearer '+self.tokens[actor]
        req=urllib.request.Request('http://127.0.0.1:'+str(self.server.server_port)+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with urllib.request.urlopen(req,timeout=4) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as error:
            with error:return error.code,json.load(error)

    def test_policy_admin_only_private_worker_read_and_scoped_reservation_through_normal_claim(self):
        route='/api/workers/'+self.f.worker+'/resource-scope'
        body={'policy':self.f.policy,'expected_revision':None}
        for actor in (None,self.f.worker,MINI_VM_WORKERS[1],'oliverhu-mac-studio'):
            self.assertEqual(self.request(route,actor,body)[0],403)
        code,value=self.request(route,'admin',body);self.assertEqual(code,200);self.f.revision=value['revision']
        self.assertEqual(self.request('/api/worker/resource-scope/policy',self.f.worker)[1]['revision'],self.f.revision)
        self.assertEqual(self.request('/api/worker/resource-scope/policy',MINI_VM_WORKERS[1])[0],409)
        for actor in (None,'admin','oliverhu-mac-studio'):
            self.assertEqual(self.request('/api/worker/resource-scope/policy',actor)[0],403)
            for action in ('reserve','complete'):
                self.assertEqual(self.request('/api/worker/resource-scope/'+action,actor,{})[0],403)
        task=self.f.task();token='inert-scoped-http-private-token-0000000000';ident='4'*32
        reserve={'id':ident,'run_id':ident,'task_id':task['id'],'queued_generation':0,'policy_revision':self.f.revision,
            'task_sha256':scope.task_fingerprint(self.f.store.detail(task['id'])),
            'token_hash':hashlib.sha256(token.encode()).hexdigest(),'local_fence_sha256':self.f.policy['local_fence_sha256']}
        self.assertEqual(self.request('/api/worker/resource-scope/reserve',MINI_VM_WORKERS[1],reserve)[0],409)
        code,value=self.request('/api/worker/resource-scope/reserve',self.f.worker,reserve)
        self.assertEqual(code,200);self.assertFalse(value['generic_available'])
        binding={'lane':'resource-scoped','id':ident,'run_id':ident,'token':token}
        self.assertIsNone(self.request('/api/worker/claim',self.f.worker,{'task_id':task['id']})[1]['task'])
        self.assertIsNone(self.request('/api/worker/claim',self.f.worker,{'task_id':task['id'],'dispatch_binding':{**binding,'token':'wrong'}})[1]['task'])
        code,value=self.request('/api/worker/claim',self.f.worker,{'task_id':task['id'],'dispatch_binding':binding})
        self.assertEqual(code,200);self.assertEqual(value['task']['status'],'running');self.f.assert_held(self)


if __name__=='__main__':unittest.main()
