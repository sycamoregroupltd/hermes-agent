import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNNER_PATH = ROOT / "profiles/jarvis/scripts/cron_guard_bundle_runner.py"
RTB_PATH = ROOT / "scripts/report-to-board.py"


class GuardBundleObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.scripts = self.root / "scripts"
        self.state_dir = self.root / "cron/state"
        self.scripts.mkdir(parents=True)
        self.state_dir.mkdir(parents=True)
        self.runner = load_module("guard_runner_fixture", RUNNER_PATH)
        self.rtb = load_module("report_to_board_fixture", RTB_PATH)
        self.runner.HERMES_HOME = self.root
        self.runner.SCRIPTS_DIR = self.scripts
        self.runner.CRON_DIR = self.root / "cron"
        self.runner.STATE_FILE = self.state_dir / "guard_bundle_last_run.json"
        self.runner.BUNDLES = {"15m": ["A", "B"]}
        self.runner.CHECKS = {
            "A": {"script": "a.sh", "interval": 900},
            "B": {"script": "b.sh", "interval": 1800},
        }
        self.runner.BUDGETS = {"15m": 60}
        self.runner._held_locks.clear()
        os.environ.update({
            "RTB_OBSERVATION_PROTOCOL": "guard-bundle-v1",
            "GUARD_TICK": "15m",
            "RTB_KEY": "guard-bundle-15m",
            "RTB_SCRIPT": "/home/frank/.hermes/scripts/guard_bundle_run.sh",
        })

    def tearDown(self):
        for fd in self.runner._held_locks:
            try:
                os.close(fd)
            except OSError:
                pass
        self.runner._held_locks.clear()
        self.tmp.cleanup()
        for key in ("RTB_OBSERVATION_PROTOCOL", "GUARD_TICK", "RTB_KEY", "RTB_SCRIPT"):
            os.environ.pop(key, None)

    def script(self, name, body="exit 0"):
        path = self.scripts / name
        path.write_text(f"#!/usr/bin/env bash\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def invoke_runner(self):
        with mock.patch.object(self.runner.sys, "argv", [str(RUNNER_PATH), "15m"]), \
             mock.patch.object(self.runner.time, "time", return_value=2_000_000_000):
            return self.runner.main()

    def seed(self, pending, due):
        self.runner.STATE_FILE.write_text("{}\n")
        sidecar = {
            "version": 1,
            "bundle": "15m",
            "members": ["A", "B"],
            "pending_recheck": sorted(pending),
            "in_flight": None,
        }
        self.runner._observation_path("15m").write_text(__import__("json").dumps(sidecar))
        state = {name: 0 if name in due else 2_000_000_000 for name in ("A", "B")}
        self.runner.STATE_FILE.write_text(__import__("json").dumps(state))

    def test_no_due_active_incident_is_deferred(self):
        self.seed(["B"], [])
        self.script("a.sh")
        self.script("b.sh")
        with mock.patch.object(self.runner, "_emit_observation", wraps=self.runner._emit_observation) as emit:
            rc = self.invoke_runner()
        self.assertEqual(rc, 0)
        emit.assert_called_once_with("DEFERRED")

    def test_due_clean_clears_pending_debt(self):
        self.seed(["B"], ["B"])
        self.script("a.sh")
        self.script("b.sh")
        with mock.patch.object(self.runner, "_emit_observation", wraps=self.runner._emit_observation) as emit:
            rc = self.invoke_runner()
        self.assertEqual(rc, 0)
        emit.assert_called_once_with("CLEAN")
        self.assertEqual(__import__("json").loads(self.runner._observation_path("15m").read_text())["pending_recheck"], [])

    def test_sibling_clean_does_not_clear_failed_member(self):
        self.seed(["B"], ["A"])
        self.script("a.sh")
        self.script("b.sh")
        with mock.patch.object(self.runner, "_emit_observation", wraps=self.runner._emit_observation) as emit:
            rc = self.invoke_runner()
        self.assertEqual(rc, 0)
        emit.assert_called_once_with("DEFERRED")
        self.assertEqual(__import__("json").loads(self.runner._observation_path("15m").read_text())["pending_recheck"], ["B"])

    def test_nonzero_empty_legacy_report_stays_loud(self):
        self.script("fail.sh", "exit 1")
        state_path = self.root / "rtb.json"
        self.rtb.STATE = state_path
        os.environ.update({
            "RTB_SCRIPT": str(self.scripts / "fail.sh"),
            "RTB_KEY": "legacy-failure",
            "RTB_BOARD": "jarvis-os",
        })
        calls = []

        def fake_hermes(*args, **kwargs):
            calls.append(args)
            if "create" in args:
                return 0, "created t_deadbeef"
            return 0, ""

        with mock.patch.object(self.rtb, "hermes", side_effect=fake_hermes):
            rc = self.rtb.main()
        self.assertEqual(rc, 1)
        self.assertTrue(any("create" in call for call in calls))
        self.assertIn("legacy-failure", state_path.read_text())

    def test_protocol_no_due_does_not_touch_rtb_state(self):
        self.seed([], [])
        self.script("a.sh", "printf 'GUARD_BUNDLE_OBSERVATION_V1 NO_DUE_CHECKS\\n'")
        self.rtb.STATE = self.root / "rtb.json"
        os.environ.update({
            "RTB_SCRIPT": str(self.scripts / "a.sh"),
            "RTB_KEY": "guard-bundle-15m",
            "RTB_BOARD": "jarvis-os",
        })
        with mock.patch.object(self.rtb, "hermes") as hermes:
            self.assertEqual(self.rtb.main(), 0)
        hermes.assert_not_called()
        self.assertFalse(self.rtb.STATE.exists())

    def test_protocol_malformed_success_is_loud_and_creates_card(self):
        self.rtb.STATE = self.root / "rtb.json"
        self.script("bad-marker.sh", "printf 'WRONG\\n'")
        os.environ.update({
            "RTB_SCRIPT": str(self.scripts / "bad-marker.sh"),
            "RTB_KEY": "guard-bundle-15m",
            "RTB_BOARD": "jarvis-os",
        })
        calls = []

        def fake_hermes(*args, **kwargs):
            calls.append(args)
            if "create" in args:
                return 0, "created t_bad0a001"
            return 0, ""

        with mock.patch.object(self.rtb, "hermes", side_effect=fake_hermes):
            rc = self.rtb.main()
        self.assertEqual(rc, 1)
        self.assertTrue(any("create" in call for call in calls))
        self.assertIn("guard-bundle-15m", self.rtb.STATE.read_text())

    def test_protocol_nonzero_marker_is_failure_not_clear(self):
        self.rtb.STATE = self.root / "rtb.json"
        self.script("failed-marker.sh", "printf 'GUARD_BUNDLE_OBSERVATION_V1 CLEAN\\n'; exit 1")
        os.environ.update({
            "RTB_SCRIPT": str(self.scripts / "failed-marker.sh"),
            "RTB_KEY": "guard-bundle-15m",
            "RTB_BOARD": "jarvis-os",
        })
        calls = []

        def fake_hermes(*args, **kwargs):
            calls.append(args)
            if "create" in args:
                return 0, "created t_fa110001"
            return 0, ""

        with mock.patch.object(self.rtb, "hermes", side_effect=fake_hermes):
            rc = self.rtb.main()
        self.assertEqual(rc, 1)
        self.assertTrue(any("create" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
