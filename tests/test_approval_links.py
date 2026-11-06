import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from coordinator.store import Store, Conflict, Invalid
from coordinator import approval_links as links


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'db', clock=lambda:1000)
        self.worker='oliver-mini';self.store.pulse(self.worker,{'capacity':{'cpus':12,'memory_gb':24}})
        t=self.store.submit(self.worker,{'request_key':'link','title':'Exact actor'})
        self.claim=self.store.claim(self.worker,t['id'])
        self.store.finish(self.worker,t['id'],{'claim_token':self.claim['claim_token'],'generation':1,'step':0,
            'outcome':'pending_external_approval','receipt':{'execution_stopped':True,'external_state_checked':True,
             'acceptance_complete':False,'summary':'Original pending question','approval_request':{'thread_id':'original','question_id':'binding'}}})
        self.before=self.store.detail(t['id'])
        self.body={'task':t['id'],'generation':1,'worker':self.worker,'original_question':self.before['result']['approval_request'],
            'original_question_item_id':'actual-provider-item','source':{'thread_id':'coordinator','question_id':'later-question','message_id':'human-response'},
            'original_question_text':'Exact actor asks three categories','question_text':'Coordinator explicitly repeats exact actor and categories',
            'response':'Granted requested access','scope':'Reported grant only; predicates still require qualification',
            'scope_evidence':'Actual primary question and response inspected; no other task covered',
            'result_sha256':hashlib.sha256(links.encode(self.before['result']).encode()).hexdigest(),
            'decision_verified':True,'link_verified':True,'execution_stopped':True,'external_state_checked':True}

    def test_link_preserves_original_records_and_audits_both_sources(self):
        r=links.apply(self.store,'admin',self.body);t=self.store.detail(self.body['task'])
        self.assertEqual('queued',t['status']);self.assertIsNone(t['owner'])
        for k in ('result','receipts','dependencies','requirements','generation','fix_attempts_used','total_fix_attempts'):
            self.assertEqual(self.before[k],t[k])
        self.assertEqual(self.before['events'],t['events'][:-1]);self.assertEqual('approval-applied',t['events'][-1]['kind'])
        audit=links.detail(self.store,'admin',r['link_id']);self.assertEqual(self.body,audit['request'])
        self.assertEqual('original',audit['request']['original_question']['thread_id'])
        self.assertEqual('coordinator',t['approval']['evidence']['source']['thread_id'])
        with self.store.connection() as db:self.assertEqual(0,db.execute('SELECT count(*) FROM approvals').fetchone()[0])

    def test_replay_never_requeues_a_later_claim_and_scope_conflicts_reject(self):
        r=links.apply(self.store,'admin',self.body);self.store.claim(self.worker,self.body['task'])
        self.assertTrue(links.apply(self.store,'admin',self.body)['replayed'])
        self.assertEqual('running',self.store.detail(self.body['task'])['status'])
        changed={**self.body,'scope':'Expanded scope'}
        with self.assertRaises(Conflict):links.apply(self.store,'admin',changed)

    def test_strict_scope_identity_and_verification_fail_closed(self):
        for delta in ({'generation':2},{'worker':'foreign'},{'result_sha256':'0'*64},
                      {'original_question':{'thread_id':'other','question_id':'binding'}},
                      {'decision_verified':False},{'link_verified':False},{'execution_stopped':False},
                      {'external_state_checked':False},{'scope_evidence':''},{'original_question_item_id':''},
                      {'source':{'thread_id':'original','question_id':'binding','message_id':'human'}}):
            with self.subTest(delta=delta),self.assertRaises((ValueError,Conflict,Invalid)):
                links.apply(self.store,'admin',{**self.body,**delta})
        with self.assertRaises(Conflict):links.apply(self.store,self.worker,self.body)
        self.assertEqual(self.before,self.store.detail(self.body['task']))

    def test_ordinary_binding_and_immutable_source_remain_enforced(self):
        b={'source':self.body['source'],'decision':'approved','decision_verified':True,'response':'Granted',
           'scope':'Exact actor','tasks':[{'id':self.body['task'],'generation':1,'scope_evidence':'Exact',
             'execution_stopped':True,'external_state_checked':True}]}
        with self.assertRaises(Conflict):self.store.apply_approval('admin',b)
        links.apply(self.store,'admin',self.body)
        with self.assertRaises(Conflict):self.store.apply_approval('admin',b)
