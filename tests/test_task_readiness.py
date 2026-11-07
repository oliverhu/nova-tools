"""Exercise real admission, revocation and stopped transitions against SQLite."""
import concurrent.futures
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from coordinator import task_readiness as policy
from coordinator.store import Store, Conflict, Invalid
from coordinator import event_dispatch


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = Store(Path(self.temp.name)/'db', clock=lambda: self.now)
        self.worker = 'oliverhu-mini-vm2'
        self.store.set_worker_state('admin', self.worker, {'paused': False, 'reason': 'Isolated service test worker enrollment'})
        self.store.pulse(self.worker, {'capacity': {'capabilities': ['cli-shell']}})
        self.task = self.store.submit('admin', {'request_key': 'task', 'title': 'Task'})['id']

    def tearDown(self):
        self.temp.cleanup()

    def decision_body(self, **changes):
        return {'request_id': uuid.uuid4().hex, 'decision_key': 'setup:mode', 'expected_revision': 0,
                'state': 'approved', 'source': {'thread_id': 'human-thread', 'message_id': uuid.uuid4().hex, 'verified': True},
                'scope_sha256': 'a'*64, 'evidence': 'Actual scoped primary response', **changes}

    def ready_body(self, **changes):
        task = self.store.detail(self.task)
        return {'request_id': uuid.uuid4().hex, 'generation': task['generation'],
                'expected_revision': task['task_readiness']['revision'],
                'context_sha256': task['task_readiness']['context_sha256'], 'worker': self.worker, 'state': 'ready',
                'valid_until': self.now+3600, 'checks': [{'name': 'source-cli', 'state': 'qualified', 'evidence_sha256': 'b'*64}],
                'decisions': [], 'evidence': 'Scoped actual readiness, no GUI access inferred', **changes}

    def ready(self, **changes):
        return policy.update(self.store, 'admin', self.task, self.ready_body(**changes))

    def claim(self):
        value = self.store.claim(self.worker, self.task)
        self.assertIsNotNone(value['task'])
        return value

    def identity(self, claim):
        return {'generation': claim['task']['generation'], 'claim_token': claim['claim_token'], 'step': 0}

    def finish(self, claim, outcome='completed'):
        return self.store.finish(self.worker, self.task, {**self.identity(claim), 'outcome': outcome,
            'receipt': {'summary': 'Actual stopped result', 'outcome': outcome, 'execution_stopped': True}})

    def test_legacy_tasks_are_not_retroactively_requalified(self):
        claim = self.claim()
        self.store.heartbeat(self.worker, self.task, {**self.identity(claim), 'phase': 'testing'})
        self.assertEqual('completed', self.finish(claim)['status'])
        self.assertFalse(self.store.detail(self.task)['task_readiness']['managed'])

    def test_structured_missing_prevents_preclaim_and_unknown_is_invalid(self):
        self.ready(state='waiting', checks=[{'name': 'runner', 'state': 'missing', 'evidence_sha256': 'c'*64}])
        self.assertFalse(self.store.detail(self.task)['ready'])
        self.assertIsNone(self.store.claim(self.worker, self.task)['task'])
        with self.assertRaises(Invalid): self.ready(checks=[])
        with self.assertRaises(Invalid): self.ready(checks=[{'name': 'runner', 'state': 'unknown', 'evidence_sha256': 'c'*64}])
        with self.assertRaises(Invalid): self.ready(checks=[{'name': 'runner', 'state': 'missing', 'evidence_sha256': 'c'*64}])
        self.ready()
        self.claim()

    def test_finite_readiness_wrong_actor_context_and_revision_fail_closed(self):
        body = self.ready_body()
        with self.assertRaises(Invalid): policy.update(self.store, self.worker, self.task, body)
        for field, value in [('generation', 9), ('expected_revision', 1), ('context_sha256', 'd'*64)]:
            with self.assertRaises(Conflict): policy.update(self.store, 'admin', self.task, {**body, field: value})
        with self.assertRaises(Invalid): self.ready(valid_until=float('inf'))
        self.ready()
        self.now += 3600
        self.store.pulse(self.worker, {'capacity': {}})
        self.assertIn('expired', self.store.detail(self.task)['waiting_reason'])
        self.assertIsNone(self.store.claim(self.worker, self.task)['task'])

    def test_exact_worker_and_hard_requirements_both_apply(self):
        self.ready(worker='oliverhu-mac-studio')
        self.assertIsNone(self.store.claim(self.worker, self.task)['task'])
        self.assertFalse(self.store.detail(self.task)['ready'])
        self.ready()
        self.claim()

    def test_decision_missing_denied_superseded_scope_and_source(self):
        binding = {'decision_key': 'setup:mode', 'revision': 1, 'scope_sha256': 'a'*64}
        with self.assertRaises(Conflict): self.ready(decisions=[binding])
        body = self.decision_body()
        with self.assertRaises(Invalid): policy.decision(self.store, self.worker, body)
        policy.decision(self.store, 'admin', body)
        with self.assertRaises(Conflict): self.ready(decisions=[{**binding, 'scope_sha256': 'b'*64}])
        self.ready(decisions=[binding])
        policy.decision(self.store, 'admin', self.decision_body(expected_revision=1, state='superseded'))
        self.assertFalse(self.store.detail(self.task)['ready'])
        self.assertIsNone(self.store.claim(self.worker, self.task)['task'])
        with self.assertRaises(Conflict): self.ready(decisions=[binding])
        with self.assertRaises(Conflict): policy.decision(self.store, 'admin', {**body, 'request_id': uuid.uuid4().hex, 'expected_revision': 2})

    def test_decision_change_stops_effects_retains_owner_and_truthful_cleanup(self):
        policy.decision(self.store, 'admin', self.decision_body())
        self.ready(decisions=[{'decision_key': 'setup:mode', 'revision': 1, 'scope_sha256': 'a'*64}])
        claim = self.claim()
        identity = self.identity(claim)
        self.store.heartbeat(self.worker, self.task, {**identity, 'phase': 'testing'})
        policy.decision(self.store, 'admin', self.decision_body(expected_revision=1, state='denied'))
        for phase in (None, 'implementing', 'testing'):
            with self.assertRaises(Conflict): self.store.heartbeat(self.worker, self.task, {**identity, **({'phase': phase} if phase else {})})
        with self.assertRaises(Conflict): self.finish(claim)
        self.assertEqual(self.worker, self.store.detail(self.task)['owner'])
        self.assertEqual('running', self.store.detail(self.task)['status'])
        self.store.heartbeat(self.worker, self.task, {**identity, 'phase': 'cleanup'})
        self.store.heartbeat(self.worker, self.task, identity)
        with self.assertRaises(Conflict): self.store.heartbeat(self.worker, self.task, {**identity, 'phase': 'testing'})
        self.assertEqual('blocked', self.finish(claim, 'blocked')['status'])
        events = self.store.detail(self.task)['events']
        self.assertEqual(['testing', 'cleanup'], [json.loads(e['detail'])['phase'] for e in events if e['kind']=='execution-phase'])
        self.assertEqual('blocked', self.store.detail(self.task)['receipts'][0]['receipt']['outcome'])

    def test_revocation_no_running_requalification_or_fabricated_completion(self):
        self.ready(); claim = self.claim()
        with self.assertRaises(Conflict): self.ready()
        self.ready(state='waiting')
        with self.assertRaises(Conflict): self.store.heartbeat(self.worker, self.task, self.identity(claim))
        with self.assertRaises(Conflict): self.finish(claim)
        self.finish(claim, 'pending_external_approval')
        self.ready()
        self.assertEqual('pending_external_approval', self.store.detail(self.task)['status'])
        self.assertIsNone(self.store.claim(self.worker, self.task)['task'])

    def test_wait_invalidates_context_and_generation_preserves_receipts(self):
        self.ready(); old = self.claim()
        dep = self.store.submit('admin', {'request_key': 'dep', 'title': 'Dep'})['id']
        self.store.defer(self.worker, self.task, {**self.identity(old), 'execution_stopped': True,
            'external_state_checked': True, 'evidence': 'Owned operations settled', 'dependencies': [dep]})
        self.assertIn('changed', self.store.detail(self.task)['waiting_reason'])
        self.ready()
        self.assertFalse(self.store.detail(self.task)['ready'])  # real dependency remains
        other = self.store.claim(self.worker, dep)
        self.store.finish(self.worker, dep, {**self.identity(other), 'outcome': 'completed',
            'receipt': {'summary': 'Dependency actually complete', 'execution_stopped': True}})
        new = self.claim()
        with self.assertRaises(Conflict): self.store.heartbeat(self.worker, self.task, self.identity(old))
        self.finish(new)
        self.assertEqual(['waiting', 'completed'], [r['receipt']['outcome'] for r in self.store.detail(self.task)['receipts']])

    def test_cancel_does_not_become_cleanup_or_gain_readiness(self):
        self.ready()
        self.store.reconcile('admin', self.task, {'generation': 0, 'action': 'cancel', 'execution_stopped': True,
            'external_state_checked': True, 'evidence': 'Never claimed, cancellation only'})
        with self.assertRaises(Conflict): self.ready()
        self.assertIsNone(self.store.claim(self.worker, self.task)['task'])
        self.assertEqual('cancelled', self.store.detail(self.task)['status'])
        self.assertFalse(any(e['kind']=='execution-phase' for e in self.store.detail(self.task)['events']))

    def test_cas_concurrent_update_and_idempotent_retry(self):
        body = self.ready_body()
        def write():
            try: return policy.update(self.store, 'admin', self.task, {**body, 'request_id': uuid.uuid4().hex})
            except Conflict: return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(lambda _: write(), range(2)))
        self.assertEqual(1, results.count('conflict'))
        body = self.ready_body()
        first = policy.update(self.store, 'admin', self.task, body)
        self.assertTrue(policy.update(self.store, 'admin', self.task, body)['duplicate'])
        with self.assertRaises(Conflict): policy.update(self.store, 'admin', self.task, {**body, 'state': 'waiting'})
        self.assertEqual(2, first['revision'])

    def test_decision_claim_race_is_atomic_and_recheck_fences_winner(self):
        policy.decision(self.store, 'admin', self.decision_body())
        self.ready(decisions=[{'decision_key':'setup:mode','revision':1,'scope_sha256':'a'*64}])
        revoke = self.decision_body(expected_revision=1, state='superseded')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            claim = pool.submit(self.store.claim, self.worker, self.task)
            change = pool.submit(policy.decision, self.store, 'admin', revoke)
            result = claim.result(); change.result()
        if result['task']:
            with self.assertRaises(Conflict): self.store.heartbeat(self.worker, self.task, self.identity(result))
        else: self.assertEqual('queued', self.store.detail(self.task)['status'])

    def test_restart_does_not_erase_revocation_or_original_outcomes(self):
        self.ready(); claim = self.claim(); self.finish(claim, 'blocked')
        self.ready(state='waiting')
        self.store = Store(Path(self.temp.name)/'db', clock=lambda: self.now)
        shown = self.store.detail(self.task)
        self.assertFalse(shown['ready']); self.assertEqual('blocked', shown['status'])
        self.assertEqual('blocked', shown['receipts'][0]['receipt']['outcome'])

    def test_dispatch_preclaim_decision_race_keeps_reservation_until_actual_settlement(self):
        event_dispatch.configure(self.store, 'admin', self.worker, {'enabled': True, 'readiness_revision': 'e'*64, 'evidence': 'Actual guest ready'})
        event_dispatch.report(self.store, self.worker, {'readiness_revision': 'e'*64, 'sequence':1, 'availability':'available',
            'latest_run_id':None, 'settlement_sha256':'f'*64, 'issue':None})
        self.ready()
        self.assertEqual(self.task, event_dispatch.next_candidate(self.store, self.worker)['task_id'])
        token = 'x'*40
        event_dispatch.reserve(self.store, self.worker, {'wake_id':'1'*32, 'run_id':'1'*32, 'task_id':self.task,
            'queued_generation':0, 'readiness_revision':'e'*64, 'token_hash':policy.hashlib.sha256(token.encode()).hexdigest()})
        self.ready(state='waiting')
        self.assertIsNone(self.store.claim(self.worker, self.task, {'wake_id':'1'*32,'run_id':'1'*32,'token':token})['task'])
        with self.store.connection() as db:
            self.assertEqual('reserved', event_dispatch.active(db, self.worker)['status'])
        self.assertEqual(0, self.store.detail(self.task)['generation'])

    def test_declared_readiness_missing_record_and_omitted_decisions_fail_closed(self):
        task = self.store.submit('admin', {'request_key':'declared', 'title':'Declared', 'requirements': {
            'readiness': {'checks':['source-cli'], 'decisions':{'setup:mode':'a'*64}}}})
        self.task = task['id']
        self.assertIn('not yet recorded', self.store.detail(self.task)['waiting_reason'])
        self.assertIsNone(self.store.claim(self.worker, self.task)['task'])
        with self.assertRaises(Invalid): self.ready()
        policy.decision(self.store, 'admin', self.decision_body())
        self.ready(decisions=[{'decision_key':'setup:mode','revision':1,'scope_sha256':'a'*64}])
        claim = self.claim()
        self.finish(claim)

    def test_generation_change_invalidates_readiness_without_relabeling_history(self):
        # Reset implementation belongs to a69. Exercise the shared boundary with
        # a normal owner wait + new generation, without synthesizing reset safety.
        self.ready(); old = self.claim()
        self.store.defer(self.worker, self.task, {**self.identity(old), 'execution_stopped':True,
            'external_state_checked':True, 'evidence':'Owned inert command reaped; no unknowns', 'worker':self.worker})
        self.assertIsNone(self.store.claim(self.worker, self.task)['task'])
        self.assertIn('changed', self.store.detail(self.task)['waiting_reason'])
        self.ready(); new = self.claim()
        with self.assertRaises(Conflict): self.store.heartbeat(self.worker, self.task, {**self.identity(old),'phase':'cleanup'})
        self.assertEqual(2,new['task']['generation'])
        self.assertEqual('waiting', self.store.detail(self.task)['receipts'][0]['receipt']['outcome'])

    def test_admin_completion_cannot_bypass_stale_decision(self):
        policy.decision(self.store,'admin',self.decision_body())
        self.ready(decisions=[{'decision_key':'setup:mode','revision':1,'scope_sha256':'a'*64}])
        claim=self.claim(); self.finish(claim,'blocked')
        policy.decision(self.store,'admin',self.decision_body(expected_revision=1,state='superseded'))
        with self.assertRaises(Conflict): self.store.reconcile('admin',self.task,{
            'generation':1,'action':'complete','execution_stopped':True,'external_state_checked':True,'evidence':'Must fail closed'})
        self.assertEqual('blocked',self.store.detail(self.task)['status'])

    def test_preupgrade_active_claim_can_settle_after_explicit_revocation(self):
        claim=self.claim()
        # Emulate an actual old service claim that predates the additive table.
        with self.store.transaction() as db:
            db.execute('DELETE FROM task_execution_admission WHERE task_id=?',(self.task,))
        self.ready(state='waiting')
        self.store.heartbeat(self.worker,self.task,{**self.identity(claim),'phase':'cleanup'})
        self.store.heartbeat(self.worker,self.task,self.identity(claim))
        with self.assertRaises(Conflict): self.store.heartbeat(self.worker,self.task,{**self.identity(claim),'phase':'implementing'})
        self.finish(claim,'blocked')

    def test_reset_owner_hook_withdraws_queued_context_atomically_and_never_claims_cleanup(self):
        self.ready()
        original=self.store.detail(self.task)
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as db:
                policy.invalidate_worker(self.store,db,'admin',self.worker,'Actual authorized epoch context replaced')
                raise RuntimeError('Reset transaction interrupted')
        self.assertEqual(1,self.store.detail(self.task)['task_readiness']['revision'])
        with self.store.transaction() as db:
            with self.assertRaises(Invalid): policy.invalidate_worker(self.store,db,self.worker,self.worker,'Worker cannot reset')
            self.assertEqual([],policy.invalidate_worker(self.store,db,'admin','oliverhu-mini-vm1','Other worker context changed'))
            self.assertEqual([self.task],policy.invalidate_worker(self.store,db,'admin',self.worker,'Exact owner epoch CAS passed; old unknowns retained'))
            self.assertEqual([],policy.invalidate_worker(self.store,db,'admin',self.worker,'Duplicate invalidation'))
        shown=self.store.detail(self.task)
        self.assertEqual(original['generation'],shown['generation']); self.assertEqual(original['receipts'],shown['receipts'])
        self.assertEqual('queued',shown['status']); self.assertFalse(shown['ready'])
        self.assertFalse(any(e['kind']=='execution-phase' for e in shown['events']))
        self.assertIsNone(self.store.claim(self.worker,self.task)['task'])
        self.ready(); self.claim()
