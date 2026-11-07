import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from test_cli_wait_settlement import fixture
from coordinator import cli_dispatch as a


def setup(root):
    args=list(fixture());run,task,finish,note,worker,report,wait=args
    task.update(approval=None, attempts_host=worker, reassignment_count=0, attempt_allowance={}, attempt_exception={})
    run['started_at']=2.;rd=root/run['run_id'];rd.mkdir();session=root/'runtime-sessions'/'s';session.mkdir(parents=True)
    flags={'resourceReleaseAuthorized':False,'featureCleanupState':'unknown','uiEvidenceState':'unknown'}
    native={'engineExited':True,'engineExitCode':0,'featureAcceptance':False,'nonce':'nonce','pid':99}
    exits={k:{'normalExit':True,'exitCode':78,'signal':None} for k in ('host','driver')}
    facts={k:True for k in ('native_process_reaped','server_stopped','final_census_clean','controller_reaped','host_driver_absent','watchdog_returned','original_deadlines_preserved','no_cleanup_signals')}
    facts.update(native_exit=native,host_driver_kernel_exits=exits,opaque_processes=[],owned_remaining=[],controller_group_remaining=[])
    report.pop('execution_stopped');report['cleanup']=facts;report['evidence_paths']=[str(rd/'stopped-cleanup.json')]
    stop={'task':task['id'],'generation':1,'worker':worker,'execution_stopped':True,'external_state_checked':True,
          'facts':facts,'evidence':str(rd/'runtime-failure-and-cleanup.json'),'executor_conservative_status_preserved':flags}
    failure={'cleanup':facts,'executor_conservative_status':flags,'acceptance_complete':False,
             'error':{'message':'Fixture readiness deadline exceeded'},'actual_results':{k:False for k in ('fixture_ready','native_input_dispatched','capture_performed','renderer_helper_qualification')}}
    files={rd/'stopped-cleanup.json':stop,rd/'runtime-failure-and-cleanup.json':failure,rd/'runtime-observation.json':{'session':str(session)},
           session/'executor-return.json':{'reaped':True},session/'host-admission.json':{'qualified':True},
           session/'prepared/run/outcome.json':{**flags,'runNonce':'nonce','remainingControllerGroup':[],'controllerExitedAndReaped':True},
           session/'prepared/run/qualification-result.json':{'nativeExitReceipt':native,'acceptanceComplete':False,'featureAcceptance':False,
             'taskID':task['id'],'generation':1,'worker':worker,'executionStopped':True,'nativeProcessReaped':True,'serverStopped':True},
           session/'prepared/run/consumption.json':{'target':{'pid':99},'claims':{'resourceReleaseAuthorized':False}},
           session/'prepared/run/historical-join.json':{'currentResponsibilityClaimed':False,'historicalCapturedIntervalOnly':True,'claims':{'resourceReleaseAuthorized':False}},
           session/'prepared/run/final-census.json':{'owned':[]},session/'prepared/run/observer-return.json':{'reaped':True}}
    for p,v in files.items():a.save(p,v)
    sources={root/'controller.py':b'# fixture controller',root/'ui_test_runner_executor.py':b'# fixture executor'}
    for p,b in sources.items():p.write_bytes(b)
    def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
    identity={'taskID':task['id'],'generation':1,'dispatchRunID':run['run_id'],'resourceReservationID':'own-claim-'+task['id']+'-generation-1'}
    review={**identity,'kind':'exact-run-resource-ui-review','recordBindingsVerified':True,'allRecordedEvidenceHashesMatch':True,
            'executorOriginalFlags':flags,'uiEvidence':{'classification':'not-reached-before-input-or-capture','featureAcceptance':False,'fixtureReadiness':'failed-original-35-second-bound'},
            'positiveClosure':{k:True for k in ('exactNativeGenerationAbsentInFinalCensus','hostAndDriverAbsent','metadataSettlementVerified','nativeAppAndEngineNormalExit','nativeReaped','observerNormallyReaped','ownedControllerGroupReaped','serverStopped')},
            'runtimeSessionID':'s','runNonce':'nonce','sourceControllerSHA256':sha(root/'controller.py'),'sourceExecutorSHA256':sha(root/'ui_test_runner_executor.py'),
            'evidenceSHA256':{str(p):sha(p) for p in [*files,*sources]}}
    review['positiveClosure']['hostAndDriverNormalExit78']=exits
    a.save(root/'review.json',review)
    interpretation={**identity,'rawFieldsMustRemainUnchanged':flags,'reviewRef':{'sha256':sha(root/'review.json')},
                    'localWorkerAdmissionMayBeSettled':True,'separatePositiveDesktopReleaseRequired':False,
                    'runtimeActionPerformed':False,'sourceOrQueueMutationPerformed':False}
    a.save(root/'interpretation.json',interpretation)
    note['owner_review']={'review_path':str(root/'review.json'),'review_sha256':sha(root/'review.json'),
                          'interpretation_path':str(root/'interpretation.json'),'interpretation_sha256':sha(root/'interpretation.json')}
    args.append(copy.deepcopy(task))
    return args


class OwnerReviewedWaitTests(unittest.TestCase):
    def test_positive_owner_proof_does_not_clear_original_flags_or_report(self):
        with tempfile.TemporaryDirectory() as d,patch.object(a,'ROOT',Path(d).resolve()):
            args=setup(a.ROOT);before=copy.deepcopy(args);proof=a.validate_settlement(*args)
            self.assertEqual(before,args);self.assertTrue(proof['closure']['execution_stopped'])
            self.assertEqual('unknown',proof['flags_preserved']['uiEvidenceState']);self.assertFalse(proof['flags_preserved']['resourceReleaseAuthorized'])

    def test_drift_contradictions_unknowns_and_later_claims_refuse(self):
        for case in ('report-false','unknown','generation','later-history','review-hash','review-replaced','interpretation-hash','stopped-symlink','source-change','nonce-change'):
            with tempfile.TemporaryDirectory() as d,patch.object(a,'ROOT',Path(d).resolve()):
                args=setup(a.ROOT);run,task,finish,note,worker,report,wait,readback=args
                if case=='report-false':report['execution_stopped']=False
                elif case=='unknown':note['unknown_operations']=['unresolved input']
                elif case=='generation':task['generation']=2
                elif case=='later-history':task['events'].append({'kind':'claimed'})
                elif case=='review-hash':note['owner_review']['review_sha256']='0'*64
                elif case=='interpretation-hash':note['owner_review']['interpretation_sha256']='0'*64
                elif case=='review-replaced':a.save(a.ROOT/'review.json',{'different':True})
                elif case=='stopped-symlink':
                    p=a.ROOT/run['run_id']/'stopped-cleanup.json';p.rename(p.with_suffix('.old'));p.symlink_to(p.with_suffix('.old'))
                elif case=='source-change':(a.ROOT/'controller.py').write_text('changed')
                else:
                    p=a.ROOT/'runtime-sessions/s/prepared/run/outcome.json';v=json.loads(p.read_text());v['runNonce']='changed';a.save(p,v)
                with self.subTest(case=case),self.assertRaises(ValueError):a.validate_settlement(*args)

    def test_check_only_is_read_only_then_settle_preserves_failed_evidence(self):
        with tempfile.TemporaryDirectory() as d,patch.object(a,'ROOT',Path(d).resolve()):
            args=setup(a.ROOT);run,task,finish,note,worker,report,wait,readback=args;rd=a.ROOT/run['run_id']
            for name,v in [('state.json',run),('finish-response.json',finish),('worker-report.json',report),('wait-receipt.json',wait),('task-readback.json',readback)]:a.save(rd/name,v)
            a.save(a.ROOT/'config.json',{'worker':worker,'hostname':'fixture'});a.save(a.ROOT/'note.json',note)
            originals={p:p.read_bytes() for p in a.ROOT.rglob('*') if p.is_file()}
            client=Mock();client.config={'worker':worker};client.request.return_value=task
            with patch.object(a,'CONFIG',a.ROOT/'config.json'),patch.object(a,'Client',return_value=client),patch.object(a.socket,'gethostname',return_value='fixture'),patch.object(a,'spawn') as spawn,patch.object(a,'rpc_account') as account:
                for check in (True,False):
                    argv=['nova-cli-worker','settle','--run-id',run['run_id'],'--note-file',str(a.ROOT/'note.json')]+(['--check-only'] if check else [])
                    out=io.StringIO()
                    with patch('sys.argv',argv),contextlib.redirect_stdout(out):a.main()
                    result=json.loads(out.getvalue());self.assertEqual(not check,result['settled_for_other_work'])
                    for p,b in originals.items():
                        if check or p!=rd/'state.json':self.assertEqual(b,p.read_bytes())
                    if check:self.assertFalse((rd/'dispatch-settlement.json').exists())
            spawn.assert_not_called();account.assert_not_called();self.assertEqual(2,client.request.call_count)
            state=json.loads((rd/'state.json').read_text());self.assertEqual('failed',state['settlement_outcome']);self.assertFalse(state['completion_verified'])
            self.assertEqual(originals[rd/'state.json'],(rd/'owner-reviewed-failed-wait-state-before.json').read_bytes())
            audit=json.loads((rd/'dispatch-settlement.json').read_text());self.assertFalse(audit['acceptance_complete']);self.assertTrue(audit['owner_review_evidence_sha256'])
