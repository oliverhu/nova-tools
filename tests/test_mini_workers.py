"""Mini guests cannot claim before onboarding or receive another worker's token."""
import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from coordinator.store import Store, Invalid
from coordinator.workers import WORKERS, MINI_VM_WORKERS, STUDIO_WORKERS


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'


def script(name):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location('mini_test_' + name.replace('-', '_'), SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class MiniWorkersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'db', clock=lambda: 1000, heavy_worker=STUDIO_WORKERS[0])

    def tearDown(self):
        self.temp.cleanup()

    def pulse(self, worker):
        self.store.pulse(worker, {'capacity': {'cpus': 5, 'memory_gb': 8, 'disk_gb': 100,
                                             'capabilities': ['macos', 'development']}})

    def test_new_guests_stay_paused_through_pulses_restart_and_explicit_claims(self):
        for index, worker in enumerate(MINI_VM_WORKERS):
            task = self.store.submit('admin', {'request_key': 'mini-' + str(index),
                'title': 'Work after actual onboarding', 'requirements': {'worker': worker}})
            self.pulse(worker)
            self.assertIsNone(self.store.claim(worker, task['id'])['task'])
            self.assertIsNone(self.store.claim(worker)['task'])
            self.assertFalse(self.store.detail(task['id'])['ready'])
        self.store = Store(self.store.path, clock=lambda: 1000)
        for worker in MINI_VM_WORKERS:
            self.pulse(worker)
            row = next(x for x in self.store.snapshot()['workers'] if x['id'] == worker)
            self.assertTrue(row['paused'])
            self.assertIsNone(self.store.claim(worker)['task'])

    def test_readiness_activation_is_admin_only_and_individual_and_persists(self):
        first, second = MINI_VM_WORKERS
        for worker in MINI_VM_WORKERS: self.pulse(worker)
        with self.assertRaises(Invalid):
            self.store.set_worker_state(first, first, {'paused': False, 'reason': 'Self-enable refused'})
        self.store.set_worker_state('admin', first, {'paused': False, 'reason': 'Verified exact onboarding receipt'})
        self.store = Store(self.store.path, clock=lambda: 1000)
        states = {x['id']: x['paused'] for x in self.store.snapshot()['workers']}
        self.assertFalse(states[first]); self.assertTrue(states[second])
        task = self.store.submit('admin', {'request_key': 'eligible', 'title': 'Qualified worker work',
                                         'requirements': {'worker': first}})
        claim = self.store.claim(first, task['id'])
        self.assertEqual(first, claim['task']['owner'])
        self.assertIsNone(self.store.claim(second)['task'])

    def test_enrollment_preserves_retired_host_vm2_hold_and_existing_running_claim(self):
        for worker in ('oliver-mini', STUDIO_WORKERS[2]):
            self.store.set_worker_state('admin', worker, {'paused': True, 'reason': 'Existing genuine hold'})
        self.pulse(STUDIO_WORKERS[0])
        task = self.store.submit('admin', {'request_key': 'active', 'title': 'Existing work',
                                         'requirements': {'worker': STUDIO_WORKERS[0]}})
        claim = self.store.claim(STUDIO_WORKERS[0], task['id']); before = self.store.detail(task['id'])
        self.store = Store(self.store.path, clock=lambda: 1000)
        self.assertEqual(before, self.store.detail(task['id']))
        self.store.heartbeat(STUDIO_WORKERS[0], task['id'], {'generation': 1, 'claim_token': claim['claim_token']})
        for worker in ('oliver-mini', STUDIO_WORKERS[2], *MINI_VM_WORKERS):
            self.pulse(worker)
            self.assertTrue(next(x for x in self.store.snapshot()['workers'] if x['id'] == worker)['paused'])
            self.assertIsNone(self.store.claim(worker)['task'])

    def test_guest_capacity_and_distinct_claims_after_separate_activation(self):
        claims = []
        for index, worker in enumerate(MINI_VM_WORKERS):
            self.pulse(worker)
            self.store.set_worker_state('admin', worker, {'paused': False, 'reason': 'Verified worker ' + str(index)})
            too_large = self.store.submit('admin', {'request_key': 'large-' + str(index), 'title': 'Too large',
                'requirements': {'worker': worker, 'memory_gb': 9}})
            self.assertIsNone(self.store.claim(worker, too_large['id'])['task'])
            task = self.store.submit('admin', {'request_key': 'fits-' + str(index), 'title': 'Fits guest',
                'requirements': {'worker': worker, 'cpus': 5, 'memory_gb': 8}})
            claims.append(self.store.claim(worker, task['id']))
        self.assertEqual(set(MINI_VM_WORKERS), {x['task']['owner'] for x in claims})
        self.assertEqual(2, len({x['claim_token'] for x in claims}))

    def test_engine_work_remains_on_configured_studio(self):
        task = self.store.submit('admin', {'request_key': 'engine', 'title': 'Studio engine', 'task_type': 'engine_rebuild'})
        for worker in MINI_VM_WORKERS:
            self.pulse(worker)
            self.store.set_worker_state('admin', worker, {'paused': False, 'reason': 'General development ready'})
            self.assertIsNone(self.store.claim(worker, task['id'])['task'])
        self.assertEqual(STUDIO_WORKERS[0], self.store.detail(task['id'])['next_worker'])


class MiniBootstrapTests(unittest.TestCase):
    def test_standalone_bootstrap_allowlist_matches_service(self):
        module = script('bootstrap')
        self.assertEqual(set(WORKERS), set(module.SUPPORTED_WORKERS))
        with self.assertRaises(ValueError): module.credential_command('unknown-guest')

    def test_remote_selector_releases_only_the_selected_identity(self):
        module = script('bootstrap')
        credentials = {worker: str(index) * 40 for index, worker in enumerate(('admin', *WORKERS), 1)}
        secret = {'data': {'credentials.json': base64.b64encode(json.dumps(credentials).encode()).decode()}}
        for worker in MINI_VM_WORKERS:
            command = module.credential_command(worker)
            selector = shlex.split(command.split(' | ', 1)[1])
            result = subprocess.run([sys.executable, *selector[1:]], input=json.dumps(secret),
                                    text=True, capture_output=True, check=True)
            self.assertEqual({'worker': worker, 'token': credentials[worker]}, json.loads(result.stdout))
            for other in credentials:
                if other != worker: self.assertNotIn(credentials[other], result.stdout)
        missing = subprocess.run([sys.executable, *selector[1:]], input=json.dumps({'data': {
            'credentials.json': base64.b64encode(b'{}').decode()}}), text=True, capture_output=True)
        self.assertNotEqual(0, missing.returncode); self.assertEqual('', missing.stdout)

    def test_install_accepts_each_own_identity_without_starting_agents(self):
        module = script('install-client')
        for worker in MINI_VM_WORKERS:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); ca = root / 'ca'; ca.write_text('public test CA')
                source = root / 'input.json'; source.write_text(json.dumps({'worker': worker,
                    'token': 'own-credential-fixture-only', 'url': 'https://fixture.invalid', 'ca_file': str(ca)}))
                with patch.object(Path, 'home', return_value=root / 'guest'), patch.object(sys, 'argv', [
                    'install-client.py', '--config', str(source), '--no-start']), patch.object(module.subprocess, 'run') as run, contextlib.redirect_stdout(io.StringIO()):
                    module.main()
                target = root / 'guest/.config/nova-coordinator/client.json'
                self.assertEqual(worker, json.loads(target.read_text())['worker'])
                self.assertEqual(0o600, target.stat().st_mode & 0o777)
                run.assert_not_called()

    def test_setup_plan_and_coordinator_phase_accept_guest_without_starting_agents(self):
        module = script('setup-new-mac')
        for worker in MINI_VM_WORKERS:
            config = json.loads((ROOT / 'setup/new-mac.example.json').read_text())
            config['coordinator']['worker'] = worker
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'setup.json'; path.write_text(json.dumps(config))
                output = io.StringIO()
                with patch.object(sys, 'argv', ['setup-new-mac.py', '--config', str(path), '--phase', 'coordinator']), contextlib.redirect_stdout(output):
                    self.assertEqual(0, module.main())
                self.assertFalse(json.loads(output.getvalue())['changes_performed'])
            instance = module.Setup.__new__(module.Setup); instance.config = config
            instance.browser = Path('/guest/source'); instance.report = {}; instance.command = Mock()
            instance.coordinator()
            command = instance.command.call_args.args[0]
            self.assertEqual(worker, command[command.index('--worker') + 1])
            self.assertIn('--no-start', command)


if __name__ == '__main__':
    unittest.main()
