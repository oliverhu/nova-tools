import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from test_cli_owner_review import setup as original_setup
from coordinator import cli_dispatch as a


def setup(root):
    args=original_setup(root);run,task,finish,note,worker,report,wait,readback=args;rd=root/run['run_id'];prepared=root/'runtime-sessions/s/prepared'
    flags={'resourceReleaseAuthorized':False,'featureCleanupState':'unknown','uiEvidenceState':'unknown'}
    identity={'pid':99,'uid':501,'birth':[100,5],'executable':'/owned/fixture'};native={'engineExited':True,'engineExitCode':0,'featureAcceptance':False,'nonce':'nonce','pid':99}
    exits={k:{'normalExit':True,'exitCode':78,'signal':None} for k in ('host','driver')}
    stop={k:True for k in ('execution_stopped','external_state_checked','nativeProcessReaped','serverStopped','finalExactNamespaceCensusClean','normalCleanup','executorReaped','controllerExitedAndReaped','outerOpenReaped','initialObservationPixelsCaptured','unknownFlagsRetained','consumedNonceNotReplayed')}
    stop.update(task=task['id'],generation=1,worker=worker,runtimeSession=str(prepared.parent),nativeExitReceipt=native,hostDriverKernelExits=exits,hostAfter=None,driverAfter=None,nativeInputDispatched=False,postNavigationCaptureProduced=False,resourceReleaseAuthorized=False,commonExecutorFeatureCleanupState='unknown')
    stop.update({k:[] for k in ('finalOwned','finalOpaque','signals','executorProcessGroupMembers','supervisorChildren')})
    report.update(cleanup=stop,source_commit_used='c'*40)
    matches=[{'id':'link','ax_role':'AXLink','is_actionable':True,'is_enabled':True},{'id':'text','ax_role':'AXStaticText','is_actionable':False,'is_enabled':True}]
    error={'type':'RuntimeError','message':'Unique enabled authored link unavailable'}
    failure={'session':str(prepared.parent),'cleanup':str(rd/'stopped-cleanup.json'),'acceptance_complete':False,'error':error,'matchingElements':matches}
    docs={'run/qualification-result.json':{'taskID':task['id'],'generation':1,'worker':worker,'acceptanceComplete':False,'featureAcceptance':False,'executionStopped':True,'nativeExitReceipt':native,'error':error},
          'run/owner-result.json':{},'run/consumption.json':{'claims':{'resourceReleaseAuthorized':False}},
          'run/historical-join.json':{'currentResponsibilityClaimed':False,'historicalCapturedIntervalOnly':True,'claims':{'resourceReleaseAuthorized':False}},
          'run/settlement.json':{'claims':{'resourceReleaseAuthorized':False}},'run/outcome.json':{**flags,'runNonce':'nonce','controllerExitedAndReaped':True,'remainingControllerGroup':[]},
          'run/final-census.json':{},'run/native-readiness.json':{'nativeIdentity':identity},'run/input/binding.json':{'identity':identity,'windowID':42}}
    ledger=[]
    for index,(label,operation) in enumerate([('navigate-before','see'),('navigate-window','window')]):
        pid=900+index;cmd=['/tools/peekaboo',operation]+(['list'] if operation=='window' else [])+['--pid','99']+(['--window-id','42'] if operation=='see' else [])+['--json','--no-remote']
        prefix='run/input/'+label+'.'
        docs[prefix+'intent.json']={'command':cmd};docs[prefix+'process.json']={'pid':pid,'privateProcessGroup':pid}
        docs[prefix+'result.json']={'pid':pid,'accepted':True,'exit':0,'waited':True,'execution_stopped':True,'error':None,'phase':'observation','operation':operation}
        docs[prefix+'stdout.json']={'success':True,'data':{'target_application_info':{'pid':99},'windows':[{'window_id':42}]}} if operation=='window' else {'success':True,'target_receipt':{'pid':99,'window_id':42,'process_start_identity_decimal':'100000005'},'data':{'screenshot_raw':'','screenshot_annotated':'','observation':{'target':{'window_id':42},'spans':[{'name':'capture.window','metadata':{'engine':'CGWindowList'}}]}}}
        ledger.append({'label':label,'operation':operation,'pid':pid,'accepted':True,'exit':0,'waited':True,'phase':'observation','freshReadOnlyPrivateGroupMembers':[]})
    sources={'snapshot/.build/qualification/controller.py':b'# fixture controller','snapshot/scripts/ui_test_runner_executor.py':b'# fixture executor','snapshot/scripts/peekaboo_bounded_click.py':b'# fixture reader'}
    def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
    for rel,b in sources.items():p=prepared/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b)
    spec={'taskID':task['id'],'runNonce':'nonce','runRoot':str(prepared/'run'),'controller':{'path':str(prepared/'snapshot/.build/qualification/controller.py'),'sha256':sha(prepared/'snapshot/.build/qualification/controller.py')},'executor':{'path':str(prepared/'snapshot/scripts/ui_test_runner_executor.py'),'sha256':sha(prepared/'snapshot/scripts/ui_test_runner_executor.py')}}
    a.save(prepared/'spec.json',spec);docs['run/outcome.json']['spec']={'path':str(prepared/'spec.json'),'sha256':sha(prepared/'spec.json')}
    for rel,v in docs.items():a.save(prepared/rel,v)
    stderr=prepared/'run/controller.stderr';stderr.write_bytes(b'fixture selector refusal')
    for name,v in [('worker-report.json',report),('stopped-cleanup.json',stop),('runtime-failure-and-cleanup.json',failure)]:a.save(rd/name,v)
    positive={k:True for k in ('nativeAppAndEngineNormalExit0','nativeReaped','allNineHistoricalAppHelperGenerationsAbsent','serverStopped','observerNormallyReaped','metadataSettlementVerified','ownedControllerGroupReaped','hostDriverNormalExit78AndAbsent','executorAndOuterOpenReaped')};positive['observationLedger']=ledger
    review={'kind':'exact-run-resource-ui-review','taskID':task['id'],'generation':1,'dispatchRunID':run['run_id'],'resourceReservationID':'own-claim-'+task['id']+'-generation-1','runtimeSessionID':'s','runNonce':'nonce','sourceCommit':'c'*40,'recordBindingsVerified':True,'recordedEvidenceHashesMatch':True,'verificationCommandExitCode':0,'executorOriginalFlags':flags,'localWorkerAdmissionMayBeSettled':True,'separatePositiveDesktopLeaseReleaseRequired':False,'interpretation':'Exact independent failed local closure only','positiveClosure':positive,
            'uiEvidence':{'classification':'read-and-window-capture-completed-selector-refused-before-native-input','exactOwnedWindowID':42,'selectorMatches':[{'id':e['id'],'role':e['ax_role'],'actionable':e['is_actionable'],'enabled':True} for e in matches],**{k:False for k in ('nativeInputDispatched','clickIntentExists','navigationReceiverObserved','postNavigationPNGExists','featureAcceptance')}},
            'manifest':{'preparedRoot':str(prepared),'sha256':{rel:sha(prepared/rel) for rel in ['spec.json',*sources,*docs,'run/controller.stderr']},'dispatchReceiptSHA256':{n:sha(rd/n) for n in ['worker-report.json','stopped-cleanup.json','runtime-failure-and-cleanup.json']}}}
    a.save(root/'review.json',review);note['owner_review']={'review_path':str(root/'review.json'),'review_sha256':sha(root/'review.json')}
    return args,prepared,review


class ObservedReviewTests(unittest.TestCase):
    def test_truthful_observations_and_initial_capture_are_preserved(self):
        with tempfile.TemporaryDirectory() as d,patch.object(a,'ROOT',Path(d).resolve()):
            args,prepared,review=setup(a.ROOT);before=copy.deepcopy(args);proof=a.validate_settlement(*args)
            self.assertEqual(before,args);self.assertEqual(2,len(review['positiveClosure']['observationLedger']));self.assertFalse(proof['flags_preserved']['resourceReleaseAuthorized'])

    def test_unknown_operations_effects_nonce_source_and_identity_refuse(self):
        for case in ('third-intent','hold','post-png','source','nonce','receiver','native-click','pid','window-close','capture','later-claim','group','human-stop','approval-request','other-unknown'):
            with tempfile.TemporaryDirectory() as d,patch.object(a,'ROOT',Path(d).resolve()):
                args,p,r=setup(a.ROOT)
                if case=='third-intent':a.save(p/'run/input/click.intent.json',{})
                elif case=='hold':a.save(p/'run/input/HOLD.json',{})
                elif case=='post-png':(p/'run/input/rendered-after.png').write_bytes(b'not accepted')
                elif case=='later-claim':args[1]['events'].append({'kind':'claimed'})
                elif case=='human-stop':args[3]['human_stop']=True
                elif case=='approval-request':args[3]['approval_request']={'question_id':'unresolved'}
                elif case=='other-unknown':args[1]['result']['unknown_operations']=['unreviewed effect']
                else:
                    if case in ('receiver','native-click'):r['uiEvidence']['navigationReceiverObserved' if case=='receiver' else 'nativeInputDispatched']=True
                    elif case=='nonce':r['runNonce']='other'
                    elif case=='group':r['positiveClosure']['observationLedger'][0]['freshReadOnlyPrivateGroupMembers']=[12]
                    else:
                        rel={'source':'snapshot/.build/qualification/controller.py','pid':'run/input/navigate-before.result.json','window-close':'run/input/navigate-window.intent.json','capture':'run/input/navigate-before.stdout.json'}[case]
                        if case=='source':(p/rel).write_bytes(b'changed source')
                        else:
                            data=json.loads((p/rel).read_text())
                            if case=='pid':data['pid']=88
                            elif case=='window-close':data['command'][2]='close'
                            else:data['data']['screenshot_raw']='unreviewed.png'
                            a.save(p/rel,data);r['manifest']['sha256'][rel]=hashlib.sha256((p/rel).read_bytes()).hexdigest()
                    a.save(a.ROOT/'review.json',r);args[3]['owner_review']['review_sha256']=hashlib.sha256((a.ROOT/'review.json').read_bytes()).hexdigest()
                with self.subTest(case=case),self.assertRaises(ValueError):a.validate_settlement(*args)

    def test_stale_check_fingerprint_cannot_overwrite_newer_local_state(self):
        with tempfile.TemporaryDirectory() as d,patch.object(a,'ROOT',Path(d).resolve()):
            args,p,r=setup(a.ROOT);run,task,finish,note,worker,report,wait,readback=args;rd=a.ROOT/run['run_id']
            for n,v in [('state.json',run),('finish-response.json',finish),('worker-report.json',report),('wait-receipt.json',wait),('task-readback.json',readback)]:a.save(rd/n,v)
            a.save(a.ROOT/'config.json',{'worker':worker,'hostname':'fixture'});a.save(a.ROOT/'note.json',note);client=Mock();client.config={'worker':worker};client.request.return_value=task
            base=['nova-cli-worker','settle','--run-id',run['run_id'],'--note-file',str(a.ROOT/'note.json')]
            with patch.object(a,'CONFIG',a.ROOT/'config.json'),patch.object(a,'Client',return_value=client),patch.object(a.socket,'gethostname',return_value='fixture'),patch.object(a,'spawn') as spawn,patch.object(a,'rpc_account') as account:
                out=io.StringIO()
                with patch('sys.argv',base+['--check-only']),contextlib.redirect_stdout(out):a.main()
                fingerprint=json.loads(out.getvalue())['settlement_fingerprint'];changed={**run,'model':'changed-local-admission'};a.save(rd/'state.json',changed)
                with patch('sys.argv',base+['--expected-fingerprint',fingerprint]),self.assertRaises(ValueError):a.main()
                self.assertEqual(changed,json.loads((rd/'state.json').read_text()));self.assertFalse((rd/'dispatch-settlement.json').exists())
                a.save(rd/'state.json',run)
                out=io.StringIO()
                with patch('sys.argv',base+['--check-only']),contextlib.redirect_stdout(out):a.main()
                fresh=json.loads(out.getvalue())['settlement_fingerprint']
                originals={f:f.read_bytes() for f in a.ROOT.rglob('*') if f.is_file()}
                out=io.StringIO()
                with patch('sys.argv',base+['--expected-fingerprint',fresh]),contextlib.redirect_stdout(out):a.main()
                self.assertEqual('failed',json.loads(out.getvalue())['settlement_outcome'])
                for f,raw in originals.items():
                    if f!=rd/'state.json':self.assertEqual(raw,f.read_bytes())
                self.assertEqual(originals[rd/'state.json'],(rd/'owner-reviewed-failed-wait-state-before.json').read_bytes())
                state=json.loads((rd/'state.json').read_text());self.assertFalse(state['completion_verified']);self.assertEqual('failed',state['settlement_outcome'])

            spawn.assert_not_called();account.assert_not_called()
