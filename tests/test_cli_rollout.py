import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('rollout', Path(__file__).resolve().parents[1]/'scripts/rollout-cli-results.py')
rollout = importlib.util.module_from_spec(spec); spec.loader.exec_module(rollout)


class RolloutTests(unittest.TestCase):
    def test_v2_includes_qualified_result_dependency_without_migrating_v1_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); source = root/'source'; installed = root/'installed'
            source.mkdir(); installed.mkdir()
            for name in rollout.EVENT_FILES_V2: (source/name).write_text('value = 2\n')
            old = root/'old'; previous = rollout.plan(source, installed, old, 'mini', profile='event-dispatch')
            new = root/'new'; current = rollout.plan(source, installed, new, 'mini', profile='event-dispatch-v2')
            self.assertNotIn('cli_results.py', previous['files'])
            self.assertIn('cli_results.py', current['files'])
            rollout.apply(old, 'mini'); self.assertFalse((installed/'cli_results.py').exists())
            rollout.apply(new, 'mini'); self.assertTrue((installed/'cli_results.py').exists())
    def test_event_profile_preserves_unrelated_modules_and_rolls_back_absent_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); source = root/'source'; destination = root/'installed'
            source.mkdir(); destination.mkdir()
            for name in rollout.EVENT_FILES: (source/name).write_text('value = 2\n')
            (destination/'client.py').write_text('value = 1\n')
            (destination/'cli_owner_review.py').write_text('preserved = True\n')
            plan = root/'plan'; manifest = rollout.plan(source, destination, plan, 'mini', profile='event-dispatch')
            self.assertEqual(set(manifest['files']), set(rollout.EVENT_FILES))
            rollout.apply(plan, 'mini')
            self.assertEqual((destination/'cli_owner_review.py').read_text(), 'preserved = True\n')
            rollout.apply(plan, 'mini', True)
            self.assertFalse((destination/'cli_event_dispatch.py').exists())
            self.assertEqual((destination/'client.py').read_text(), 'value = 1\n')

    def test_recovery_profile_installs_exact_dependency_and_rolls_back_without_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); source = root/'source'; installed = root/'installed'
            source.mkdir(); installed.mkdir()
            for name in rollout.RECOVERY_FILES_V1: (source/name).write_text('value = 2\n')
            (installed/'cli_dispatch.py').write_text('value = 1\n')
            run = installed/'state.json'; run.write_bytes(b'preserve held run')
            plan = root/'plan'; manifest = rollout.plan(source, installed, plan, 'mini', profile='scoped-recovery-v1')
            self.assertEqual(set(manifest['files']), set(rollout.RECOVERY_FILES_V1))
            rollout.apply(plan, 'mini'); self.assertTrue((installed/'cli_recovery.py').is_file())
            self.assertEqual(run.read_bytes(), b'preserve held run')
            rollout.apply(plan, 'mini', True)
            self.assertFalse((installed/'cli_recovery.py').exists())
            self.assertEqual((installed/'cli_dispatch.py').read_text(), 'value = 1\n')
            self.assertEqual(run.read_bytes(), b'preserve held run')

    def test_arbitrary_rollout_profile_refuses_before_creating_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaises(ValueError): rollout.plan(root, root, root/'plan', 'mini', profile='arbitrary')
            self.assertFalse((root/'plan').exists())
    def test_exact_apply_partial_recovery_rollback_and_unknown_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); source = root/'source'; destination = root/'installed'
            source.mkdir(); destination.mkdir()
            for name in rollout.FILES: (source/name).write_text('value = 2\n')
            (destination/'client.py').write_text('value = 1\n')
            plan = root/'plan'; manifest = rollout.plan(source, destination, plan, 'mini')
            originals = {p.name: p.read_bytes() for p in plan.iterdir()}
            # Simulate interruption after dependency install. Only exact known
            # before/after bytes are recoverable; third-party changes refuse.
            (destination/rollout.FILES[0]).write_bytes((source/rollout.FILES[0]).read_bytes())
            receipt = rollout.apply(plan, 'mini')
            self.assertTrue(receipt['verified'])
            rollout.apply(plan, 'mini')  # exact replay is harmless
            for name in rollout.FILES: self.assertEqual((destination/name).read_bytes(), (source/name).read_bytes())
            (destination/'client.py').write_text('foreign change\n')
            with self.assertRaises(ValueError): rollout.apply(plan, 'mini', True)
            self.assertTrue((destination/'cli_dispatch.py').exists())
            (destination/'client.py').write_bytes((source/'client.py').read_bytes())
            rollout.apply(plan, 'mini', True)
            self.assertEqual((destination/'client.py').read_text(), 'value = 1\n')
            self.assertEqual(list(destination.iterdir()), [destination/'client.py'])
            for name, raw in originals.items(): self.assertEqual((plan/name).read_bytes(), raw)
            with self.assertRaises(ValueError): rollout.apply(plan, 'foreign')

    def test_modified_candidate_or_symlink_refuses_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); source = root/'source'; destination = root/'installed'
            source.mkdir(); destination.mkdir()
            for name in rollout.FILES: (source/name).write_text('pass\n')
            plan = root/'plan'; rollout.plan(source, destination, plan, 'mini')
            (plan/'cli_dispatch.py.after').write_text('changed\n')
            with self.assertRaises(ValueError): rollout.apply(plan, 'mini')
            self.assertEqual(list(destination.iterdir()), [])
            (plan/'cli_dispatch.py.after').unlink()
            (plan/'cli_dispatch.py.after').symlink_to(source/'cli_dispatch.py')
            with self.assertRaises(ValueError): rollout.apply(plan, 'mini')
            self.assertEqual(list(destination.iterdir()), [])
