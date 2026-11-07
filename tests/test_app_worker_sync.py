import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('app_worker_sync', Path(__file__).resolve().parents[1] / 'scripts/sync-app-worker.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class AppWorkerSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.skills = self.root / 'skills'; self.state = self.root / 'state'
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        for folder in sync.SOURCES.values():
            p = self.repo / folder; p.mkdir(parents=True)
            (p / 'SKILL.md').write_text('approved instructions')
        self.commit = self.commit_files()

    def git(self, *args):
        return sync.git(self.repo, *args).decode().strip()

    def commit_files(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def run_sync(self, commit=None):
        return sync.synchronize(self.repo, commit or self.commit, self.skills, self.state)

    def test_exact_commit_ignores_dirty_checkout_and_does_not_grant_readiness(self):
        (self.repo / 'skill/SKILL.md').write_text('unapproved working copy')
        result = self.run_sync()
        self.assertEqual('approved instructions', (self.skills / 'nova-coordination/SKILL.md').read_text())
        self.assertFalse(result['worker_readiness_established'])
        self.assertTrue(result['instruction_readback_required'])
        self.assertEqual(self.commit, result['source_commit'])
        self.assertEqual(0o600, (self.state / 'instruction-installation.json').stat().st_mode & 0o777)

    def test_preserves_local_changes_outside_discovery_and_is_idempotent(self):
        self.run_sync()
        (self.skills / 'nova-coordination/SKILL.md').write_text('local instructions')
        result = self.run_sync()
        self.assertEqual(['nova-coordination'], result['changed_skills'])
        self.assertEqual('local instructions', (Path(result['backup_path']) / 'nova-coordination/SKILL.md').read_text())
        self.assertEqual([], self.run_sync()['changed_skills'])

    def test_rejects_unpinned_revision_and_symlink_source_without_replacing(self):
        self.run_sync()
        before = sync.inventory(self.skills / 'nova-coordination')
        with self.assertRaises(ValueError): self.run_sync('main')
        (self.repo / 'skill/escape').symlink_to('/etc/passwd')
        revision = self.commit_files()
        with self.assertRaises(ValueError): self.run_sync(revision)
        self.assertEqual(before, sync.inventory(self.skills / 'nova-coordination'))

    def test_rejects_destination_symlink(self):
        self.skills.mkdir()
        (self.skills / 'nova-coordination').symlink_to(self.repo / 'skill')
        with self.assertRaises(ValueError): self.run_sync()
        self.assertEqual('approved instructions', (self.repo / 'skill/SKILL.md').read_text())

    def test_failure_restores_both_skills_and_preserves_previous_receipt(self):
        self.run_sync()
        old_receipt = (self.state / 'instruction-installation.json').read_bytes()
        for folder in sync.SOURCES.values():
            (self.repo / folder / 'SKILL.md').write_text('new instructions')
        revision = self.commit_files()
        original = sync.os.replace
        def fail_receipt(source, target):
            if Path(target).name == 'instruction-installation.json':
                raise OSError('simulated receipt failure')
            return original(source, target)
        with patch.object(sync.os, 'replace', side_effect=fail_receipt):
            with self.assertRaises(OSError): self.run_sync(revision)
        for name in sync.SOURCES:
            self.assertEqual('approved instructions', (self.skills / name / 'SKILL.md').read_text())
        self.assertEqual(old_receipt, (self.state / 'instruction-installation.json').read_bytes())

    def test_missing_skill_preserves_existing_installation(self):
        self.run_sync()
        (self.repo / 'skill/SKILL.md').unlink()
        revision = self.commit_files()
        with self.assertRaises(ValueError): self.run_sync(revision)
        self.assertEqual('approved instructions', (self.skills / 'nova-coordination/SKILL.md').read_text())


if __name__ == '__main__': unittest.main()
