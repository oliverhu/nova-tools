"""Bounded evidence prose does not weaken lifecycle identity or settlement proof."""
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
from coordinator.cli_results import cleanup_evidence_text, normalize_report
from test_cli_results import fixture, reduce
from test_cli_wait_settlement import fixture as wait_fixture


EVIDENCE = ['Owned compiler returned and direct child was reaped.',
            'No input, GUI helper, server or unknown operation remains.']


def with_evidence(outcome='blocked', evidence=None, legacy=False):
    run, task, docs = fixture(outcome)
    report = docs['worker-report.json']
    report['cleanup']['evidence'] = copy.deepcopy(EVIDENCE if evidence is None else evidence)
    if legacy:
        del report['result_schema']; del report['run_id']
    task['result'] = copy.deepcopy(report)
    task['receipts'][0]['receipt']['receipt'] = copy.deepcopy(report)
    docs['finish-response.json'] = {k:copy.deepcopy(v) for k,v in task.items() if k != 'receipts'}
    docs['task-readback.json'] = copy.deepcopy(task)
    return run,task,docs


class EvidenceLifecycleTests(unittest.TestCase):
    def test_lists_and_strings_preserve_versioned_and_legacy_originals_and_outcomes(self):
        for legacy in (False, True):
            for outcome in ('completed', 'blocked', 'pending_external_approval'):
                for evidence in (EVIDENCE, EVIDENCE[0]):
                    args=with_evidence(outcome,evidence,legacy);before=copy.deepcopy(args)
                    result=reduce(*args)
                    self.assertEqual('available',result.availability)
                    self.assertEqual(outcome,result.outcome)
                    self.assertEqual(outcome=='completed',result.acceptance_complete)
                    self.assertEqual(before,args)
                    normalized=normalize_report(args[2]['worker-report.json'],args[0])
                    self.assertEqual('\n'.join(evidence) if isinstance(evidence,list) else evidence,
                                     normalized.cleanup_evidence)
        self.assertEqual('x'*20000,cleanup_evidence_text('x'*20000))  # Existing string contract.
        self.assertEqual('\n'.join(['x']*32),cleanup_evidence_text(['x']*32))
        self.assertEqual('x'*16384,cleanup_evidence_text(['x'*16384]))

    def test_missing_empty_malformed_and_unbounded_lists_refuse(self):
        for evidence in (None, '', ' \n ', [], [''], [' \t '], ['valid',None], ['valid',False],
                         [1], [['nested']], {'evidence':'prose'}, ['x']*33, ['x'*16385],
                         ['é'*8193], ['x'*8192,'x'*8192]):
            args=with_evidence();args[2]['worker-report.json']['cleanup']['evidence']=evidence
            with self.subTest(evidence_type=type(evidence).__name__),self.assertRaises(ValueError):reduce(*args)
        args=with_evidence();del args[2]['worker-report.json']['cleanup']['evidence']
        with self.assertRaises(ValueError):reduce(*args)

    def test_valid_lists_do_not_clear_missing_predicates_unknowns_holds_or_identity(self):
        for where,delta in [('run',{'cli_exit_code':None}),('run',{'unreceived_message_ids':['unread']}),
                            ('task',{'generation':2}),('task',{'owner':'foreign'}),
                            ('task',{'receipts':[]}),('task',{'human_stop':True}),
                            ('report',{'worker':'foreign'}),('report',{'generation':2}),
                            ('report',{'approval_request':{'question':'original'}}),
                            ('cleanup',{'execution_stopped':False}),('cleanup',{'external_state_checked':False}),
                            ('cleanup',{'input_settled':False}),('cleanup',{'unknown_operations':['input']}),
                            ('cleanup',{'unsettled_operations':['server']}),('cleanup',{'human_stop':True})]:
            args=with_evidence();run,task,docs=args
            {'run':run,'task':task,'report':docs['worker-report.json'],
             'cleanup':docs['worker-report.json']['cleanup']}[where].update(delta)
            with self.subTest(where=where,delta=delta),self.assertRaises(ValueError):reduce(*args)
        for key in ('execution_stopped','external_state_checked','input_settled',
                    'unknown_operations','unsettled_operations'):
            args=with_evidence();del args[2]['worker-report.json']['cleanup'][key]
            with self.subTest(missing=key),self.assertRaises(ValueError):reduce(*args)

    def test_real_wait_binding_and_public_recovery_preserve_raw_list_and_hashes(self):
        run,task,response,note,worker,report,wait=wait_fixture()
        report.update(result_schema=1,run_id=run['run_id'],outcome='waiting',cleanup={
            'execution_stopped':True,'external_state_checked':True,'input_settled':True,
            'unknown_operations':[],'unsettled_operations':[],'evidence':EVIDENCE[:]})
        docs={'worker-report.json':report,'finish-response.json':response,
              'task-readback.json':copy.deepcopy(task),'wait-receipt.json':wait}
        self.assertEqual('waiting',reduce(run,task,docs).outcome)
        for case in ('apply','stale-api','unreceived','mismatched-wait','symlink'):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve();directory=root/run['run_id'];directory.mkdir()
                prior={**run,'created_at':1.0};adapter.save(directory/'state.json',prior)
                for name,value in docs.items():adapter.save(directory/name,value)
                adapter.save(root/'config.json',{'worker':worker,'hostname':'fixture'})
                current=copy.deepcopy(task)
                if case=='mismatched-wait':adapter.save(directory/'wait-receipt.json',{**wait,'evidence':'unbound'})
                if case=='symlink':
                    path=directory/'worker-report.json';path.rename(root/'elsewhere.json');path.symlink_to(root/'elsewhere.json')
                originals={p.name:p.read_bytes() for p in directory.iterdir()}
                client=Mock();client.config={'worker':worker}
                client.request.side_effect=[current,{**current,'generation':2} if case=='stale-api' else current]
                with patch.object(adapter,'ROOT',root),patch.object(adapter,'CONFIG',root/'config.json'), \
                     patch.object(adapter.socket,'gethostname',return_value='fixture'), \
                     patch.object(adapter,'Client',return_value=client), \
                     patch('coordinator.cli_messages.closeout',return_value=['unread'] if case=='unreceived' else []), \
                     patch.object(adapter,'spawn') as spawn,patch.object(adapter,'rpc_account') as account, \
                     patch.object(adapter.subprocess,'Popen') as process, \
                     patch('sys.argv',['worker','reconcile-result','--run-id',run['run_id']]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if case=='apply':adapter.main()
                    else:
                        with self.assertRaises(ValueError):adapter.main()
                spawn.assert_not_called();account.assert_not_called();process.assert_not_called()
                self.assertTrue(all(c.args==('/api/tasks/'+task['id'],) for c in client.request.call_args_list))
                for name,b in originals.items():
                    if name!='state.json' or case!='apply':self.assertEqual(b,(directory/name).read_bytes())
                if case=='apply':
                    final=json.loads((directory/'state.json').read_text());self.assertEqual('settled',final['status'])
                    self.assertFalse(final['completion_verified']);self.assertEqual('available',final['availability'])
                    result=json.loads((directory/'run-result.json').read_text())
                    self.assertEqual('waiting',result['outcome']);self.assertFalse(result['acceptance_complete'])
                    self.assertEqual({n:hashlib.sha256(originals[n]).hexdigest() for n in docs},result['evidence_sha256'])
                    self.assertIsInstance(json.loads((directory/'worker-report.json').read_text())['cleanup']['evidence'],list)
                else:self.assertFalse((directory/'run-result.json').exists())
