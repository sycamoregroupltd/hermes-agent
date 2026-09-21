import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
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
        for key in ("RTB_OBSERVATION_PROTOCOL", "GUARD_TICK", "RTB_KEY", "RTB_SCRIPT",
                    "RTB_BOARD", "RTB_TITLE", "RTB_STATE_FILE", "RTB_STATE",
                    "RTB_ECHO_STDOUT", "RTB_TIMEOUT", "GUARD_BUNDLE_ROOT"):
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

    def test_inner_and_outer_lock_contention_are_silent(self):
        self.seed([], [])
        self.script("a.sh")
        self.script("b.sh")
        runner_fd = self.runner.acquire_single_instance("15m")
        try:
            with mock.patch.object(self.runner, "_emit_observation") as emit:
                self.assertEqual(self.invoke_runner(), 0)
                emit.assert_not_called()
        finally:
            os.close(self.runner._held_locks.pop())

        self.rtb.STATE = self.root / "rtb.json"
        self.script("no-op.sh")
        os.environ["RTB_SCRIPT"] = str(self.scripts / "no-op.sh")
        outer_fd = self.rtb._acquire_observation_lock("jarvis-os", "guard-bundle-15m")
        try:
            with mock.patch.object(self.rtb, "hermes") as hermes:
                self.assertEqual(self.rtb.main(), 0)
                hermes.assert_not_called()
        finally:
            self.rtb._release_observation_lock(outer_fd)

    def test_crash_after_prelaunch_keeps_in_flight_pending(self):
        self.seed(["A", "B"], ["A"])
        self.script("a.sh")
        self.script("b.sh")
        with mock.patch.object(self.runner, "run_check", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.invoke_runner()
        observation = json.loads(self.runner._observation_path("15m").read_text())
        self.assertEqual(observation["in_flight"], "A")
        self.assertEqual(observation["pending_recheck"], ["A", "B"])
        os.close(self.runner._held_locks.pop())

        with mock.patch.object(self.runner, "run_check", return_value=(0, "", True)):
            self.assertEqual(self.invoke_runner(), 0)
        observation = json.loads(self.runner._observation_path("15m").read_text())
        self.assertEqual(observation["in_flight"], None)
        self.assertEqual(observation["pending_recheck"], ["B"])

    def test_crash_after_prelaunch_adds_new_identity_to_pending(self):
        self.seed([], ["A"])
        self.script("a.sh")
        self.script("b.sh")
        with mock.patch.object(self.runner, "run_check", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.invoke_runner()
        observation = json.loads(self.runner._observation_path("15m").read_text())
        self.assertEqual(observation["in_flight"], "A")
        self.assertEqual(observation["pending_recheck"], ["A"])
        os.close(self.runner._held_locks.pop())

    def test_runner_lock_setup_failure_is_visible(self):
        self.seed([], [])
        with mock.patch.object(self.runner, "acquire_single_instance", return_value=None):
            self.assertEqual(self.invoke_runner(), 1)

    def test_malformed_observation_state_is_visible_failure(self):
        self.runner.STATE_FILE.write_text(json.dumps({"A": 0, "B": 0}))
        path = self.runner._observation_path("15m")
        path.write_text("not-json")
        with mock.patch.object(self.runner, "_emit_observation") as emit:
            self.assertEqual(self.invoke_runner(), 1)
            emit.assert_not_called()

    def test_observation_write_failure_is_visible_failure(self):
        self.seed([], ["A"])
        self.script("a.sh")
        self.script("b.sh")
        with mock.patch.object(self.runner, "save_observation_state",
                               side_effect=OSError("fixture fsync failure")):
            self.assertEqual(self.invoke_runner(), 1)

    def test_timestamp_write_failure_is_visible_failure(self):
        self.seed([], ["A"])
        self.script("a.sh")
        self.script("b.sh")
        original = self.runner._atomic_write_json

        def fail_timestamp(path, payload):
            if Path(path) == self.runner.STATE_FILE:
                raise OSError("fixture timestamp replace failure")
            return original(path, payload)

        with mock.patch.object(self.runner, "_atomic_write_json", side_effect=fail_timestamp):
            self.assertEqual(self.invoke_runner(), 1)

    def test_rtb_state_file_vetoes_clean_but_failure_bypasses_veto(self):
        self.rtb.STATE = self.root / "rtb.json"
        active = self.root / "still-active.json"
        active.write_text("active")
        self.rtb.STATE.write_text(json.dumps({
            "guard-bundle-15m": {"card_id": "t_aabbccdd", "digest": "old",
                                  "board": "jarvis-os"}
        }))
        self.script("clean.sh", "printf 'GUARD_BUNDLE_OBSERVATION_V1 CLEAN\\n'")
        os.environ.update({"RTB_SCRIPT": str(self.scripts / "clean.sh"),
                           "RTB_STATE_FILE": str(active)})
        calls = []

        def active_hermes(*args, **kwargs):
            calls.append(args)
            if "show" in args:
                return 0, "status: ready"
            return 0, "ok"

        with mock.patch.object(self.rtb, "hermes", side_effect=active_hermes):
            self.assertEqual(self.rtb.main(), 0)
        self.assertFalse(any("complete" in call or "archive" in call for call in calls))
        self.assertTrue(any("comment" in call for call in calls))
        self.assertTrue(self.rtb.STATE.exists())

        self.script("fail.sh", "printf 'failure\\n'; exit 1")
        os.environ["RTB_SCRIPT"] = str(self.scripts / "fail.sh")
        calls.clear()
        with mock.patch.object(self.rtb, "hermes", side_effect=active_hermes):
            self.assertEqual(self.rtb.main(), 1)
        self.assertTrue(any("show" in call for call in calls))
        self.assertTrue(self.rtb.STATE.exists())

    def test_unknown_card_status_is_fail_closed_and_preserves_mapping(self):
        self.rtb.STATE = self.root / "rtb.json"
        self.rtb.STATE.write_text(json.dumps({
            "guard-bundle-15m": {"card_id": "t_11223344", "digest": "old",
                                  "board": "jarvis-os"}
        }))
        self.script("clean.sh", "printf 'GUARD_BUNDLE_OBSERVATION_V1 CLEAN\\n'")
        os.environ.update({"RTB_SCRIPT": str(self.scripts / "clean.sh"),
                           "RTB_STATE_FILE": ""})
        with mock.patch.object(self.rtb, "hermes", return_value=(0, "status: mystery")) as hermes:
            self.assertEqual(self.rtb.main(), 1)
        hermes.assert_called_once()
        self.assertIn("t_11223344", self.rtb.STATE.read_text())

    def test_board_api_failure_preserves_mapping_and_is_loud(self):
        self.rtb.STATE = self.root / "rtb.json"
        self.rtb.STATE.write_text(json.dumps({
            "guard-bundle-15m": {"card_id": "t_55667788", "digest": "old",
                                  "board": "jarvis-os"}
        }))
        self.script("clean.sh", "printf 'GUARD_BUNDLE_OBSERVATION_V1 CLEAN\\n'")
        os.environ.update({"RTB_SCRIPT": str(self.scripts / "clean.sh"),
                           "RTB_STATE_FILE": ""})

        def failing_hermes(*args, **kwargs):
            if "show" in args:
                return 0, "status: ready"
            if "complete" in args:
                return 1, "board API unavailable"
            return 0, "ok"

        with mock.patch.object(self.rtb, "hermes", side_effect=failing_hermes):
            self.assertEqual(self.rtb.main(), 1)
        self.assertIn("t_55667788", self.rtb.STATE.read_text())

    def test_archive_api_failure_preserves_mapping_and_is_loud(self):
        self.rtb.STATE = self.root / "rtb.json"
        self.rtb.STATE.write_text(json.dumps({
            "guard-bundle-15m": {"card_id": "t_33445566", "digest": "old",
                                  "board": "jarvis-os"}
        }))
        self.script("clean.sh", "printf 'GUARD_BUNDLE_OBSERVATION_V1 CLEAN\\n'")
        os.environ.update({"RTB_SCRIPT": str(self.scripts / "clean.sh"),
                           "RTB_STATE_FILE": ""})

        def failing_archive(*args, **kwargs):
            if "show" in args:
                return 0, "status: done"
            if "archive" in args:
                return 1, "archive unavailable"
            return 0, "ok"

        with mock.patch.object(self.rtb, "hermes", side_effect=failing_archive):
            self.assertEqual(self.rtb.main(), 1)
        self.assertIn("t_33445566", self.rtb.STATE.read_text())

    def test_same_key_lock_spans_slow_producer_and_application(self):
        self.rtb.STATE = self.root / "rtb.json"
        self.script("slow.sh")
        os.environ.update({"RTB_SCRIPT": str(self.scripts / "slow.sh"),
                           "RTB_STATE_FILE": ""})
        started = threading.Event()
        release = threading.Event()
        result = []

        def slow_run(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(3))
            return subprocess.CompletedProcess(
                ["bash", "slow.sh"], 0,
                stdout="\n".join(["GUARD_BUNDLE_OBSERVATION_V1 CLEAN", ""]), stderr=""
            )

        with mock.patch.object(self.rtb.subprocess, "run", side_effect=slow_run) as run:
            worker = threading.Thread(target=lambda: result.append(self.rtb.main()))
            worker.start()
            self.assertTrue(started.wait(3))
            self.assertEqual(self.rtb.main(), 0)
            self.assertEqual(run.call_count, 1)
            release.set()
            worker.join(3)
        self.assertEqual(result, [0])

    def test_isolated_real_entrypoint_chain_and_exact_copies(self):
        candidate = {
            "runner": RUNNER_PATH,
            "report_to_board": RTB_PATH,
            "shim": ROOT / "profiles/jarvis/scripts/guard_bundle_tick_15m.sh",
            "wrapper": ROOT / "scripts/guard_bundle_run.sh",
        }
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            hermes_root = tmp / "hermes"
            profile_scripts = hermes_root / "profiles/jarvis/scripts"
            root_scripts = hermes_root / "scripts"
            profile_scripts.mkdir(parents=True)
            root_scripts.mkdir(parents=True)
            copied = {}
            for name, source in candidate.items():
                destination = (profile_scripts / source.name if name in {"runner", "shim"}
                               else root_scripts / source.name)
                shutil.copyfile(source, destination)
                copied[name] = destination
                self.assertEqual(destination.read_bytes(), source.read_bytes())
                self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(),
                                 hashlib.sha256(source.read_bytes()).hexdigest())

            # Replace every manifest member with a harmless fixture. The runner,
            # wrapper, report consumer, and shim remain the exact candidate bytes.
            chain_runner = load_module("guard_runner_chain_fixture", RUNNER_PATH)
            for spec in chain_runner.CHECKS.values():
                fixture = profile_scripts / spec["script"]
                fixture.parent.mkdir(parents=True, exist_ok=True)
                if fixture.suffix == ".py":
                    fixture.write_text("\n".join(["import sys", "sys.exit(0)", ""]))
                else:
                    fixture.write_text("\n".join(["#!/usr/bin/env bash", "exit 0", ""]))
                fixture.chmod(fixture.stat().st_mode | stat.S_IXUSR)

            report_state = tmp / "report-to-board.json"
            report_state.write_text(json.dumps({
                "guard-bundle-15m": {"card_id": "t_abcdef12", "digest": "old",
                                      "board": "jarvis-os"}
            }))
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            hermes_log = tmp / "hermes-calls.log"
            fake_hermes = bin_dir / "hermes"
            fake_hermes.write_text(
                "\n".join([
                    "#!/usr/bin/env bash",
                    f"printf '%s\\n' \"$*\" >> {hermes_log}",
                    "case \"$*\" in *' show '*) printf 'status: ready\\n';; esac",
                    "exit 0",
                    "",
                ])
            )
            fake_hermes.chmod(fake_hermes.stat().st_mode | stat.S_IXUSR)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("HERMES_KANBAN_")}
            env.update({
                "GUARD_BUNDLE_ROOT": str(hermes_root),
                "RTB_STATE": str(report_state),
                "RTB_ECHO_STDOUT": "1",
                "PATH": f"{bin_dir}:{env.get('PATH', '')}",
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            proc = subprocess.run(["bash", str(copied["shim"])], env=env,
                                  capture_output=True, text=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stdout + "\\n" + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn("show", hermes_log.read_text())
            self.assertIn("complete", hermes_log.read_text())
            self.assertIn("archive", hermes_log.read_text())
            self.assertEqual(json.loads(report_state.read_text()), {})
            observation = json.loads(
                (hermes_root / "profiles/jarvis/cron/state/"
                 "guard_bundle_observations_15m.json").read_text())
            self.assertEqual(observation["pending_recheck"], [])

    def test_child_rtb_environment_isolated_from_parent(self):
        self.script("child.sh")
        parent_keys = {"RTB_SCRIPT": "parent-script", "RTB_KEY": "parent-key",
                       "RTB_STATE": "parent-state", "RTB_BOARD": "parent-board"}
        os.environ.update(parent_keys)
        captured = {}

        def capture_run(*args, **kwargs):
            captured.update(kwargs["env"])
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(self.runner.subprocess, "run", side_effect=capture_run):
            rc, out, attempted = self.runner.run_check("A", {"script": "child.sh"}, 30)
        self.assertEqual((rc, out, attempted), (0, "", True))
        self.assertTrue(all(not key.startswith("RTB_") for key in captured))
        for key, value in parent_keys.items():
            self.assertEqual(os.environ[key], value)

    def test_legacy_success_and_timeout_behavior_is_preserved(self):
        os.environ.pop("RTB_OBSERVATION_PROTOCOL", None)
        self.rtb.STATE = self.root / "rtb.json"
        self.script("healthy.sh")
        os.environ.update({"RTB_SCRIPT": str(self.scripts / "healthy.sh"),
                           "RTB_KEY": "legacy-healthy", "RTB_STATE_FILE": ""})
        with mock.patch.object(self.rtb, "hermes") as hermes:
            self.assertEqual(self.rtb.main(), 0)
            hermes.assert_not_called()

        self.script("timeout.sh")
        os.environ.update({"RTB_SCRIPT": str(self.scripts / "timeout.sh"),
                           "RTB_KEY": "legacy-timeout"})
        self.rtb.RTB_TIMEOUT = 1
        calls = []

        def fake_hermes(*args, **kwargs):
            calls.append(args)
            return (0, "created t_1234abcd") if "create" in args else (0, "")

        timeout = subprocess.TimeoutExpired(["bash", "timeout.sh"], 1,
                                            output=b"partial", stderr=b"diagnostic")
        with mock.patch.object(self.rtb.subprocess, "run", side_effect=timeout), \
             mock.patch.object(self.rtb, "hermes", side_effect=fake_hermes):
            self.assertEqual(self.rtb.main(), 124)
        self.assertTrue(any("create" in call for call in calls))
        self.assertTrue(any("ABORTED" in str(call) for call in calls))

    def test_no_due_active_incident_emits_no_due_and_stays_open(self):
        self.seed(["B"], [])
        self.script("a.sh")
        self.script("b.sh")
        with mock.patch.object(self.runner, "_emit_observation", wraps=self.runner._emit_observation) as emit:
            rc = self.invoke_runner()
        self.assertEqual(rc, 0)
        emit.assert_called_once_with("NO_DUE_CHECKS")

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

    def test_protocol_disabled_marker_is_ordinary_legacy_report(self):
        self.rtb.STATE = self.root / "rtb.json"
        self.script("legacy-marker.sh", "printf 'GUARD_BUNDLE_OBSERVATION_V1 CLEAN\\n'")
        os.environ.update({"RTB_SCRIPT": str(self.scripts / "legacy-marker.sh"),
                           "RTB_KEY": "legacy-marker", "RTB_BOARD": "jarvis-os"})
        os.environ.pop("RTB_OBSERVATION_PROTOCOL", None)
        calls = []

        def fake_hermes(*args, **kwargs):
            calls.append(args)
            return (0, "created t_abcddcba") if "create" in args else (0, "")

        with mock.patch.object(self.rtb, "hermes", side_effect=fake_hermes):
            self.assertEqual(self.rtb.main(), 0)
        self.assertTrue(any("create" in call for call in calls))
        self.assertIn("legacy-marker", self.rtb.STATE.read_text())

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
    def test_no_incident_no_due_emits_no_due_without_state_change(self):
        self.seed([], [])
        self.script("a.sh")
        self.script("b.sh")
        with mock.patch.object(self.runner, "_emit_observation", wraps=self.runner._emit_observation) as emit:
            self.assertEqual(self.invoke_runner(), 0)
        emit.assert_called_once_with("NO_DUE_CHECKS")

    def test_legacy_nonzero_nonempty_report_remains_one_key_failure(self):
        self.rtb.STATE = self.root / "rtb.json"
        self.script("fail.sh", "printf 'legacy diagnostic\\n'; exit 1")
        os.environ.update({"RTB_OBSERVATION_PROTOCOL": "", "RTB_SCRIPT": str(self.scripts / "fail.sh"),
                           "RTB_KEY": "legacy-nonempty", "RTB_BOARD": "jarvis-os"})
        calls = []

        def fake_hermes(*args, **kwargs):
            calls.append(args)
            return (0, "created t_aa11bb22") if "create" in args else (0, "")

        with mock.patch.object(self.rtb, "hermes", side_effect=fake_hermes):
            self.assertEqual(self.rtb.main(), 1)
        self.assertTrue(any("create" in call for call in calls))
        self.assertTrue(any("legacy diagnostic" in str(call) for call in calls))

    def test_failure_dominates_deferred_work(self):
        self.seed(["A", "B"], ["A", "B"])
        self.script("a.sh", "exit 1")
        self.script("b.sh")
        with mock.patch.object(self.runner, "_emit_observation") as emit:
            self.assertEqual(self.invoke_runner(), 1)
            emit.assert_not_called()

    def test_budget_refusal_is_deferred_without_timestamp_advance(self):
        self.seed(["A", "B"], ["A"])
        self.script("a.sh")
        self.script("b.sh")
        self.runner.BUDGETS["15m"] = 0
        try:
            with mock.patch.object(self.runner, "_emit_observation", wraps=self.runner._emit_observation) as emit:
                self.assertEqual(self.invoke_runner(), 0)
            emit.assert_called_once_with("DEFERRED")
        finally:
            self.runner.BUDGETS["15m"] = 60
        self.assertEqual(json.loads(self.runner.STATE_FILE.read_text())["A"], 0)

    def test_missing_sidecar_bootstraps_all_members_before_partial_pass(self):
        self.runner.STATE_FILE.write_text(json.dumps({"A": 0, "B": 2_000_000_000}))
        self.script("a.sh")
        self.script("b.sh")
        with mock.patch.object(self.runner, "run_check", return_value=(0, "", True)):
            self.assertEqual(self.invoke_runner(), 0)
        observation = json.loads(self.runner._observation_path("15m").read_text())
        self.assertEqual(observation["pending_recheck"], ["B"])

    def test_echo_suppresses_control_records_but_preserves_human_failure(self):
        self.rtb.STATE = self.root / "rtb.json"
        self.script("clean.sh", "printf 'GUARD_BUNDLE_OBSERVATION_V1 CLEAN\\n'")
        os.environ.update({"RTB_SCRIPT": str(self.scripts / "clean.sh"),
                           "RTB_ECHO_STDOUT": "1"})
        clean_output = io.StringIO()
        with mock.patch.object(self.rtb, "hermes"), redirect_stdout(clean_output):
            self.assertEqual(self.rtb.main(), 0)
        self.assertEqual(clean_output.getvalue(), "")

        self.script("fail.sh", "printf 'human failure\\n'; exit 1")
        os.environ["RTB_SCRIPT"] = str(self.scripts / "fail.sh")
        calls = []

        def fake_hermes(*args, **kwargs):
            calls.append(args)
            return (0, "created t_ee11ff22") if "create" in args else (0, "")

        failure_output = io.StringIO()
        with mock.patch.object(self.rtb, "hermes", side_effect=fake_hermes), \
             redirect_stdout(failure_output):
            self.assertEqual(self.rtb.main(), 1)
        self.assertIn("human failure", failure_output.getvalue())

    def test_running_owner_gets_recovery_comment_not_lifecycle_mutation(self):
        self.rtb.STATE = self.root / "rtb.json"
        self.rtb.STATE.write_text(json.dumps({
            "guard-bundle-15m": {"card_id": "t_77889900", "digest": "old",
                                  "board": "jarvis-os"}
        }))
        self.script("clean.sh", "printf 'GUARD_BUNDLE_OBSERVATION_V1 CLEAN\\n'")
        os.environ["RTB_SCRIPT"] = str(self.scripts / "clean.sh")
        calls = []

        def owner_hermes(*args, **kwargs):
            calls.append(args)
            return (0, "status: running") if "show" in args else (0, "ok")

        with mock.patch.object(self.rtb, "hermes", side_effect=owner_hermes):
            self.assertEqual(self.rtb.main(), 0)
        self.assertTrue(any("comment" in call and "pending" in str(call) for call in calls))
        self.assertFalse(any("complete" in call or "archive" in call for call in calls))

    def test_repeated_failure_digest_does_not_duplicate_comment(self):
        self.rtb.STATE = self.root / "rtb.json"
        self.script("fail.sh", "printf 'same failure\\n'; exit 1")
        os.environ.update({"RTB_SCRIPT": str(self.scripts / "fail.sh"), "RTB_KEY": "repeat-failure"})
        calls = []

        def fake_hermes(*args, **kwargs):
            calls.append(args)
            if "create" in args:
                return 0, "created t_8899aabb"
            if "show" in args:
                return 0, "status: ready"
            return 0, "ok"

        with mock.patch.object(self.rtb, "hermes", side_effect=fake_hermes):
            self.assertEqual(self.rtb.main(), 1)
            calls.clear()
            self.assertEqual(self.rtb.main(), 1)
        self.assertTrue(any("show" in call for call in calls))
        self.assertFalse(any("comment" in call for call in calls))

    def test_membership_change_retains_removed_pending_identity(self):
        self.runner.STATE_FILE.write_text(json.dumps({"A": 2_000_000_000, "B": 2_000_000_000}))
        self.runner._observation_path("15m").write_text(json.dumps({
            "version": 1, "bundle": "15m", "members": ["A", "C"],
            "pending_recheck": ["C"], "in_flight": None,
        }))
        with mock.patch.object(self.runner, "_emit_observation", wraps=self.runner._emit_observation) as emit:
            self.assertEqual(self.invoke_runner(), 0)
        emit.assert_called_once_with("NO_DUE_CHECKS")
        observation = json.loads(self.runner._observation_path("15m").read_text())
        self.assertEqual(observation["members"], ["A", "B"])
        self.assertEqual(observation["pending_recheck"], ["A", "B", "C"])


if __name__ == "__main__":
    unittest.main()
