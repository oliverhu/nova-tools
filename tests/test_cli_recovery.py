"""Real bounded local record IO/locks and inert service responses; no guest use."""
import copy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from coordinator import cli_recovery as recovery
from coordinator import cli_dispatch as dispatch
from coordinator.cli_event_dispatch import admission_inventory
from coordinator.cli_results import clean

RUN = 'b'*32
WORKER = 'inert-recovery-mini'
TASK = 'a'*16


def write(path, value):
    path.write_bytes(recovery.encode(value)); path.chmod(0o600)
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


class Client:
    def __init__(self, task): self.task = task; self.calls = []
    def request(self, path, body=None):
        assert body is None and path == '/api/tasks/'+TASK, 'Recovery must never mutate service state'
        self.calls.append(path); return copy.deepcopy(self.task)


class Fixture:
    def __init__(self, root):
        self.root = root; root.chmod(0o700); self.directory = root/RUN; self.directory.mkdir(mode=0o700)
        self.run = {'run_id': RUN, 'task': TASK, 'worker': WORKER, 'queued_generation': 0,
                    'status': 'needs_attention', 'cli_exit_code': 64, 'created_at': 800, 'ended_at': 950}
        self.unknown = {'pid': 7777, 'uid': os.getuid(), 'birth': [1, 2], 'pathErrno': 2}
        self.report = {'run_id': RUN, 'task': TASK, 'worker': WORKER, 'generation': 1,
                       'execution_stopped': False, 'resourceReleaseAuthorized': False,
                       'cleanup': {'execution_stopped': False, 'unresolved_observations': [self.unknown],
                                   'unknown_operations': ['inert native outcome'], 'unsettled_operations': []}}
        self.task = {'id': TASK, 'generation': 1, 'step': 0, 'steps': [{}], 'status': 'queued',
                     'owner': None, 'last_owner': WORKER, 'requirements': {'worker': WORKER},
                     'dependencies': ['c'*16], 'result': copy.deepcopy(self.report), 'approval': {'state': 'awaiting_decision'},
                     'receipts': [], 'events': [{'id': 1, 'task_id': TASK, 'kind': 'claimed', 'at': 800,
                                               'actor': WORKER, 'detail': 'Step 1; generation 1; resources []'}]}
        self.task.update(fix_attempts_used=0, total_fix_attempts=0, attempts_host=WORKER, attempt_base=0,
                         reassignment_count=0, attempt_exception={}, attempt_allowance={})
        self.client = Client(self.task)
        originals = {'state.json': self.run, 'worker-report.json': self.report, 'task-readback.json': self.task,
                     'finish-response.json': {'clean': False, 'resourceReleaseAuthorized': False}}
        self.originals = {name: write(self.directory/name, value)['sha256'] for name, value in originals.items()}
        self.before = {name: (self.directory/name).read_bytes() for name in originals}
        self.inputs = self.directory/'recovery-inputs'; self.inputs.mkdir(mode=0o700)
        self.resources = []; raw_resources = []
        for index, role in enumerate(('host', 'controller', 'driver', 'executor', 'server')):
            expected = {'role': role, 'identity': {'pid': 100+index, 'uid': os.getuid(), 'birth': [2, index]},
                        'waiter': {'pid': 200, 'uid': os.getuid(), 'birth': [1, 1]},
                        'observer_source_sha256': 'd'*64, 'not_before_epoch': 900, 'not_after_epoch': 990}
            result = {key: expected[key] for key in ('role', 'identity', 'waiter')}
            operation = 'verify-owned-process-settlement'
            if role == 'server':
                operation = 'verify-owned-server-settlement'; expected.update(endpoint='http://127.0.0.1:49152', nonce='e'*32)
                result.update(thread_joined=True, listener_closed=True, connection_refused=True, endpoint=expected['endpoint'], nonce=expected['nonce'])
            else: result.update(reaped=True, normal_exit=True, returncode=64 if role == 'host' else 0, wait_target_pid=expected['identity']['pid'])
            raw_proof = {**copy.deepcopy(result), 'recorded_at': 950}
            raw_id = role+'-raw'
            source_ref = write(self.inputs/(raw_id+'.json'), raw_proof)
            raw_resources.append({'id': raw_id, 'operation': 'read-json-record', 'reference': source_ref, 'expected': {}})
            provenance = {key: {'reference': source_ref, 'pointer': '/'+key} for key in raw_proof if key != 'role'}
            value = {'schema': 1, 'kind': 'owned-settlement-metadata', 'run_id': RUN, 'task': TASK, 'worker': WORKER,
                     'generation': 1, 'resource_id': role, 'observer_source_sha256': 'd'*64, 'recorded_at': 950,
                     'available': True, 'effects': [], 'result': result, 'provenance': provenance}
            ref = write(self.inputs/(role+'.json'), value)
            self.resources.append({'id': role, 'operation': operation, 'reference': ref, 'expected': expected})
        self.resources.extend(raw_resources)
        self.resources.append({'id': 'false-census', 'operation': 'read-json-record', 'expected': {},
                               'reference': write(self.inputs/'false-census.json', {'clean': False, 'opaque': [self.unknown], 'owned': []})})
        self.request = {'schema': 1, 'kind': 'scoped-readonly-recovery-request', 'run_id': RUN, 'task': TASK, 'worker': WORKER,
                        'generation': 1, 'originals': self.originals, 'resources': self.resources, 'ttl_seconds': 60}
        self.request_ref = write(root/'request.json', self.request)

    def plan(self):
        self.request_ref = write(self.root/'request.json', self.request)
        self.plan_ref = recovery.plan(self.root, WORKER, RUN, self.request_ref, self.client, clock=lambda: 1000)
        return self.plan_ref

    def review(self):
        self.review_value = {'schema': 1, 'kind': 'scoped-readonly-recovery-review', 'plan_sha256': self.plan_ref['sha256'],
                             'decision': 'approved', 'scope': 'bound-records-only', 'resources': self.resources,
                             'source': {'thread_id': 'inert-source', 'message_id': 'inert-no-human-decision'},
                             'reviewed_at': 1000, 'expires_at': 1060, 'nativeObservationAuthorized': False,
                             'nativeExecutionAuthorized': False, 'resourceReleaseAuthorized': False}
        self.review_ref = write(self.root/'review.json', self.review_value)
        return self.review_ref

    def observe(self, clock=lambda: 1001):
        return recovery.observe(self.root, WORKER, RUN, self.plan_ref, self.review_ref, self.plan_ref['sha256'], self.client, clock=clock)

    def mutation(self, role, change):
        resource = next(r for r in self.resources if r['id'] == role)
        path = Path(resource['reference']['path']); value = json.loads(path.read_text()); change(value)
        resource['reference'] = write(path, value)

    def assert_unchanged(self, test):
        for name, raw in self.before.items(): test.assertEqual((self.directory/name).read_bytes(), raw)
        with test.assertRaises(ValueError): admission_inventory(self.root, WORKER)
        with test.assertRaises(ValueError): clean([self.report])


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.f = Fixture(Path(self.temp.name).resolve())

    def ready(self): self.f.plan(); self.f.review()

    def test_all_positive_owned_roles_remain_separate_from_all_retained_unknowns(self):
        self.ready(); ref = self.f.observe(); value = json.loads(Path(ref['path']).read_text())
        self.assertEqual(value['status'], 'recorded')
        self.assertEqual([r['id'] for r in value['resources'] if r['verified_owned_closure']], ['host', 'controller', 'driver', 'executor', 'server'])
        self.assertFalse(value['resources'][-1]['metadata']['clean'])
        self.assertTrue(any(row['value'] == self.f.unknown for row in value['retained_unknowns']))
        self.assertEqual(value['availability'], 'unknown')
        for key in ('wakeACKAuthorized', 'execution_stopped', 'resourceReleaseAuthorized', 'nativeObservationAuthorized', 'nativeExecutionAuthorized', 'originalsRewritten', 'ownedClosureObservedHere'):
            self.assertFalse(value[key])
        self.assertEqual(Path(ref['path']).stat().st_mode & 0o777, 0o400)
        self.f.assert_unchanged(self)
        with self.assertRaisesRegex(ValueError, 'refused'): self.f.observe()  # One-shot, no automatic replay.
        self.assertEqual(Path(ref['path']).read_bytes(), recovery.encode(value))

    def test_unavailable_observer_and_unreaped_child_cannot_be_positive_closure(self):
        self.f.mutation('controller', lambda v: v.update(available=False, result={}))
        self.f.mutation('driver-raw', lambda v: v.update(reaped=False))
        self.f.mutation('driver', lambda v: v['result'].update(reaped=False))
        raw_ref = next(r['reference'] for r in self.f.resources if r['id'] == 'driver-raw')
        self.f.mutation('driver', lambda v: [source.update(reference=raw_ref) for source in v['provenance'].values()])
        self.ready(); ref = self.f.observe(); value = json.loads(Path(ref['path']).read_text())
        self.assertFalse(next(r for r in value['resources'] if r['id'] == 'controller')['verified_owned_closure'])
        self.assertFalse(next(r for r in value['resources'] if r['id'] == 'driver')['verified_owned_closure'])
        self.f.assert_unchanged(self)

    def test_wrong_owned_identity_waiter_source_clock_effects_or_server_resource_refuse(self):
        changes = [('host', lambda v: v['result']['identity'].update(birth=[99, 1])),
                   ('driver', lambda v: v['result']['waiter'].update(pid=999)),
                   ('executor', lambda v: v.update(observer_source_sha256='f'*64)),
                   ('controller', lambda v: v.update(recorded_at=899)),
                   ('host', lambda v: v.update(effects=['unsettled signal'])),
                   ('server', lambda v: v['result'].update(endpoint='http://127.0.0.1:49153')),
                   ('server', lambda v: v['result'].update(nonce='f'*32)),
                   ('host', lambda v: v.update(generation=2))]
        for role, change in changes:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                f = Fixture(Path(directory).resolve()); f.mutation(role, change); f.plan(); f.review()
                with self.assertRaisesRegex(ValueError, 'refused'): f.observe()
                refused = list((f.directory/'scoped-recovery').glob('refused-*.json'))
                self.assertEqual(len(refused), 1); self.assertEqual(json.loads(refused[0].read_text())['status'], 'refused')
                f.assert_unchanged(self)

    def test_forged_positive_fact_or_provenance_outside_inventory_refuses(self):
        self.f.mutation('host-raw', lambda v: v.update(reaped=False))
        raw_ref = next(r['reference'] for r in self.f.resources if r['id'] == 'host-raw')
        self.f.mutation('host', lambda v: [source.update(reference=raw_ref) for source in v['provenance'].values()])
        self.ready()
        with self.assertRaisesRegex(ValueError, 'refused'): self.f.observe()
        self.f.assert_unchanged(self)
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory).resolve())
            f.mutation('host', lambda v: v['provenance']['reaped']['reference'].update(path='/unapproved/receipt.json'))
            f.plan(); f.review()
            with self.assertRaisesRegex(ValueError, 'refused'): f.observe()
            f.assert_unchanged(self)

    def test_wait_pid_and_exit_facts_cannot_be_grafted_from_another_child(self):
        controller = next(r['reference'] for r in self.f.resources if r['id'] == 'controller-raw')
        self.f.mutation('host', lambda v: v['provenance']['reaped'].update(reference=controller))
        self.ready()
        with self.assertRaisesRegex(ValueError, 'refused'): self.f.observe()
        self.f.assert_unchanged(self)
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory).resolve())
            f.mutation('host-raw', lambda v: v.update(wait_target_pid=999))
            source = next(r['reference'] for r in f.resources if r['id'] == 'host-raw')
            def forged(value):
                value['result']['wait_target_pid'] = 999
                for row in value['provenance'].values(): row['reference'] = source
            f.mutation('host', forged); f.plan(); f.review()
            with self.assertRaisesRegex(ValueError, 'refused'): f.observe()
            f.assert_unchanged(self)

    def test_wrong_request_scope_and_unapproved_operations_refuse_before_resource_read(self):
        for update in ({'worker': 'foreign'}, {'generation': True}, {'ttl_seconds': 61}, {'ttl_seconds': float('nan')}):
            self.f.request.update(update)
            with self.assertRaises((ValueError, TypeError)): self.f.plan()
            self.f = Fixture(Path(tempfile.mkdtemp()).resolve()); self.addCleanup(__import__('shutil').rmtree, self.f.root)
        for operation in ('native-census', 'signal', 'launch', 'input', 'consent', 'sign', 'release', 'arbitrary-command'):
            self.f.resources[0]['operation'] = operation
            with self.assertRaisesRegex(ValueError, 'allowlist'): self.f.plan()
        self.assertFalse((self.f.directory/'scoped-recovery').exists())

    def test_review_mismatch_native_authority_and_stale_plan_refuse_without_reading_resources(self):
        for update in ({'decision': 'pending'}, {'scope': 'native-observation'}, {'nativeObservationAuthorized': True},
                       {'nativeExecutionAuthorized': True}, {'resourceReleaseAuthorized': True}, {'plan_sha256': '0'*64},
                       {'resources': []}, {'reviewed_at': 999}, {'expires_at': 1061}):
            self.ready(); self.f.review_value.update(update); self.f.review_ref = write(self.f.root/'review.json', self.f.review_value)
            with patch.object(recovery, 'observe_resource') as observe:
                with self.assertRaisesRegex(ValueError, 'refused'): self.f.observe()
                observe.assert_not_called()
        self.ready()
        with self.assertRaisesRegex(ValueError, 'refused'): self.f.observe(clock=lambda: 1061)
        self.f.assert_unchanged(self)

    def test_full_api_history_receipts_owner_and_generation_cas_are_required(self):
        changes = [lambda t: t.update(owner='foreign'), lambda t: t.update(generation=2),
                   lambda t: t['events'].append({'id': 2, 'task_id': TASK, 'kind': 'changed', 'actor': WORKER, 'at': 1001, 'detail': 'inert history race'}),
                   lambda t: t['receipts'].append({'changed': True}), lambda t: t.update(approval={'new': True}),
                   lambda t: t.update(dependencies=[])]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                f = Fixture(Path(directory).resolve()); f.plan(); f.review(); change(f.client.task)
                with self.assertRaisesRegex(ValueError, 'refused'): f.observe()
                f.assert_unchanged(self)

    def test_history_race_during_operation_and_observation_deadline_do_not_commit_success(self):
        self.ready(); original = recovery.observe_resource
        def race(*args):
            value = original(*args)
            if len(self.f.client.task['events']) == 1:
                self.f.client.task['events'].append({'id': 2, 'task_id': TASK, 'kind': 'changed', 'actor': WORKER, 'at': 1001, 'detail': 'inert history race'})
            return value
        with patch.object(recovery, 'observe_resource', side_effect=race):
            with self.assertRaisesRegex(ValueError, 'refused'): self.f.observe()
        self.assertFalse(list((self.f.directory/'scoped-recovery').glob('evidence-*.json')))
        self.f.assert_unchanged(self)
        # A fresh inert case isolates a clock overrun from the history race.
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory).resolve()); f.plan(); f.review()
            ticks = iter([1001]+[1002]*11+[1061])
            with self.assertRaisesRegex(ValueError, 'refused'): f.observe(clock=lambda: next(ticks))
            f.assert_unchanged(self)

    def test_same_bytes_replaced_original_or_resource_identity_refuses(self):
        self.ready(); path = self.f.directory/'state.json'; raw = path.read_bytes()
        new = self.f.root/'replacement'; new.write_bytes(raw); new.chmod(0o600); os.replace(new, path)
        with self.assertRaisesRegex(ValueError, 'refused'): self.f.observe()
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory).resolve()); f.plan(); f.review(); original = recovery.observe_resource
            def replace(resource, *args):
                value = original(resource, *args)
                path = Path(resource['reference']['path']); replacement = f.root/'replacement'
                replacement.write_bytes(path.read_bytes()); replacement.chmod(0o600); os.replace(replacement, path)
                return value
            with patch.object(recovery, 'observe_resource', side_effect=replace):
                with self.assertRaisesRegex(ValueError, 'refused'): f.observe()
            f.assert_unchanged(self)

    def test_missing_unreadable_oversize_symlink_or_changed_resource_stays_unknown(self):
        cases = ('missing', 'oversize', 'symlink', 'changed')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                f = Fixture(Path(directory).resolve()); f.plan(); f.review(); path = Path(f.resources[0]['reference']['path'])
                if case == 'missing': path.unlink()
                elif case == 'oversize': path.write_bytes(b'x'*(recovery.MAX_RECORD+1)); path.chmod(0o600)
                elif case == 'symlink':
                    target = f.root/'retained'; path.rename(target); path.symlink_to(target)
                else: path.write_bytes(b'{}'); path.chmod(0o600)
                with self.assertRaisesRegex(ValueError, 'refused'): f.observe()
                f.assert_unchanged(self)

    def test_no_unknowns_missing_history_or_foreign_claim_cannot_use_recovery(self):
        self.f.task['events'][0]['actor'] = 'foreign'
        with self.assertRaises(ValueError): self.f.plan()
        self.f.task['events'] = []
        with self.assertRaises(ValueError): self.f.plan()
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory).resolve())
            for name in f.originals:
                value = json.loads((f.directory/name).read_text())
                def remove(v):
                    if isinstance(v, dict):
                        for k, item in v.items():
                            if k in recovery.UNKNOWN_KEYS: v[k] = []
                            else: remove(item)
                    elif isinstance(v, list):
                        for item in v: remove(item)
                remove(value); f.originals[name] = write(f.directory/name, value)['sha256']
            remove(f.client.task)
            with self.assertRaisesRegex(ValueError, 'retained unknown'): f.plan()

    def test_request_or_review_identity_replacement_after_read_refuses(self):
        for reference in ('request_ref', 'review_ref'):
            with self.subTest(reference=reference), tempfile.TemporaryDirectory() as directory:
                f = Fixture(Path(directory).resolve()); f.plan(); f.review(); original = recovery.observe_resource
                def replace(resource, *args):
                    result = original(resource, *args)
                    path = Path(getattr(f, reference)['path']); replacement = f.root/'replacement'
                    replacement.write_bytes(path.read_bytes()); replacement.chmod(0o600); os.replace(replacement, path)
                    return result
                with patch.object(recovery, 'observe_resource', side_effect=replace):
                    with self.assertRaisesRegex(ValueError, 'refused'): f.observe()
                f.assert_unchanged(self)

    def test_backward_clock_and_new_run_inventory_refuse(self):
        self.ready()
        with self.assertRaisesRegex(ValueError, 'refused'): self.f.observe(clock=iter([1001, 1000]).__next__)
        self.ready(); other = self.f.root/('c'*32); other.mkdir(mode=0o700)
        write(other/'state.json', {**self.f.run, 'run_id': other.name})
        with self.assertRaisesRegex(ValueError, 'refused'): self.f.observe()
        self.f.assert_unchanged(self)

    def test_worker_and_exact_run_lock_races_refuse_and_other_runs_are_preserved(self):
        self.ready()
        for path in (self.f.root/'worker.lock', self.f.directory/'scoped-recovery.lock'):
            with path.open('a+') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError): self.f.observe()
        self.f.assert_unchanged(self)

    def test_api_unavailable_and_evidence_fsync_failure_never_enable_admission(self):
        self.ready()
        with patch.object(self.f.client, 'request', side_effect=OSError('offline')):
            with self.assertRaisesRegex(ValueError, 'refused'): self.f.observe()
        self.ready()
        with patch.object(recovery.os, 'fsync', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.f.observe()
        self.f.assert_unchanged(self)

    def test_cli_observation_does_not_save_state_ack_or_report_capacity(self):
        self.f.plan_ref = recovery.plan(self.f.root, WORKER, RUN, self.f.request_ref, self.f.client, clock=time.time)
        self.f.review(); plan = json.loads(Path(self.f.plan_ref['path']).read_text())
        self.f.review_value.update(reviewed_at=time.time(), expires_at=plan['expires_at'])
        self.f.review_ref = write(self.f.root/'review.json', self.f.review_value)
        config = self.f.root/'config.json'; config.write_text(json.dumps({'hostname': socket.gethostname(), 'worker': WORKER}))
        self.f.client.config = {'worker': WORKER}
        command = ['nova-cli-worker', 'recovery-observe', '--run-id', RUN, '--note-file', self.f.plan_ref['path'],
                   '--request-sha256', self.f.plan_ref['sha256'], '--expected-fingerprint', self.f.plan_ref['sha256'],
                   '--review-file', self.f.review_ref['path'], '--review-sha256', self.f.review_ref['sha256']]
        with patch.object(dispatch, 'CONFIG', config), patch.object(dispatch, 'ROOT', self.f.root), patch.object(dispatch, 'Client', return_value=self.f.client), \
             patch.object(dispatch, 'save', side_effect=AssertionError('no lifecycle state write')), \
             patch('coordinator.cli_event_dispatch.supervisor_settled', side_effect=AssertionError('no ACK')), \
             patch('coordinator.cli_event_dispatch.report_locked', side_effect=AssertionError('no availability')), \
             patch('coordinator.cli_messages.closeout', side_effect=AssertionError('no closeout')), \
             patch('sys.argv', command), patch('sys.stdout', new_callable=io.StringIO) as output:
            dispatch.main()
        reference = json.loads(output.getvalue())
        self.assertEqual(json.loads(Path(reference['path']).read_text())['status'], 'recorded')
        self.f.assert_unchanged(self)

    def test_cli_routes_only_to_recovery_and_never_finalize_closeout_ack_or_capacity(self):
        config = self.f.root/'config.json'; config.write_text(json.dumps({'hostname': socket.gethostname(), 'worker': WORKER}))
        self.f.client.config = {'worker': WORKER}
        with patch.object(dispatch, 'CONFIG', config), patch.object(dispatch, 'ROOT', self.f.root), patch.object(dispatch, 'Client', return_value=self.f.client), \
             patch.object(dispatch, 'finalize_result', side_effect=AssertionError('no lifecycle')), patch.object(dispatch, 'execute', side_effect=AssertionError('no native')), \
             patch.object(recovery, 'plan', wraps=recovery.plan) as plan, patch('sys.argv', ['nova-cli-worker','recovery-plan','--run-id',RUN,'--note-file',self.f.request_ref['path'],'--request-sha256',self.f.request_ref['sha256']]), patch('sys.stdout', new_callable=io.StringIO) as output:
            dispatch.main()
        plan.assert_called_once(); self.assertEqual(json.loads(output.getvalue()).keys(), {'path', 'sha256'})
        self.f.assert_unchanged(self)


if __name__ == '__main__': unittest.main()
