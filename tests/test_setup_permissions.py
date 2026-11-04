"""Prompt-policy tests with mocked tools only; never request live OS consent."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("permission_setup", SCRIPTS / "setup-new-mac.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def status(**overrides):
    values = {"Accessibility": True, "Screen Recording": True, "Event Synthesizing": False}
    values.update(overrides)
    return SimpleNamespace(returncode=0, stdout=json.dumps({"data": {"permissions": [
        {"name": name, "isGranted": value} for name, value in values.items()]}}).encode())


class PermissionSetupTests(unittest.TestCase):
    def setup(self, selection=None):
        setup = m.Setup.__new__(m.Setup)
        setup.config = {} if selection is None else {"permissions": selection}
        setup.report = {"manual_steps": []}
        setup.browser = Path("/owned/source")
        setup.command = Mock(return_value=status())
        return setup

    def test_existing_grants_no_prompt_even_with_optional_event_denied(self):
        setup = self.setup()
        with patch.object(m, "require_interactive_desktop") as desktop, patch.object(m, "acknowledge") as ack:
            setup.permissions()
        desktop.assert_not_called()
        ack.assert_not_called()
        self.assertEqual(setup.command.call_count, 1)
        self.assertEqual(setup.report["permission_requests"], [])

    def test_only_missing_selected_category_is_requested_once(self):
        setup = self.setup()
        setup.command.side_effect = [status(**{"Screen Recording": False}), SimpleNamespace(returncode=0), status()]
        with patch.object(m, "require_interactive_desktop"), patch.object(m, "acknowledge") as ack, contextlib.redirect_stdout(io.StringIO()):
            setup.permissions()
        requests = [call.args[0] for call in setup.command.call_args_list if "request" in call.args[0]]
        self.assertEqual(requests, [["peekaboo", "permissions", "request", "screen-recording", "--no-remote"]])
        self.assertEqual(ack.call_count, 1)

    def test_denial_stops_without_retry(self):
        setup = self.setup()
        denied = status(**{"Accessibility": False})
        setup.command.side_effect = [denied, SimpleNamespace(returncode=1), denied]
        with patch.object(m, "require_interactive_desktop"), patch.object(m, "acknowledge"), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(m.SetupRequired):
            setup.permissions()
        self.assertEqual(setup.report["permission_requests"], ["accessibility"])
        self.assertEqual(setup.command.call_count, 3)

    def test_background_missing_consent_never_requests(self):
        setup = self.setup()
        setup.command.return_value = status(**{"Accessibility": False})
        with patch.object(m, "require_interactive_desktop", side_effect=m.SetupRequired("background")), self.assertRaises(m.SetupRequired):
            setup.permissions()
        self.assertEqual(setup.command.call_count, 1)
        self.assertEqual(setup.report["permission_requests"], [])

    def test_unknown_status_does_not_trigger_a_request(self):
        setup = self.setup()
        setup.command.return_value = status(**{"Accessibility": None})
        with self.assertRaises(m.SetupRequired):
            setup.permissions()
        self.assertEqual(setup.command.call_count, 1)

    def test_headless_selection_never_launches_anything(self):
        setup = self.setup({"peekaboo": [], "runner_families": []})
        with patch.object(m, "require_interactive_desktop") as desktop:
            setup.permissions()
            setup.runners()
        desktop.assert_not_called()
        setup.command.assert_not_called()

    def test_selected_runner_only_without_confirmation_relay(self):
        setup = self.setup()
        with patch.object(m, "require_interactive_desktop"), patch.object(m, "acknowledge") as ack, contextlib.redirect_stdout(io.StringIO()):
            setup.runners()
        self.assertEqual(setup.report["runner_permissions"], ["browser-features"])
        self.assertEqual(setup.command.call_count, 2)
        self.assertTrue(all("browser-features" in c.args[0] for c in setup.command.call_args_list))
        ack.assert_not_called()
        self.assertNotIn("permissions", m.DEPENDENCIES["runners"])

    def test_interactive_guard_rejects_ssh_and_nonterminal(self):
        for env, tty in (({"SSH_CONNECTION": "fixture"}, True), ({}, False)):
            with patch.dict(m.os.environ, env, clear=True), patch.object(m.os, "stat", return_value=SimpleNamespace(st_uid=m.os.getuid())), patch.object(m.sys.stdin, "isatty", return_value=tty), self.assertRaises(m.SetupRequired):
                m.require_interactive_desktop()

    def test_selection_rejects_unknown_duplicate_and_malformed_values(self):
        for selected in ({"peekaboo": ["automation"], "runner_families": []},
                         {"peekaboo": ["accessibility", "accessibility"], "runner_families": []},
                         {"peekaboo": [], "runner_families": ["new-feature-identity"]},
                         {"peekaboo": "accessibility", "runner_families": []},
                         {"peekaboo": [], "runner_families": [], "grant_all": True}):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                m.permission_plan({"permissions": selected})

    def test_plan_is_read_only_and_scope_specific(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = json.loads((SCRIPTS.parent / "setup/new-mac.example.json").read_text())
            config.pop("permissions")  # Old configs get the smaller defaults.
            p = root / "config.json"
            p.write_text(json.dumps(config))
            before = set(root.rglob("*"))
            for phase, expected in (("permissions", ["accessibility", "screen-recording"]), ("credential_sync", [])):
                result = subprocess.run([sys.executable, str(SCRIPTS / "setup-new-mac.py"), "--config", str(p), "--phase", phase], capture_output=True, text=True, check=True)
                plan = json.loads(result.stdout)
                self.assertEqual(plan["permission_setup"]["peekaboo"], expected)
                self.assertFalse(plan["changes_performed"])
                self.assertEqual(set(root.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
