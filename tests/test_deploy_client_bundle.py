import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from deploy import deploy as deployment


class SeparateClientBundleTests(unittest.TestCase):
    def test_code_only_preserves_live_settings_and_can_leave_newer_client_bundle_alone(self):
        for skip in (False, True):
            with self.subTest(skip=skip), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                live = {'metadata': {'resourceVersion': '123'}, 'spec': {'template': {'spec': {
                    'volumes': [{'name': 'tls', 'secret': {'secretName': 'existing-tls'}},
                                {'name': 'source', 'configMap': {'name': 'old-code'}}],
                    'containers': [{'image': 'existing-pinned-image', 'env': [{'name': 'KEEP', 'value': 'existing'}]}]}}}}
                before = copy.deepcopy(live)
                code = {'kind': 'ConfigMap', 'metadata': {'name': 'new-code'}, 'immutable': True, 'data': {}}
                desired = {'kind': 'Deployment', 'spec': {'template': {'spec': {'volumes': [
                    {'name': 'source', 'configMap': {'name': 'new-code', 'items': []}}]}}}}
                cluster = Mock(); cluster.existing.return_value = True
                cluster.run.side_effect = [json.dumps(live).encode(), b'']
                argv = ['deploy.py', '--apply', '--code-only'] + (['--skip-client-package'] if skip else [])
                with patch.object(deployment, 'ROOT', root), patch.object(deployment, 'Cluster', return_value=cluster), \
                     patch.object(deployment, 'manifests', return_value=([code, desired], 'digest')), \
                     patch.object(deployment.subprocess, 'run') as package, patch('sys.argv', argv), \
                     contextlib.redirect_stdout(io.StringIO()):
                    deployment.main()
                cluster.apply.assert_called_once_with(code)
                patch_args, patch_body = cluster.run.call_args.args
                self.assertIn('patch', patch_args)
                self.assertEqual([
                    {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '123'},
                    {'op': 'replace', 'path': '/spec/template/spec/volumes/1/configMap',
                     'value': {'name': 'new-code', 'items': []}}], json.loads(patch_body))
                self.assertEqual(before, live)
                if skip: package.assert_not_called()
                else:
                    package.assert_called_once()
                    self.assertIn(str(root/'scripts/package-client.py'), package.call_args.args[0])

    def test_skip_flag_refuses_full_deployment_or_tls_changes_before_any_operation(self):
        for flags in (['--apply'], ['--apply', '--prepare-tls'], ['--apply', '--code-only', '--prepare-tls']):
            with self.subTest(flags=flags), patch.object(deployment, 'Cluster') as cluster, \
                 patch.object(deployment, 'manifests') as manifests, patch.object(deployment.subprocess, 'run') as package, \
                 patch('sys.argv', ['deploy.py', '--skip-client-package', *flags]), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit): deployment.main()
                cluster.assert_not_called(); manifests.assert_not_called(); package.assert_not_called()


if __name__ == '__main__':
    unittest.main()
