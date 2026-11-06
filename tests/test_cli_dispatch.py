import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coordinator.cli_dispatch import check_account, environment, prior_finish, validate_task, verify_completion, save


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.config = {'worker': 'mini', 'admission': 'enabled'}
        self.task = {'id': 'a'*16, 'status': 'queued', 'owner': None, 'generation': 0,
                     'requirements': {}, 'dependencies': []}
        self.state = {'tasks': [self.task], 'workers': [
            {'id': 'mini', 'paused': 0, 'online': True,
             'capacity': {'cpus': 5, 'memory_gb': 8, 'disk_gb': 100, 'capabilities': ['macos']}}]}

    def test_valid_exact_dispatch(self):
        validate_task(self.task, self.state, self.config)

    def test_rejects_conflicting_or_ineligible_work(self):
        for delta in ({'status': 'running'}, {'owner': 'other'}, {'task_type': 'publish'},
                      {'compute': 'heavy'}, {'requirements': {'worker': 'other'}},
                      {'requirements': {'memory_gb': 16}}, {'requirements': {'capabilities': ['signing']}},
                      {'dependencies': ['missing']}):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                validate_task({**self.task, **delta}, self.state, self.config)

    def test_worker_hold_and_existing_claim(self):
        for field, value in [('paused', 1), ('online', False)]:
            state = {**self.state, 'workers': [{**self.state['workers'][0], field: value}]}
            with self.assertRaises(ValueError):
                validate_task(self.task, state, self.config)
        self.state['tasks'].append({'status': 'running', 'owner': 'mini'})
        with self.assertRaises(ValueError):
            validate_task(self.task, self.state, self.config)

    def test_auth_environment_does_not_inject_studio_or_api_account(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test', 'CODEX_API_KEY': 'test',
                                    'CODEX_HOME': '/wrong', 'OPENAI_BASE_URL': 'wrong'}):
            env = environment()
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('CODEX_API_KEY', env)
        self.assertNotIn('OPENAI_BASE_URL', env)
        self.assertEqual(env['CODEX_HOME'], str(Path.home()/'.codex'))

    def test_completion_requires_actual_matching_finish_and_acceptance(self):
        task = {**self.task, 'status': 'completed', 'step': 1, 'steps': [{}],
                'result': {'outcome': 'completed', 'acceptance_complete': True, 'execution_stopped': True}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(verify_completion(task, root))
            save(root/'finish-response.json', task)
            save(root/'worker-report.json', {'outcome': 'completed'})
            self.assertTrue(verify_completion(task, root))
            for delta in ({'status': 'running'}, {'generation': 10}, {'id': 'other'}):
                save(root/'finish-response.json', {**task, **delta})
                self.assertFalse(verify_completion(task, root))
            save(root/'finish-response.json', task)
            for delta in ({'acceptance_complete': False}, {'execution_stopped': False}, {'outcome': 'blocked'}):
                self.assertFalse(verify_completion({**task, 'result': {**task['result'], **delta}}, root))

    def test_exact_account_model_reasoning_and_usage_are_required(self):
        import hashlib
        account = {'type': 'chatgpt', 'email': 'worker@example.test'}
        config = {'account_email_sha256': hashlib.sha256(account['email'].encode()).hexdigest(),
                  'model': 'selected-model', 'reasoning': 'medium'}
        models = [{'model': 'selected-model', 'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]}]
        self.assertEqual(check_account(account, models, {'ordinaryUsageAllowed': True}, config), config['account_email_sha256'])
        for a, m, r in [({**account, 'email': 'other@example.test'}, models, {'ordinaryUsageAllowed': True}),
                        (account, [], {'ordinaryUsageAllowed': True}),
                        (account, models, {'ordinaryUsageAllowed': False}),
                        (account, models, {})]:
            with self.assertRaises(ValueError):
                check_account(a, m, r, config)

    def test_noop_continuation_retains_only_same_task_owner_parent_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = {'run_id': 'a'*32, 'task': 'task', 'worker': 'mini'}
            child = {'run_id': 'b'*32, 'parent_run': parent['run_id'], 'task': 'task', 'worker': 'mini'}
            save(root/parent['run_id']/'state.json', parent)
            save(root/parent['run_id']/'finish-response.json', {'status': 'blocked'})
            self.assertEqual(prior_finish(root, child), root/parent['run_id']/'finish-response.json')
            for delta in ({'worker': 'other'}, {'task': 'other'}):
                self.assertIsNone(prior_finish(root, {**child, **delta}))


if __name__ == '__main__':
    unittest.main()
