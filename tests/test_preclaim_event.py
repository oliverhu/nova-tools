"""Real store + real local files; only transport and message closeout are adapted."""
import copy
import fcntl
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import Mock, patch

from coordinator import event_dispatch as server
from coordinator import cli_event_dispatch as local
from coordinator.cli_dispatch import save
from coordinator.store import Store, Conflict, Invalid
from coordinator.workers import MINI_VM_WORKERS
from coordinator.server import Server
from coordinator.client import Client, ServiceRejected


class PreclaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        self.worker = MINI_VM_WORKERS[0]; self.now = 1000.
        self.store = Store(self.root/'db', clock=lambda: self.now)
        self.config = {'worker': self.worker, 'hostname': socket.gethostname(), 'model': 'selected',
                       'reasoning': 'medium', 'account_email_sha256': 'a'*64, 'admission': 'enabled'}
        source = self.root/'qualified.py'; source.write_text('qualified')
        manifest = {**self.config, 'schema': 1, 'files': {str(source): local.digest(source.read_bytes())}}
        path = self.root/'readiness-manifest.json'; save(path, manifest)
        self.revision = local.digest(path.read_bytes())
        self.config['event_dispatch'] = {'enabled': True, 'readiness_revision': self.revision, 'readiness_manifest': str(path)}
        self.store.set_worker_state('admin', self.worker, {'paused': False, 'reason': 'qualified fixture'})
        self.store.pulse(self.worker, {'capacity': {'cpus': 4, 'capabilities': ['cli-shell']}})
        server.configure(self.store, 'admin', self.worker,
                         {'enabled': True, 'readiness_revision': self.revision, 'evidence': 'qualified fixture'})
        server.report(self.store, self.worker, {'sequence': 1, 'readiness_revision': self.revision,
            'availability': 'available', 'latest_run_id': None, 'settlement_sha256': 'b'*64, 'issue': None})
        self.task = self.store.submit('admin', {'request_key': 'browser', 'title': 'Exact CLI Browser acceptance',
            'description': 'Actual built-in Browser read/input/capture, no alternative route',
            'requirements': {'worker': self.worker},
            'steps': [{'name': 'Exact acceptance', 'instructions': 'Keep original Browser acceptance'}]})
        self.run_id = 'c'*32; self.token = 'private-token-fixture-01234567890123456789'
        self.binding = {'wake_id': self.run_id, 'run_id': self.run_id, 'token': self.token}
        server.reserve(self.store, self.worker, {**{k:v for k,v in self.binding.items() if k != 'token'},
            'task_id': self.task['id'], 'queued_generation': 0, 'readiness_revision': self.revision,
            'token_hash': local.digest(self.token.encode())})
        self.old = self.store.detail(self.task['id'])
        self.refinement = {'request_id': 'd'*32, 'generation': 0,
            'expected_requirements': self.old['requirements'], 'capabilities': ['builtin-browser-cli'],
            'execution_stopped': True, 'external_state_checked': True,
            'evidence': 'Exact CLI tool inventory lacks the built-in Browser required by original acceptance'}
        self.client = Mock(); self.client.request.side_effect = self.request
        self.runs = self.root/'runs'; self.runs.mkdir(); (self.runs/'worker.lock').touch(mode=0o600)

    def tearDown(self): self.temp.cleanup()

    def request(self, route, body=None):
        if route.startswith('/api/tasks/'): return self.store.detail(self.task['id'])
        if route.endswith('/preclaim-settle'): return server.settle_preclaim(self.store, self.worker, body)
        raise AssertionError(route)

    def refine(self):
        return server.require_capabilities(self.store, 'admin', self.task['id'], self.refinement)

    def fixture(self, profile='explicit-cleanup'):
        directory = self.runs/self.run_id; directory.mkdir()
        state = {'run_id': self.run_id, 'worker': self.worker, 'task': self.task['id'], 'queued_generation': 0,
            'status': 'needs_attention', 'completion_verified': False, 'cli_exit_code': 0,
            'created_at': 100., 'started_at': 101., 'ended_at': 110., 'cli_pid': 42, 'supervisor_pid': 41,
            'unreceived_message_ids': []}
        cleanup = {'execution_stopped': True, 'external_state_checked': True, 'input_settled': True,
            'unknown_operations': [], 'unsettled_operations': [], 'evidence': ['Every read-only operation returned; no runtime/input started']}
        report = {'run_id': self.run_id, 'worker': self.worker, 'task': self.task['id'], 'generation': 0,
            'outcome': 'waiting', 'acceptance_complete': False, 'summary': 'Required capability unsupported', 'cleanup': cleanup}
        if profile == 'explicit-cleanup':
            report.update(queued_generation=0, claim_attempted=False, service_receipt_submitted=False,
                test_started=False, publication=False, execution_stopped=True, external_state_checked=True)
            save(directory/'stopped-cleanup.json', cleanup); save(directory/'readiness-check.json', {'claim_attempted': False})
        else:
            report.pop('summary')  # The actual noninvocation report records its reason in the marker.
            report.update(claimed=False, service_status='queued')
            save(directory/'finish-response.json', {'run_id': self.run_id, 'task': self.task['id'], 'worker': self.worker,
                'generation': 0, 'operation': 'not_invoked', 'service_response': None, 'service_finish_submitted': False,
                'service_defer_submitted': False, 'observed_status': 'queued', 'reason': 'No claim; no finish invoked'})
            save(directory/'readiness.json', {'claim_allowed': False, 'tests_started': False, 'operations_launched': []})
        for n,v in {'state.json':state, 'worker-report.json':report, 'task-readback.json':self.old,
            'tool-discovery.json': {'tools': []}, 'wake-reservation-response.json': {'wake_id':self.run_id,'status':'reserved','duplicate':False},
            'model-spawn-intent.json': {'wake_id':self.run_id,'run_id':self.run_id}, 'wake-authorization.json': self.binding}.items(): save(directory/n,v)
        (directory/'events.jsonl').write_text('{"type":"thread.started","thread_id":"fixture"}\n')
        (directory/'events.jsonl').chmod(0o600)
        disposition = self.refine()
        note = {'run_id': self.run_id, 'task': self.task['id'], 'worker': self.worker, 'generation': 0,
            'execution_stopped': True, 'external_state_checked': True, 'input_settled': True,
            'unknown_operations': [], 'unsettled_operations': [], 'claim_attempted': False, 'finish_attempted': False,
            'capability_event_id': disposition['event_id'], 'evidence': 'Original cleanup and never-owned API history reviewed',
            'evidence_profile': profile,
            'evidence_sha256': {n:local.digest((directory/n).read_bytes()) for n in local.PRECLAIM_PROFILES[profile]}}
        note_path = self.root/'note.json'; save(note_path, note)
        return directory, state, note_path

    def apply(self, state, note, check_only=False):
        with (self.runs/'worker.lock').open('r') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
            return local.reconcile_preclaim_event(self.client, self.config, self.runs, state, note, lock, check_only)

    def test_additive_audited_requirements_exclude_event_and_normal_claim_preserving_acceptance(self):
        first = self.refine(); self.assertFalse(first['duplicate'])
        self.assertTrue(self.refine()['duplicate'])
        current = self.store.detail(self.task['id'])
        for key in ('description','dependencies','generation','step','owner','last_owner','result','receipts','fix_attempts_used'):
            self.assertEqual(self.old[key], current[key])
        self.assertEqual(current['requirements']['worker'], self.worker)
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?',(self.task['id'],)).fetchone()
            self.assertIsNone(self.store.candidate(db,row))
        self.assertIsNone(server.next_candidate(self.store,self.worker)['task_id'])
        self.assertIsNone(self.store.claim(self.worker,self.task['id'],self.binding)['task'])
        # Actual separately qualified capacity can later meet the requirement.
        self.store.pulse(self.worker, {'capacity': {'capabilities': ['cli-shell','builtin-browser-cli']}})
        self.assertEqual(self.store.claim(self.worker,self.task['id'],self.binding)['task']['generation'],1)

    def test_refinement_rejects_foreign_stale_conflicting_or_active_ownership(self):
        with self.assertRaises(Invalid): server.require_capabilities(self.store,self.worker,self.task['id'],self.refinement)
        with self.assertRaises(Conflict): server.require_capabilities(self.store,'admin',self.task['id'],{**self.refinement,'generation':1})
        self.refine()
        with self.assertRaises(Conflict): server.require_capabilities(self.store,'admin',self.task['id'],{**self.refinement,'capabilities':['other']})
        self.store.pulse(self.worker, {'capacity': {'capabilities': ['builtin-browser-cli']}})
        self.store.claim(self.worker,self.task['id'],self.binding)
        fresh=self.store.detail(self.task['id'])
        with self.assertRaises(Conflict): server.require_capabilities(self.store,'admin',self.task['id'],
            {**self.refinement,'request_id':'e'*32,'generation':1,'expected_requirements':fresh['requirements']})

    def test_positive_no_claim_settlement_preserves_originals_and_task_and_recovers_idempotently(self):
        directory,state,note=self.fixture()
        originals={n:(directory/n).read_bytes() for n in local.PRECLAIM_PROFILES['explicit-cleanup']}
        before={p.name:p.read_bytes() for p in directory.iterdir()}
        self.assertTrue(self.apply(state,note,True)['validated'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in directory.iterdir()})
        self.assertTrue(self.apply(state,note)['settled'])
        for n,raw in originals.items():
            self.assertEqual(raw,(directory/('preclaim-state-before.json' if n=='state.json' else n)).read_bytes())
        settled=json.loads((directory/'state.json').read_bytes())
        self.assertTrue(self.apply(settled,note)['settled'])
        response=json.loads((directory/'event-settlement-response.json').read_bytes());self.assertTrue(response['duplicate'])
        current=self.store.detail(self.task['id'])
        self.assertEqual((current['status'],current['generation'],current['owner'],current['receipts']),('queued',0,None,[]))
        self.assertEqual(current['events'][-1]['kind'],'wake-preclaim-settled')
        self.assertNotIn(self.token,str(current))
        self.assertEqual(local.admission_inventory(self.runs,self.worker)[0],self.run_id)
        self.assertIsNone(server.next_candidate(self.store,self.worker)['task_id'])

    def test_truthful_noninvocation_marker_is_preserved_and_not_a_finish(self):
        directory,state,note=self.fixture('noninvocation-marker')
        original=(directory/'finish-response.json').read_bytes()
        self.assertTrue(self.apply(state,note)['settled'])
        self.assertEqual(original,(directory/'finish-response.json').read_bytes())
        self.assertEqual(self.store.detail(self.task['id'])['receipts'],[])

    def test_actual_authenticated_http_refinement_and_preclaim_ack(self):
        directory,state,note=self.fixture()
        tokens={'admin':'a'*40,self.worker:'b'*40}
        service=Server(('127.0.0.1',0),self.store,tokens,Path(__file__).resolve().parents[1]/'web')
        thread=threading.Thread(target=service.serve_forever,daemon=True);thread.start()
        try:
            url='http://127.0.0.1:'+str(service.server_port)
            admin_path=self.root/'admin.json';worker_path=self.root/'worker.json'
            save(admin_path,{'url':url,'worker':'admin','token':tokens['admin']})
            save(worker_path,{'url':url,'worker':self.worker,'token':tokens[self.worker]})
            admin=Client(admin_path);self.client=Client(worker_path)
            route='/api/tasks/'+self.task['id']+'/require-capabilities'
            self.assertTrue(admin.request(route,self.refinement)['duplicate'])
            with self.assertRaises(ServiceRejected):self.client.request(route,self.refinement)
            with self.assertRaises(ServiceRejected):admin.request('/api/worker/dispatch/preclaim-settle',{})
            self.assertTrue(self.apply(state,note)['settled'])
        finally:
            service.shutdown();service.server_close();thread.join()

    def test_real_finish_marker_changed_evidence_or_unread_messages_refuse(self):
        directory,state,note_path=self.fixture('noninvocation-marker')
        note=json.loads(note_path.read_bytes())
        marker=json.loads((directory/'finish-response.json').read_bytes());marker['service_finish_submitted']=True
        save(directory/'finish-response.json',marker)
        note['evidence_sha256']['finish-response.json']=local.digest((directory/'finish-response.json').read_bytes());save(note_path,note)
        with self.assertRaises(ValueError):self.apply(state,note_path)
        marker['service_finish_submitted']=False;save(directory/'finish-response.json',marker)
        note['evidence_sha256']['finish-response.json']=local.digest((directory/'finish-response.json').read_bytes());save(note_path,note)
        save(directory/'messages.json',{'messages':[{'status':'session_ended_unread','message_id':'x'}]})
        with self.assertRaises(ValueError):self.apply(state,note_path)
        self.assertFalse((directory/'preclaim-state-before.json').exists())

    def test_ambiguous_ack_keeps_service_fence_and_reuses_same_immutable_request(self):
        directory,state,note=self.fixture()
        def outage(route,body=None):
            if route.endswith('/preclaim-settle'): raise TimeoutError('no outcome known')
            return self.request(route,body)
        self.client.request.side_effect=outage
        with self.assertRaises(TimeoutError): self.apply(state,note)
        with self.store.connection() as db: self.assertEqual(server.active(db,self.worker)['status'],'reserved')
        raw=(directory/'preclaim-event-request.json').read_bytes()
        self.client.request.side_effect=self.request
        self.apply(json.loads((directory/'state.json').read_bytes()),note)
        self.assertEqual(raw,(directory/'preclaim-event-request.json').read_bytes())

    def test_changed_cleanup_unknown_or_approval_never_releases_reservation(self):
        directory,state,note_path=self.fixture(); note=json.loads(note_path.read_bytes())
        for field,value in [('unknown_operations',['unsettled input']),('human_stop',True),('finish_attempted',True)]:
            bad={**note,field:value};save(note_path,bad)
            with self.assertRaises(ValueError):self.apply(state,note_path)
        save(note_path,note)
        report=json.loads((directory/'worker-report.json').read_bytes());report['cleanup']['input_settled']=False
        save(directory/'worker-report.json',report)
        note['evidence_sha256']['worker-report.json']=local.digest((directory/'worker-report.json').read_bytes());save(note_path,note)
        with self.assertRaises(ValueError):self.apply(state,note_path)
        self.assertFalse((directory/'preclaim-state-before.json').exists())
        with self.store.connection() as db:self.assertEqual(server.active(db,self.worker)['status'],'reserved')

    def test_foreign_or_changed_service_settlement_cannot_release(self):
        directory,state,note=self.fixture();self.apply(state,note)
        body=json.loads((directory/'preclaim-event-request.json').read_bytes())
        with self.assertRaises(Conflict):server.settle_preclaim(self.store,MINI_VM_WORKERS[1],body)
        with self.assertRaises(Conflict):server.settle_preclaim(self.store,self.worker,{**body,'token':'wrong'*10})
        with self.assertRaises(Conflict):server.settle_preclaim(self.store,self.worker,{**body,'result_sha256':'f'*64})
        with self.assertRaises(Invalid):server.settle_preclaim(self.store,self.worker,{**body,'generation':1})

    def test_busy_and_unresolved_local_reports_remain_fresh_without_starting_models(self):
        stop=threading.Event(); client=Mock()
        def request(route,body):
            if route.endswith('/next'):stop.set();return {'task_id':None}
            return {'accepted':True}
        client.request.side_effect=request
        with (self.runs/'worker.lock').open('r') as lock, patch.object(local.subprocess,'run') as spawn:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            local.listen(client,self.config,self.runs,stop)
            self.assertEqual(client.request.call_args_list[0].args[1]['availability'],'busy');spawn.assert_not_called()
        stop.clear();client.reset_mock()
        with patch.object(local,'admission_inventory',side_effect=ValueError('unresolved')),patch.object(local.subprocess,'run') as spawn:
            local.listen(client,self.config,self.runs,stop)
            self.assertEqual(client.request.call_args_list[0].args[1]['availability'],'unknown')
            self.assertEqual(client.request.call_args_list[0].args[1]['issue'],'local_admission_unresolved');spawn.assert_not_called()


if __name__=='__main__':unittest.main()
