import concurrent.futures
import copy
import json
import tempfile
import unittest
from pathlib import Path
from coordinator.approvals import approval_id, approval_payload
from coordinator.client import Client, ServiceRejected
from coordinator.store import Store, Conflict


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(self.root/'db',clock=lambda:1000)
        for worker in ('oliver-mini','oliver-mac'):
            self.store.pulse(worker,{'capacity':{'cpus':12,'memory_gb':24}})
        self.claims=[]

    def tearDown(self):self.temp.cleanup()

    def pending(self,name,worker='oliver-mini',**receipt):
        task=self.store.submit(worker,{'request_key':name,'title':name,'requirements':{'worker':worker}})
        claim=self.store.claim(worker,task['id']);self.claims.append(claim)
        self.finish(claim,'pending_external_approval',approval_request={'thread_id':'thread','question_id':'question'},**receipt)
        return claim

    def finish(self,claim,outcome,**receipt):
        task=claim['task']
        return self.store.finish(task['owner'],task['id'],{'claim_token':claim['claim_token'],
            'generation':task['generation'],'step':task['step'],'outcome':outcome,
            'receipt':{'summary':'Awaiting explicit tool choice','execution_stopped':True,**receipt}})

    def body(self,*claims):
        return {'source':{'thread_id':'thread','question_id':'question','message_id':'message'},
                'decision':'approved','decision_verified':True,'response':'Use installed Peekaboo',
                'scope':'Only isolated native qualification; no agents or security changes',
                'tasks':[{'id':c['task']['id'],'generation':c['task']['generation'],'claim_token':c['claim_token'],
                          'execution_stopped':True,'external_state_checked':True,
                          'scope_evidence':'The question explicitly covers '+c['task']['title']} for c in claims]}

    def test_multi_task_approval_during_other_claim_preserves_evidence_and_dependencies(self):
        first=self.pending('hover',fix_attempts_used=2,branch='existing-source');second=self.pending('modifier')
        untouched=self.pending('unrelated',worker='oliver-mac')
        active=self.store.submit('oliver-mini',{'request_key':'active','title':'Target qualification'})
        claim=self.store.claim('oliver-mini',active['id'])
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET dependencies=? WHERE id=?',(json.dumps([active['id']]),first['task']['id']))
        before=[self.store.detail(c['task']['id']) for c in (first,second)]
        result=self.store.apply_approval('oliver-mini',self.body(first,second))
        self.assertTrue(result['recorded']);self.assertFalse(result['replayed'])
        for original in before:
            task=self.store.detail(original['id']);self.assertEqual('queued',task['status'])
            self.assertEqual('approved',task['approval']['state']);self.assertFalse(task['ready'])
            for field in ('result','receipts','dependencies','requirements','generation','fix_attempts_used','total_fix_attempts','reassignment_count'):
                self.assertEqual(original[field],task[field])
        self.assertEqual('running',self.store.detail(active['id'])['status'])
        self.store.heartbeat('oliver-mini',active['id'],{'claim_token':claim['claim_token'],'generation':1})
        self.assertEqual('pending_external_approval',self.store.detail(untouched['task']['id'])['status'])
        with self.store.connection() as db:
            payload=db.execute('SELECT payload FROM approvals').fetchone()[0]
            self.assertNotIn(first['claim_token'],payload)
        self.assertIsNone(self.store.claim('oliver-mini',first['task']['id'])['task'])

    def test_stale_or_foreign_scope_fails_atomically(self):
        first=self.pending('first');second=self.pending('second');body=self.body(first,second)
        stale=copy.deepcopy(body);stale['tasks'][1]['generation']+=1
        foreign=copy.deepcopy(body);foreign['tasks'][1]['claim_token']='wrong'
        unrelated=copy.deepcopy(body);unrelated['source']['question_id']='different-question'
        for bad in (stale,foreign,unrelated):
            with self.assertRaises(Conflict):self.store.apply_approval('oliver-mini',bad)
        with self.assertRaises(Conflict):self.store.apply_approval('oliver-mac',body)
        for c in (first,second):self.assertEqual('pending_external_approval',self.store.detail(c['task']['id'])['status'])
        with self.store.connection() as db:self.assertEqual(0,db.execute('SELECT count(*) FROM approvals').fetchone()[0])

    def test_duplicate_concurrent_and_restarted_application_has_one_transition(self):
        first=self.pending('one');second=self.pending('two');body=self.body(first,second)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.store.apply_approval('oliver-mini',body),range(12)))
        self.assertEqual(1,sum(not r['replayed'] for r in results))
        self.store=Store(self.root/'db',clock=lambda:1000)
        self.assertTrue(self.store.apply_approval('oliver-mini',body)['replayed'])
        for claim in (first,second):
            task=self.store.detail(claim['task']['id'])
            self.assertEqual(1,sum(e['kind']=='approval-applied' for e in task['events']))
            self.assertEqual(1,task['generation'])

    def test_old_approval_never_resumes_a_later_wait_or_changes_new_claim(self):
        old=self.pending('one',pending_external_approval=True,approval_received=False);body=self.body(old);record=self.store.apply_approval('oliver-mini',body)
        new=self.store.claim('oliver-mini',old['task']['id'])
        replay=self.store.apply_approval('oliver-mini',body)
        self.assertEqual('running',replay['tasks'][0]['status']);self.assertEqual(2,replay['tasks'][0]['generation'])
        current=self.store.detail(old['task']['id'])
        self.assertFalse(current['result']['approval_received']);self.assertEqual('approved',current['approval']['state'])
        self.finish(new,'pending_external_approval',approval_request={'thread_id':'thread','question_id':'new-question'})
        replay=self.store.apply_approval('oliver-mini',body)
        self.assertEqual('pending_external_approval',replay['tasks'][0]['status'])
        self.assertEqual('awaiting_decision',self.store.detail(old['task']['id'])['approval']['state'])
        changed=copy.deepcopy(body);changed['tasks'][0]['generation']=2
        with self.assertRaises(Conflict):self.store.apply_approval('oliver-mini',changed)
        with self.assertRaises(Conflict):self.store.approval_detail('oliver-mac',record['approval_id'])

    def test_missing_denied_or_unverified_decision_does_not_mutate(self):
        task=self.pending('one');body=self.body(task)
        missing=copy.deepcopy(body);del missing['source']['message_id']
        for bad in ({**body,'decision':'denied'},{**body,'decision_verified':False},missing,{**body,'tasks':[]},{**body,'response':''}):
            with self.assertRaises(ValueError):self.store.apply_approval('oliver-mini',bad)
        self.assertEqual('pending_external_approval',self.store.detail(task['task']['id'])['status'])

    def test_human_stop_and_active_task_not_cleared(self):
        task=self.pending('stop',human_stop=True)
        with self.assertRaises(Conflict):self.store.apply_approval('oliver-mini',self.body(task))
        other=self.store.submit('oliver-mini',{'request_key':'active','title':'Active'})
        claim=self.store.claim('oliver-mini',other['id'])
        with self.assertRaises(Conflict):self.store.apply_approval('oliver-mini',self.body(claim))
        self.assertEqual('running',self.store.detail(other['id'])['status'])

    def test_scope_cannot_expand_after_response_applied(self):
        first=self.pending('one');second=self.pending('two');self.store.apply_approval('oliver-mini',self.body(first))
        with self.assertRaises(Conflict):self.store.apply_approval('oliver-mini',self.body(first,second))
        self.assertEqual('pending_external_approval',self.store.detail(second['task']['id'])['status'])

    def client(self):
        client=Client.__new__(Client);client.path=self.root/'client.json';client.state=self.root/'claims';client.state.mkdir(exist_ok=True)
        for claim in self.claims:(client.state/(claim['task']['id']+'.json')).write_text(json.dumps(claim))
        def request(path,body=None):
            if path=='/api/approvals/apply':return self.store.apply_approval('oliver-mini',body)
            if path.startswith('/api/approvals/'):return self.store.approval_detail('oliver-mini',path.rsplit('/',1)[1])
            if path=='/api/worker/claim':return self.store.claim('oliver-mini',body.get('task_id'))
            raise AssertionError(path)
        client.request=request;client.pulse=lambda:None
        return client

    def test_client_persists_before_request_and_recovers_lost_post_response(self):
        first=self.pending('one');second=self.pending('two');body=self.body(first,second);client=self.client()
        normal=client.request;calls=[]
        def lost(path,value=None):
            calls.append(path)
            if path=='/api/approvals/apply':
                journal=next((self.root/'approvals').glob('*.json'))
                self.assertEqual(0o600,journal.stat().st_mode&0o777)
                self.assertEqual('pending',json.loads(journal.read_text())['state'])
                normal(path,value);raise OSError('Connection lost after commit')
            return normal(path,value)
        client.request=lost
        with self.assertRaises(OSError):client.apply_approval(body)
        # Claim cache is no longer needed after the request was journaled.
        for path in client.state.glob('*.json'):path.unlink()
        client.request=normal;recovered=client.recover_approvals()
        self.assertEqual(1,len(recovered));self.assertTrue(recovered[0]['recorded'])
        journal=next((self.root/'approvals').glob('*.json'))
        self.assertEqual('applied',json.loads(journal.read_text())['state'])
        self.assertEqual([],client.recover_approvals())
        for claim in (first,second):self.assertEqual(1,sum(e['kind']=='approval-applied' for e in self.store.detail(claim['task']['id'])['events']))

    def test_client_lost_readback_recovers_before_next_claim(self):
        first=self.pending('one');body=self.body(first);client=self.client();normal=client.request
        def lost(path,value=None):
            if path.startswith('/api/approvals/') and path!='/api/approvals/apply':raise OSError('Readback unavailable')
            return normal(path,value)
        client.request=lost
        with self.assertRaises(OSError):client.apply_approval(body)
        client.request=normal;claim=client.claim(first['task']['id'])
        self.assertEqual('running',claim['task']['status']);self.assertTrue(claim['approval_recovery'][0]['recorded'])
        self.assertEqual('applied',json.loads(next((self.root/'approvals').glob('*.json')).read_text())['state'])

    def test_client_readback_mismatch_never_reports_applied(self):
        first=self.pending('one');client=self.client();normal=client.request
        def mismatch(path,value=None):
            result=normal(path,value)
            if path.startswith('/api/approvals/') and path!='/api/approvals/apply':result['tasks']=[]
            return result
        client.request=mismatch
        with self.assertRaisesRegex(RuntimeError,'readback mismatch'):client.apply_approval(self.body(first))
        self.assertEqual('pending',json.loads(next((self.root/'approvals').glob('*.json')).read_text())['state'])

    def test_client_rejected_scope_needs_review_not_infinite_retry(self):
        task=self.pending('one');client=self.client()
        client.request=lambda *args:(_ for _ in ()).throw(ServiceRejected(409,'Scope changed'))
        with self.assertRaises(ServiceRejected):client.apply_approval(self.body(task))
        self.assertEqual('needs_review',json.loads(next((self.root/'approvals').glob('*.json')).read_text())['state'])
        self.assertEqual([],client.recover_approvals())

    def test_client_denied_response_is_not_journaled(self):
        task=self.pending('one');client=self.client()
        with self.assertRaises(ValueError):client.apply_approval({**self.body(task),'decision':'denied'})
        self.assertFalse((self.root/'approvals').exists())


if __name__=='__main__':unittest.main()
