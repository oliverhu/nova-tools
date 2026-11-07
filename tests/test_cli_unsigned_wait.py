"""A compact original report is acceptable only with full bound no-effects proof."""
import copy
import contextlib
import fcntl
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from coordinator import cli_dispatch as adapter
from coordinator.cli_unsigned_wait import SCOPE
from test_cli_approved_wait_settlement import fixture


def packet(root):
    args = fixture()
    run, task, finish, note, worker, report, _, readback = args
    directory = root/run['run_id']; directory.mkdir()
    proof = dict(execution_stopped=True, external_state_checked=True,
        all_owned_test_invocations_reaped=True, no_matching_processes_observed=True,
        original_pins_unchanged=True, original_strict_signature_exit=0,
        exact_host_and_inert_census_rows=[], original_hashes={'original': 'a'*64},
        signing_performed=False, active_bundle_changed=False, actual_permission_probe_performed=False,
        CEF_or_server_or_input_started=False, staging_command_executed=False, source_checkout_preserved='/source')
    source = dict(checkout='/source', branch='repair', base='a'*40, commits=[])
    result = task['result']
    result.update(cleanup=proof, source=source, publication_performed=False, evidence_paths=[str(directory/n) for n in (
        'stopped-cleanup.json', 'approval-rejection.json')], approval_boundary={
            'kind': 'automatic_review_rejection', 'executed': False, 'reason': 'Original immutable risk'})
    report.update(publication_performed=False, next_task_claimed=False, summary='A shorter truthful original summary', source={**source, 'state': 'three source files staged, no integration'},
        evidence_paths={k: str(directory/n) for k,n in (
            ('stopped-cleanup','stopped-cleanup.json'), ('approval-rejection','approval-rejection.json'),
            ('finish-receipt','finish-receipt.json'), ('finish-response','finish-response.json'),
            ('task-readback','task-readback.json'))}, cleanup={
                'execution_stopped': True, 'owned_tests_reaped': True,
                'matching_host_and_inert_processes_observed': 0,
                'original_host_pins_and_signature_unchanged': True, 'source_and_all_history_retained': True})
    for obj in (finish, readback): obj['result'] = copy.deepcopy(result)
    for obj in (task, readback): obj['receipts'][-1]['receipt']['receipt'] = copy.deepcopy(result)
    evidence = task['approval']['evidence']; evidence.update(scope=SCOPE, response='approve')
    task['events'][-1]['detail'] = json.dumps(evidence)
    note['unsigned_preparation_approval'] = copy.deepcopy(evidence)
    rejection = dict(action='prepare-entry.py through claim watchdog', executed=False,
        reason='Original immutable risk', acceptance_complete=False,
        binding='Do not bypass rejection; no signing, update, installation or exact-context probe attempted')
    values = dict(zip(('state.json','finish-response.json','worker-report.json','task-readback.json'),
        (run,finish,report,readback)))
    values.update({'finish-receipt.json': result, 'stopped-cleanup.json':proof, 'approval-rejection.json':rejection})
    for name,value in values.items(): adapter.save(directory/name, value)
    return args, values


class UnsignedWaitTests(unittest.TestCase):
    def test_full_proof_preserves_distinct_summaries_and_every_original_byte(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve(); args,values=packet(root); directory=root/args[0]['run_id']
            originals={p:p.read_bytes() for p in directory.iterdir()}
            with patch.object(adapter,'ROOT',root): proof=adapter.validate_settlement(*args)
            adapter.settle_approved_wait(directory,args[0],args[3],args[2],args[5],args[7],cleanup_proof=proof)
            for p,raw in originals.items():
                if p.name != 'state.json': self.assertEqual(raw,p.read_bytes())
            audit=json.loads((directory/'dispatch-settlement.json').read_text())
            self.assertEqual(set(values),set(audit['original_evidence_sha256']))
            self.assertEqual('settled',json.loads((directory/'state.json').read_text())['status'])
            self.assertFalse(json.loads((directory/'state.json').read_text())['completion_verified'])

    def test_supported_locked_command_and_fresh_api_race(self):
        for case in ('check-only', 'apply', 'busy', 'later-run', 'api-race'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve(); args,_=packet(root)
                run,task,finish,note,worker,report,_,readback=args
                config=root/'config.json'; adapter.save(config,{'worker':worker,'hostname':'fixture'})
                note_path=root/'note.json';adapter.save(note_path,note)
                if case=='later-run':
                    adapter.save(root/('c'*32)/'state.json',{**run,'run_id':'c'*32,'created_at':2000.0})
                client=Mock();client.config={'worker':worker}
                client.request.side_effect=[task,{**task,'status':'running'} if case=='api-race' else task]
                command=['nova-cli-worker','settle','--run-id',run['run_id'],'--note-file',str(note_path)]
                if case=='check-only':command.append('--check-only')
                with (root/'worker.lock').open('a+') as lock, \
                     patch.object(adapter,'ROOT',root),patch.object(adapter,'CONFIG',config), \
                     patch.object(adapter.socket,'gethostname',return_value='fixture'), \
                     patch.object(adapter,'Client',return_value=client), \
                     patch.object(adapter,'spawn') as spawn,patch.object(adapter,'rpc_account') as account, \
                     patch.object(adapter.subprocess,'Popen') as process, \
                     patch('sys.argv',command),contextlib.redirect_stdout(io.StringIO()):
                    if case=='busy':fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    if case in ('busy','later-run','api-race'):
                        with self.assertRaises(ValueError):adapter.main()
                    else:adapter.main()
                spawn.assert_not_called();account.assert_not_called();process.assert_not_called()
                directory=root/run['run_id']
                after=json.loads((directory/'state.json').read_text())
                self.assertEqual('settled' if case=='apply' else 'needs_attention',after['status'])
                self.assertEqual(case=='apply',(directory/'dispatch-settlement.json').exists())

    def test_adversarial_files_scope_holds_and_changed_proofs_fail_closed(self):
        cases=['missing','symlink','parent-symlink','foreign-cleanup','report-conflict','signed','probe','not-reaped',
               'rejected-executed','unknown','scope','broader-authenticated-scope','missing-binding','later-claim',
               'changed-finish-receipt','wrong-reference','changed-after-validation','replaced-after-validation']
        for case in cases:
            with self.subTest(case=case),tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve(); args,values=packet(root); directory=root/args[0]['run_id']
                path=directory/'stopped-cleanup.json'
                if case=='missing': path.unlink()
                elif case=='symlink':
                    path.rename(root/'other.json');path.symlink_to(root/'other.json')
                elif case=='parent-symlink':
                    directory.rename(root/'elsewhere');directory.symlink_to(root/'elsewhere',target_is_directory=True)
                elif case=='foreign-cleanup': adapter.save(path,{'task':'foreign'})
                elif case=='report-conflict': args[5]['cleanup']['owned_tests_reaped']=False;adapter.save(directory/'worker-report.json',args[5])
                elif case in ('signed','probe','not-reaped'):
                    key={'signed':'signing_performed','probe':'actual_permission_probe_performed','not-reaped':'all_owned_test_invocations_reaped'}[case]
                    values['stopped-cleanup.json'][key]=case!='not-reaped';adapter.save(path,values['stopped-cleanup.json'])
                elif case=='rejected-executed':
                    values['approval-rejection.json']['executed']=True;adapter.save(directory/'approval-rejection.json',values['approval-rejection.json'])
                elif case=='unknown':
                    values['approval-rejection.json']['unknown_operations']=['input'];adapter.save(directory/'approval-rejection.json',values['approval-rejection.json'])
                elif case=='scope': args[3]['unsigned_preparation_approval']['scope']='signing too'
                elif case=='broader-authenticated-scope':
                    args[1]['approval']['evidence']['scope']='signing too'
                    args[1]['events'][-1]['detail']=json.dumps(args[1]['approval']['evidence'])
                    args[3]['unsigned_preparation_approval']=copy.deepcopy(args[1]['approval']['evidence'])
                elif case=='missing-binding': args[3].pop('unsigned_preparation_approval')
                elif case=='later-claim': args[1]['events'].append({'kind':'claimed'})
                elif case=='changed-finish-receipt': adapter.save(directory/'finish-receipt.json',{'execution_stopped':True})
                elif case=='wrong-reference':
                    args[5]['evidence_paths']['stopped-cleanup']=str(root/'other.json');adapter.save(directory/'worker-report.json',args[5])
                with patch.object(adapter,'ROOT',root):
                    if case.endswith('after-validation'):
                        proof=adapter.validate_settlement(*args)
                        if case=='changed-after-validation': path.write_text('{}')
                        else:
                            raw=path.read_bytes();path.rename(root/'old.json');path.write_bytes(raw)
                        with self.assertRaises(ValueError):
                            adapter.settle_approved_wait(directory,args[0],args[3],args[2],args[5],args[7],cleanup_proof=proof)
                        self.assertFalse((directory/'dispatch-settlement.json').exists())
                    else:
                        with self.assertRaises((ValueError,FileNotFoundError)): adapter.validate_settlement(*args)

if __name__=='__main__': unittest.main()
