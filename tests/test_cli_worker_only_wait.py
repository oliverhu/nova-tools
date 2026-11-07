"""Owner and destination stay distinct in an immutable worker-only defer."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coordinator import cli_dispatch as adapter
from test_cli_wait_settlement import fixture as dependency_fixture


def fixture():
    args = list(dependency_fixture())
    run, task, response, note, worker, report, wait = args
    run['started_at'] = 2.0
    task.update(requirements={'worker': 'destination'}, dependencies=[], approval=None,
                attempts_host=worker, reassignment_count=0, attempt_exception={}, attempt_allowance={},
                steps=[{'name': 'work'}], request_key='generic-worker-handoff')
    task['events'] = [
        {'id': 10, 'at': 8.0, 'task_id': task['id'], 'actor': worker, 'kind': 'claimed',
         'detail': 'Step 1; generation 1; resources []'},
        {'id': 11, 'at': 9.1, 'task_id': task['id'], 'actor': worker, 'kind': 'waiting',
         'detail': json.dumps({'worker': 'destination', 'dependencies': [], 'evidence': wait['evidence']})}]
    response.clear(); response.update({k: copy.deepcopy(v) for k, v in task.items() if k not in ('receipts', 'events', 'approval')})
    report.update(external_state_checked=True, next_worker='destination', cleanup={
        'task': task['id'], 'generation': 1, 'worker': worker,
        'execution_stopped': True, 'external_state_checked': True, 'unknown_operations': []})
    wait.update(worker='destination'); del wait['dependencies']
    return [*args, copy.deepcopy(task)]


def phase_fixture():
    """a996 event shape: claim2443, owner phases2444-2448, wait2449."""
    args = fixture(); task = args[1]; claim, wait = task['events']
    claim['id'] = 2443; wait['id'] = 2449
    task['events'] = [claim, *[
        {'id': 2444+i, 'at': 8.1+i/10, 'task_id': task['id'], 'actor': args[4],
         'kind': 'execution-phase', 'detail': json.dumps({'generation': 1, 'phase': phase})}
        for i, phase in enumerate(('implementing', 'testing', 'implementing', 'testing', 'cleanup'))], wait]
    args[7] = copy.deepcopy(task)
    return args


class WorkerOnlyWaitTests(unittest.TestCase):
    def test_actual_progress_phase_pattern_selects_original_claim_without_changing_history(self):
        args = phase_fixture(); before = copy.deepcopy(args)
        adapter.validate_settlement(*args)
        self.assertEqual(args, before)
        self.assertEqual(adapter.worker_only_wait_claim(args[1]['events'],args[1],args[1]['receipts'][-1],args[4])['id'],2443)

    def test_phase_and_history_identity_payload_and_order_are_strict_even_with_matching_readback(self):
        for delta in ({'actor':'destination'}, {'task_id':'foreign'}, {'kind':'claimed'}, {'kind':'waiting'},
                      {'kind':'approval-applied'}, {'id':2443}, {'id':True}, {'at':float('nan')},
                      {'at':7}, {'at':9.05}, {'detail':'bad-json'},
                      {'detail':json.dumps({'generation':True,'phase':'cleanup'})},
                      {'detail':json.dumps({'generation':2,'phase':'cleanup'})},
                      {'detail':json.dumps({'generation':1,'phase':'completed'})},
                      {'detail':json.dumps({'generation':1,'phase':'cleanup','owner':'foreign'})}):
            args=phase_fixture();args[1]['events'][2].update(delta);args[7]=copy.deepcopy(args[1])
            with self.subTest(delta=delta),self.assertRaises(ValueError):adapter.validate_settlement(*args)
        args=phase_fixture();args[1]['events'][2:4]=reversed(args[1]['events'][2:4]);args[7]=copy.deepcopy(args[1])
        with self.assertRaises(ValueError):adapter.validate_settlement(*args)

    def test_unique_exact_claim_generation_step_and_owner_cannot_be_forged_or_replaced(self):
        for delta in ({'actor':'foreign'}, {'detail':'Step 1; generation 2; resources []'},
                      {'detail':'Step 2; generation 1; resources []'}, {'detail':None},
                      {'detail':'Step 1; generation 1; resources ["same", "same"]'},
                      {'detail':'Step 1; generation 1; resources [__import__("os")]'}):
            args=phase_fixture();args[1]['events'][0].update(delta);args[7]=copy.deepcopy(args[1])
            with self.subTest(delta=delta),self.assertRaises(ValueError):adapter.validate_settlement(*args)
        for operation in ('duplicate','missing','intervening','future-prefix'):
            args=phase_fixture();events=args[1]['events'];claim=copy.deepcopy(events[0])
            if operation=='duplicate':events.insert(0,{**claim,'id':2442,'at':7.9})
            elif operation=='missing':events.pop(0)
            elif operation=='future-prefix':events.insert(0,{**claim,'id':2442,'at':7.9,'detail':'Step 1; generation 2; resources []'})
            else:events[-2]={**claim,'id':2448,'at':8.5,'detail':'Step 1; generation 2; resources []'}
            args[7]=copy.deepcopy(args[1])
            with self.subTest(operation=operation),self.assertRaises(ValueError):adapter.validate_settlement(*args)

    def test_earlier_real_generation_history_does_not_replace_current_matching_claim(self):
        args=phase_fixture();task=args[1]
        task['generation']=2;args[0]['queued_generation']=1
        args[5]['generation']=2;args[5]['cleanup']['generation']=2;args[6]['generation']=2
        task['receipts'][-1]['generation']=2
        for event in task['events']:
            if event['kind']=='claimed':event['detail']='Step 1; generation 2; resources []'
            elif event['kind']=='execution-phase':
                detail=json.loads(event['detail']);detail['generation']=2;event['detail']=json.dumps(detail)
        task['events'].insert(0,{'id':2400,'at':1.1,'task_id':task['id'],'actor':'previous-owner',
            'kind':'claimed','detail':'Step 1; generation 1; resources []'})
        args[2]=copy.deepcopy({k:v for k,v in task.items() if k not in ('events','receipts','approval')});args[3]['generation']=2;args[7]=copy.deepcopy(task)
        adapter.validate_settlement(*args)

    def test_progress_does_not_waive_raw_evidence_cleanup_or_current_api_fences(self):
        for index,delta in ((0,{'cli_exit_code':1}),(1,{'owner':'destination'}),(2,{'last_owner':'foreign'}),
                            (3,{'unknown_operations':['pending']}),(5,{'acceptance_complete':True}),
                            (5,{'cleanup':{'execution_stopped':False}}),(6,{'external_state_checked':False}),
                            (7,{'receipts':[]}),(0,{'unreceived_message_ids':['unread']}),
                            (5,{'unresolved_observations':['opaque inventory']})):
            args=phase_fixture();args[index].update(delta)
            with self.subTest(index=index,delta=delta),self.assertRaises(ValueError):adapter.validate_settlement(*args)

    def test_real_store_claim_phase_and_defer_events_qualify_without_event_rewriting(self):
        from coordinator.store import Store
        from coordinator.workers import MINI_VM_WORKERS
        worker=MINI_VM_WORKERS[1];clock=[1.0]
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db',clock=lambda:clock[0])
            store.set_worker_state('admin',worker,{'paused':False,'reason':'inert history fixture'})
            store.pulse(worker,{'capacity':{'cpus':4,'memory_gb':8,'disk_gb':100}})
            task=store.submit('admin',{'request_key':'actual-phase-history','title':'Worker-only source handoff',
                'requirements':{'worker':worker},'steps':[{'name':'work','instructions':'inert fixture only'}]})
            clock[0]=8.;claim=store.claim(worker,task['id'])
            for i,phase in enumerate(('implementing','testing','implementing','testing','cleanup')):
                clock[0]=8.1+i/10
                store.heartbeat(worker,task['id'],{'claim_token':claim['claim_token'],'generation':1,'phase':phase})
            evidence='Owned metadata commands reaped; no runtime/input or unknown effect'
            wait={'execution_stopped':True,'external_state_checked':True,'worker':'oliverhu-mac-studio','evidence':evidence}
            clock[0]=9.;response=store.defer(worker,task['id'],{**wait,'claim_token':claim['claim_token'],'generation':1})
            actual=store.detail(task['id'])
            args=fixture();run=args[0];run.update(task=task['id'],worker=worker)
            note={**args[3],'task':task['id'],'worker':worker,'cleanup_evidence':evidence}
            report={'task':task['id'],'generation':1,'worker':worker,'outcome':'waiting','acceptance_complete':False,
                    'execution_stopped':True,'external_state_checked':True,'cleanup':{'execution_stopped':True}}
            before=copy.deepcopy(actual)
            adapter.validate_settlement(run,actual,response,note,worker,report,wait,copy.deepcopy(actual))
            self.assertEqual(actual,before)
            self.assertEqual([e['kind'] for e in actual['events']][-7:],['claimed']+['execution-phase']*5+['waiting'])

    def test_exact_owner_destination_and_omitted_or_empty_dependencies(self):
        args = fixture(); before = copy.deepcopy(args)
        adapter.validate_settlement(*args)
        self.assertEqual(before, args)
        args[6]['dependencies'] = []
        adapter.validate_settlement(*args)
        # Existing nonempty dependency wait path remains independently valid.
        adapter.validate_settlement(*dependency_fixture())

    def test_raw_wait_identity_can_be_omitted_but_never_contradict_authoritative_records(self):
        for omitted in (('task',), ('generation',), ('task', 'generation')):
            args = fixture()
            for key in omitted: del args[6][key]
            before = copy.deepcopy(args)
            adapter.validate_settlement(*args)
            self.assertEqual(before, args)
            for index, delta in [(0, {'task': 'foreign'}), (1, {'last_owner': 'foreign'}),
                                 (2, {'generation': 2}), (5, {'task': 'foreign'}),
                                 (5, {'generation': 2}), (7, {'generation': 2}),
                                 (6, {'task': 'foreign'}), (6, {'generation': 2}),
                                 (6, {'generation': None}), (6, {'generation': True})]:
                bad = copy.deepcopy(args); bad[index].update(delta)
                with self.subTest(omitted=omitted, index=index, delta=delta), self.assertRaises(ValueError):
                    adapter.validate_settlement(*bad)
            for target in ('receipt', 'event'):
                bad = copy.deepcopy(args)
                if target == 'receipt': bad[1]['receipts'][-1]['generation'] = 2
                else: bad[1]['events'][-1]['actor'] = 'destination'
                # Even a self-consistent copy cannot replace authoritative identity/audit.
                bad[7] = copy.deepcopy(bad[1])
                with self.subTest(omitted=omitted, target=target), self.assertRaises(ValueError):
                    adapter.validate_settlement(*bad)

    def test_wrong_owner_destination_unknown_effects_and_holds(self):
        cases = [
            (0, {'worker': 'destination'}), (0, {'completion_verified': True}),
            (0, {'cli_exit_code': 1}), (0, {'queued_generation': 1}), (0, {'ended_at': float('nan')}),
            (1, {'owner': 'destination'}), (1, {'last_owner': 'destination'}),
            (1, {'requirements': {'worker': 'foreign'}}), (1, {'requirements': {'worker': 'mini'}}),
            (1, {'dependencies': ['fake']}), (1, {'result': {'outcome': 'completed'}}),
            (1, {'approval': {'pending': True}}), (1, {'generation': 2}),
            (2, {'last_owner': 'foreign'}), (2, {'fix_attempts_used': 99}),
            (3, {'human_stop': True}), (3, {'approval_request': {'question': 'original'}}),
            (3, {'unknown_operations': ['remote input']}), (3, {'input_settled': False}),
            (5, {'worker': 'destination'}), (5, {'worker_id': 'destination'}),
            (5, {'next_worker': 'foreign'}), (5, {'generation': 2}),
            (5, {'acceptance_complete': True}), (5, {'external_state_checked': False}),
            (5, {'human_stop': True}), (5, {'unknown_operations': ['signing']}),
            (6, {'worker': 'mini'}), (6, {'worker': 'foreign'}), (6, {'worker_id': 'foreign'}),
            (6, {'dependencies': ['fake']}), (6, {'dependencies': None}),
            (6, {'external_state_checked': False}), (6, {'execution_stopped': False}),
            (6, {'approval_request': {'question': 'original'}}), (6, {'evidence': 'unbound'}),
            (7, {'events': []}), (7, {'requirements': {'worker': 'foreign'}}),
            (7, {'fix_attempts_used': 99})]
        for index, delta in cases:
            args = fixture(); args[index].update(delta)
            with self.subTest(index=index, delta=delta), self.assertRaises(ValueError):
                adapter.validate_settlement(*args)
        for key, value in [('execution_stopped', False), ('external_state_checked', False),
                           ('unknown_operations', ['helper']), ('worker', 'destination')]:
            args = fixture(); args[5]['cleanup'][key] = value
            with self.subTest(cleanup=key), self.assertRaises(ValueError): adapter.validate_settlement(*args)
        args = fixture(); args[7] = None
        with self.assertRaises(ValueError): adapter.validate_settlement(*args)

    def test_matching_originals_do_not_make_forged_or_later_history_safe(self):
        for target, delta in [
            ('event', {'actor': 'destination'}), ('event', {'kind': 'claimed'}),
            ('event', {'id': 9}), ('event', {'at': float('inf')}), ('event', {'at': 11.0}),
            ('event', {'detail': '{}'}), ('event', {'task_id': 'foreign'}),
            ('receipt', {'generation': 2}), ('receipt', {'at': 9.9}),
            ('body', {'execution_stopped': False}), ('body', {'summary': 'unbound'}),
            ('claim', {'actor': 'destination'}), ('claim', {'kind': 'waiting'})]:
            args = fixture(); t=args[1]
            obj={'event': t['events'][-1], 'claim':t['events'][-2],
                 'receipt': t['receipts'][-1], 'body':t['receipts'][-1]['receipt']['receipt']}[target]
            obj.update(delta); args[7]=copy.deepcopy(t)
            with self.subTest(target=target, delta=delta), self.assertRaises(ValueError): adapter.validate_settlement(*args)
        args = fixture(); args[1]['events'].append({'kind':'claimed', 'actor':'destination'})
        with self.assertRaises(ValueError): adapter.validate_settlement(*args)

    def test_public_check_and_apply_preserve_originals_and_reject_newer_state(self):
        for bad in (None, 'raw-input', 'later-api', 'later-local', 'symlink', 'missing-readback', 'changed-input'):
            args=fixture(); run,task,response,note,worker,report,wait,readback=args
            if bad == 'raw-input':
                del wait['task']; del wait['generation']
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve(); d=root/run['run_id']; d.mkdir()
                config=root/'config.json'; adapter.save(config, {'worker':worker,'hostname':'fixture'})
                for name,value in [('state.json',run),('finish-response.json',response),
                                   ('worker-report.json',report),('wait-receipt.json',wait),('task-readback.json',readback)]:
                    adapter.save(d/name,value)
                note_path=root/'note.json';adapter.save(note_path,note)
                originals={p.name:p.read_bytes() for p in d.iterdir()}
                client=Mock();client.config={'worker':worker};client.request.return_value=task
                argv=['nova-cli-worker','settle','--run-id',run['run_id'],'--note-file',str(note_path)]
                with patch.object(adapter,'ROOT',root),patch.object(adapter,'CONFIG',config), \
                     patch.object(adapter.socket,'gethostname',return_value='fixture'), \
                     patch.object(adapter,'Client',return_value=client),patch.object(adapter,'spawn') as spawn, \
                     patch.object(adapter,'rpc_account') as account,patch.object(adapter.subprocess,'Popen') as process:
                    with patch('sys.argv',argv+['--check-only']),contextlib.redirect_stdout(io.StringIO()) as output:
                        adapter.main()
                    self.assertTrue(json.loads(output.getvalue())['settlement_ready'])
                    self.assertEqual(originals,{p.name:p.read_bytes() for p in d.iterdir()})
                    if bad=='later-api':task['events'].append({'kind':'claimed','actor':'destination'})
                    if bad=='later-local':adapter.save(root/('c'*32)/'state.json',{**run,'run_id':'c'*32,'created_at':20.0})
                    if bad=='symlink':
                        q=root/'elsewhere.json';q.write_bytes((d/'wait-receipt.json').read_bytes());(d/'wait-receipt.json').unlink();(d/'wait-receipt.json').symlink_to(q)
                    if bad=='missing-readback':(d/'task-readback.json').unlink()
                    if bad=='changed-input':adapter.save(d/'wait-receipt.json',{**wait,'worker':'foreign'})
                    before={p.name:p.read_bytes() for p in d.iterdir()}
                    rejected = bad not in (None, 'raw-input')
                    with patch('sys.argv',argv),contextlib.redirect_stdout(io.StringIO()):
                        if rejected:
                            with self.assertRaises(ValueError):adapter.main()
                        else:adapter.main()
                    spawn.assert_not_called();account.assert_not_called();process.assert_not_called()
                self.assertTrue(all(call.args[0]=='/api/tasks/'+task['id'] for call in client.request.call_args_list))
                if rejected:self.assertEqual(before,{p.name:p.read_bytes() for p in d.iterdir()})
                else:
                    self.assertEqual(originals['state.json'],(d/'worker-only-wait-state-before.json').read_bytes())
                    self.assertEqual({**run,'status':'settled','prior_status':'needs_attention'},json.loads((d/'state.json').read_text()))
                    self.assertTrue(all((d/k).read_bytes()==v for k,v in originals.items() if k!='state.json'))
                    audit=json.loads((d/'dispatch-settlement.json').read_text());self.assertEqual('worker_only_wait',audit['operation'])
                    self.assertEqual(set(originals),set(audit['original_evidence_sha256']))

    def test_phase_handoff_public_check_apply_and_final_api_race_preserve_originals(self):
        for race in (False,True):
            run,task,response,note,worker,report,wait,readback=phase_fixture()
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve();d=root/run['run_id'];d.mkdir()
                cfg=root/'config.json';adapter.save(cfg,{'worker':worker,'hostname':'fixture'})
                for name,value in [('state.json',run),('finish-response.json',response),('worker-report.json',report),
                                   ('wait-receipt.json',wait),('task-readback.json',readback)]:adapter.save(d/name,value)
                note_path=root/'note.json';adapter.save(note_path,note)
                originals={p.name:p.read_bytes() for p in d.iterdir()}
                client=Mock();client.config={'worker':worker}
                def fresh(route):
                    value=copy.deepcopy(task)
                    if race and client.request.call_count%2==0:
                        value['events'][-2]['detail']=json.dumps({'generation':1,'phase':'implementing'})
                    return value
                client.request.side_effect=fresh
                argv=['worker','settle','--run-id',run['run_id'],'--note-file',str(note_path)]
                with patch.object(adapter,'ROOT',root),patch.object(adapter,'CONFIG',cfg), \
                     patch.object(adapter.socket,'gethostname',return_value='fixture'),patch.object(adapter,'Client',return_value=client), \
                     patch.object(adapter,'spawn') as spawn,patch.object(adapter,'rpc_account') as account:
                    with patch('sys.argv',argv+['--check-only']),contextlib.redirect_stdout(io.StringIO()):
                        if race:
                            with self.assertRaisesRegex(ValueError,'changed before settlement'):adapter.main()
                        else:adapter.main()
                    self.assertEqual(originals,{p.name:p.read_bytes() for p in d.iterdir()})
                    with patch('sys.argv',argv),contextlib.redirect_stdout(io.StringIO()):
                        if race:
                            with self.assertRaisesRegex(ValueError,'changed before settlement'):adapter.main()
                        else:adapter.main()
                    spawn.assert_not_called();account.assert_not_called()
                if race:self.assertEqual(originals,{p.name:p.read_bytes() for p in d.iterdir()})
                else:
                    self.assertEqual((d/'worker-only-wait-state-before.json').read_bytes(),originals['state.json'])
                    self.assertTrue(all((d/n).read_bytes()==raw for n,raw in originals.items() if n!='state.json'))
