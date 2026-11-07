"""Historical consent metadata cannot replace current cleanup or human decisions."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coordinator import cli_dispatch as adapter
from coordinator.cli_results import classify_historical_approval as classify
from coordinator.cli_results import evaluate
from test_cli_wait_settlement import fixture as base_wait


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def fixture(linked=False):
    run,task,_,_,owner,report,wait=base_wait()
    run['queued_generation']=2
    report.update(result_schema=1,run_id=run['run_id'],generation=3,outcome='waiting',
                  external_state_checked=True,cleanup={'execution_stopped':True,
                    'external_state_checked':True,'input_settled':True,
                    'unknown_operations':[],'unsettled_operations':[],
                    'evidence':'Owned process reaped and exact fixture closed.'})
    wait['generation']=3
    question={'thread_id':'original-thread','question_id':'original-question'}
    source={**question,'message_id':'actual-human-response'}
    if linked:source={'thread_id':'decision-thread','question_id':'linked-question','message_id':'actual-human-response'}
    pending={'task':task['id'],'generation':1,'worker':owner,'outcome':'pending_external_approval',
             'acceptance_complete':False,'execution_stopped':True,'external_state_checked':True,
             'summary':'Original narrowly scoped question','approval_request':question,
             'human_action_required':True,'cleanup':{'unknown_operations':[],'unsettled_operations':[]}}
    evidence={'approval_id':digest(source),'generation':1,'source':source,'response':'approved',
              'scope':'Original exact actor only; no runtime or permission acceptance inferred.',
              'scope_evidence':'Exact original question was explicitly answered.'}
    if linked:
        identity={'task':task['id'],'generation':1,'original_question':question,'source':source}
        evidence['question_link']={'link_id':digest(identity),'task':task['id'],'generation':1,
                                  'original_question':question,'result_sha256':digest(pending)}
    receipts=[{'task_id':task['id'],'generation':1,'step':0,'at':1.9,
               'receipt':{'outcome':'pending_external_approval','receipt':pending}}]
    for generation,at,summary in [(2,4.9,'Intermediate wait'),(3,9.0,wait['evidence'])]:
        receipts.append({'task_id':task['id'],'generation':generation,'step':0,'at':at,
                         'receipt':{'outcome':'waiting','receipt':{'execution_stopped':True,
                            'external_state_checked':True,'summary':summary}}})
    def event(ident,at,kind,detail,actor=owner):
        return {'id':ident,'at':at,'task_id':task['id'],'actor':actor,'kind':kind,
                'detail':json.dumps(detail) if isinstance(detail,dict) else detail}
    task.update(generation=3,result=copy.deepcopy(pending),receipts=receipts,
                approval={'state':'approved','recorded_at':3.0,'evidence':evidence},events=[
                    event(1,1.0,'claimed','Step 1; generation 1; resources []'),
                    event(2,2.0,'pending_external_approval',pending['summary']),
                    event(3,3.0,'approval-applied',evidence,'admin'),
                    event(4,4.0,'claimed','Step 1; generation 2; resources []'),
                    event(5,5.0,'waiting',{'worker':owner,'dependencies':[],'evidence':'Intermediate wait'}),
                    event(6,6.0,'claimed','Step 1; generation 3; resources []'),
                    event(7,9.1,'waiting',{'worker':owner,'dependencies':task['dependencies'],'evidence':wait['evidence']})])
    docs={'worker-report.json':report,'wait-receipt.json':wait,'task-readback.json':copy.deepcopy(task),
          'finish-response.json':{k:copy.deepcopy(v) for k,v in task.items() if k not in ('receipts','events','approval')}}
    return run,task,docs


def proof(args):
    run,task,docs=args
    return classify(run,task,docs['worker-report.json'],docs['wait-receipt.json'],docs['task-readback.json'])


def reduce(args):
    return evaluate(*args,{},adapter.validate_settlement)


class HistoricalApprovalTests(unittest.TestCase):
    def test_direct_and_cross_chat_questions_classify_without_rewriting_or_scope_extension(self):
        for linked in (False,True):
            args=fixture(linked);before=copy.deepcopy(args)
            self.assertEqual('historical-approved-question',proof(args)['classification'])
            result=reduce(args)
            self.assertEqual('waiting',result.outcome);self.assertFalse(result.acceptance_complete)
            self.assertEqual('available',result.availability);self.assertEqual(before,args)
            self.assertTrue(args[1]['result']['human_action_required'])
            self.assertEqual(1,args[1]['approval']['evidence']['generation'])

    def test_opaque_historical_cleanup_is_metadata_and_malformed_history_refuses(self):
        args=fixture(False);task=args[1]
        task['result']['cleanup']='/original/private/stopped-cleanup.json'
        task['receipts'][0]['receipt']['receipt']=copy.deepcopy(task['result'])
        args[2]['task-readback.json']=copy.deepcopy(task)
        args[2]['finish-response.json']['result']=copy.deepcopy(task['result'])
        self.assertEqual('historical-approved-question',proof(args)['classification'])
        self.assertEqual('available',reduce(args).availability)
        for field,value in [('events',[{}]),('receipts',[None]),('approval',None),('result',None)]:
            bad=fixture(False);bad[1][field]=value;bad[2]['task-readback.json']=copy.deepcopy(bad[1])
            with self.subTest(field=field),self.assertRaises(ValueError):proof(bad)
        args=fixture(False);args[1]['events'][2]['detail']=json.dumps({**args[1]['approval']['evidence'],'generation':True})
        args[2]['task-readback.json']=copy.deepcopy(args[1])
        with self.assertRaises(ValueError):proof(args)

    def test_current_missing_mismatched_and_stale_authority_fail_closed(self):
        cases=['pending','same-generation','missing-approval','missing-pending','duplicate-pending',
               'source','question','link-result','link-identity','pending-result','pending-event',
               'approval-actor','approval-event','approval-time','missing-claim','foreign-claim',
               'claim-generation','current-wait','later-question','unsupported-event','stale-readback',
               'current-report-question','current-wait-question','current-human-stop','current-action',
               'historical-unknown','intermediate-question','intermediate-unknown']
        for case in cases:
            args=fixture(True);run,task,docs=args;e=task['approval']['evidence']
            if case=='pending':task['approval']['state']='awaiting_decision'
            elif case=='same-generation':e['generation']=3
            elif case=='missing-approval':task['approval']=None
            elif case=='missing-pending':task['receipts'].pop(0)
            elif case=='duplicate-pending':task['receipts'].insert(0,copy.deepcopy(task['receipts'][0]))
            elif case=='source':e['source']['message_id']='foreign'
            elif case=='question':task['result']['approval_request']['question_id']='foreign'
            elif case=='link-result':e['question_link']['result_sha256']='0'*64
            elif case=='link-identity':e['question_link']['link_id']='0'*64
            elif case=='pending-result':task['result']['summary']='rewritten'
            elif case=='pending-event':task['events'][1]['detail']='rewritten'
            elif case=='approval-actor':task['events'][2]['actor']='foreign'
            elif case=='approval-event':task['events'][2]['detail']='{}'
            elif case=='approval-time':task['approval']['recorded_at']=8.0
            elif case=='missing-claim':task['events'].pop(3)
            elif case=='foreign-claim':task['events'][3]['actor']='foreign'
            elif case=='claim-generation':task['events'][3]['detail']='Step 1; generation 9; resources []'
            elif case=='current-wait':task['events'][-1]['detail']='{}'
            elif case in ('later-question','unsupported-event'):
                task['events'].append({'id':8,'at':9.2,'task_id':task['id'],'actor':'mini',
                    'kind':'pending_external_approval' if case=='later-question' else 'reconciled','detail':'new hold'})
            elif case=='current-report-question':docs['worker-report.json']['approval_request']={'question':'new'}
            elif case=='current-wait-question':docs['wait-receipt.json']['approval_request']={'question':'new'}
            elif case=='current-human-stop':task['human_stop']=True
            elif case=='current-action':docs['worker-report.json']['human_action_required']=True
            elif case=='historical-unknown':task['result']['cleanup']['unknown_operations']=['old operation']
            elif case=='intermediate-question':task['receipts'][1]['receipt']['receipt']['approval_request']={'question':'unresolved'}
            elif case=='intermediate-unknown':task['receipts'][1]['receipt']['receipt']['unknown_operations']=['old operation']
            if case!='stale-readback':docs['task-readback.json']=copy.deepcopy(task)
            else:docs['task-readback.json']['events']=[]
            with self.subTest(case=case),self.assertRaises(ValueError):proof(args)
            with self.subTest(reduce=case),self.assertRaises(ValueError):reduce(args)

    def test_question_classification_never_clears_cleanup_or_resource_uncertainty(self):
        for name,value in [('unknown_operations',['input']),('unsettled_operations',['server']),
                           ('unresolved_observations',[{'pid':99,'executable':None}])]:
            args=fixture(True);cleanup=args[2]['worker-report.json']['cleanup'];cleanup[name]=value
            self.assertEqual('historical-approved-question',proof(args)['classification'])
            with self.subTest(name=name),self.assertRaises(ValueError):reduce(args)
        args=fixture(True);args[2]['wait-receipt.json']['cleanup']={
            'unresolved_observations':[{'pid':99,'executable':None}]}
        with self.assertRaises(ValueError):reduce(args)

    def test_current_original_hold_markers_are_not_ignored(self):
        for name in ('worker-report.json','wait-receipt.json','finish-response.json','task-readback.json'):
            for marker in ('human_stop','approval_request','human_action_required'):
                args=fixture(True);args[2][name][marker]={'question':'new'} if marker=='approval_request' else True
                with self.subTest(name=name,marker=marker),self.assertRaises(ValueError):reduce(args)
        args=fixture(True);args[2]['task-readback.json']['generation']=4
        with self.assertRaises(ValueError):proof(args)
        args=fixture(True);args[0]['run_id']='foreign'
        with self.assertRaises(ValueError):proof(args)

    def test_supported_local_recovery_is_additive_and_full_history_races_reject(self):
        for race in (False,True):
            args=fixture(True);run,task,docs=args
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve();d=root/run['run_id'];d.mkdir()
                for name,value in docs.items():adapter.save(d/name,value)
                adapter.save(d/'state.json',{**run,'created_at':1.0})
                adapter.save(root/'config.json',{'worker':'mini','hostname':'fixture'})
                originals={p.name:p.read_bytes() for p in d.iterdir()}
                newer=copy.deepcopy(task)
                if race:newer['events'].append({'id':8,'at':9.2,'task_id':task['id'],'actor':'mini','kind':'claimed','detail':'later'})
                client=Mock();client.config={'worker':'mini'};client.request.side_effect=[task,newer]
                with patch.object(adapter,'ROOT',root),patch.object(adapter,'CONFIG',root/'config.json'), \
                     patch.object(adapter.socket,'gethostname',return_value='fixture'), \
                     patch.object(adapter,'Client',return_value=client), \
                     patch('coordinator.cli_messages.closeout',return_value=[]), \
                     patch.object(adapter,'spawn') as spawn,patch.object(adapter,'rpc_account') as account, \
                     patch.object(adapter.subprocess,'Popen') as process, \
                     patch('sys.argv',['worker','reconcile-result','--run-id',run['run_id']]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if race:
                        with self.assertRaises(ValueError):adapter.main()
                    else:adapter.main()
                spawn.assert_not_called();account.assert_not_called();process.assert_not_called()
                for name,raw in originals.items():
                    if name!='state.json' or race:self.assertEqual(raw,(d/name).read_bytes())
                if race:self.assertFalse((d/'run-result.json').exists())
                else:
                    after=json.loads((d/'state.json').read_text());self.assertEqual('settled',after['status'])
                    self.assertFalse(after['completion_verified'])
                    journal=json.loads((d/'run-result.json').read_text())
                    self.assertEqual({n:hashlib.sha256(originals[n]).hexdigest() for n in docs},journal['evidence_sha256'])
