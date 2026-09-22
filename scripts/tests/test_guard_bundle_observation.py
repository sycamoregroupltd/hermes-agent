#!/usr/bin/env python3
"""Guard-bundle qualified-observation tests (t_d14445d2).

Covers the opt-in observation protocol added to
`profiles/jarvis/scripts/cron_guard_bundle_runner.py` (producer) and
`scripts/report-to-board.py` (consumer), and — just as important — that every
non-opted-in behavior is byte-identical to the pre-change live copies.

Harness rules:
  * the runner's module-level paths are re-pointed at a temp root, so the real
    cron state (/home/frank/.hermes/profiles/jarvis/cron/state) is never read
    or written;
  * RTB_STATE is set to a temp file BEFORE the consumer module is imported
    (the module computes STATE at import time);
  * the producer subprocess and every `hermes` board call are doubles. No test
    in this file touches the live kanban DB or the live board.

Requirement ids (r0x) refer to the design contract
`Architecture/2026-09-07-guard-bundle-observation-contract-t_t_8f5e42af`.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "profiles" / "jarvis" / "scripts" / "cron_guard_bundle_runner.py"
CONSUMER_PATH = ROOT / "scripts" / "report-to-board.py"
SHIM_15M = ROOT / "profiles" / "jarvis" / "scripts" / "guard_bundle_tick_15m.sh"
WRAPPER = ROOT / "scripts" / "guard_bundle_run.sh"

PROTOCOL_ENV = "RTB_OBSERVATION_PROTOCOL"
CLEAN = "GUARD_BUNDLE_OBSERVATION_V1 CLEAN\n"
NO_DUE = "GUARD_BUNDLE_OBSERVATION_V1 NO_DUE_CHECKS\n"
DEFERRED = "GUARD_BUNDLE_OBSERVATION_V1 DEFERRED\n"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved
    return module


class _SubprocessShim:
    """Stand-in for the `subprocess` module inside the loaded consumer.

    The consumer catches `subprocess.TimeoutExpired` and calls `.run`; rebinding
    the module's global name keeps the real, process-wide subprocess module
    intact for every other test.
    """

    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, run):
        self.run = run


class Harness(unittest.TestCase):
    """Shared fixture: temp hermes root, temp RTB state, doubled I/O."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="guard-obs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._env_saved = dict(os.environ)
        self.addCleanup(self._restore_env)

        for key in ("RTB_STATE", "RTB_OBSERVATION_PROTOCOL", "RTB_SCRIPT", "RTB_KEY",
                    "RTB_TITLE", "RTB_BOARD", "RTB_ECHO_STDOUT", "RTB_TIMEOUT",
                    "RTB_QUIET_WINDOW_SEC", "GUARD_TICK", "GUARD_BUNDLE_ROOT"):
            os.environ.pop(key, None)
        os.environ["RTB_STATE"] = str(self.tmp / "report-to-board.json")

        self.runner = load_module(RUNNER_PATH, "guard_runner_under_test")
        self.rtb = load_module(CONSUMER_PATH, "rtb_under_test")

        # Temporary hermes root: never the live profile.
        self.hermes_root = self.tmp / "hermes"
        for sub in ("cron/state", "scripts", "profiles/jarvis/scripts"):
            (self.hermes_root / sub).mkdir(parents=True, exist_ok=True)
        self.runner.HERMES_HOME = self.hermes_root
        self.runner.SCRIPTS_DIR = self.hermes_root / "scripts"
        self.runner.CRON_DIR = self.hermes_root / "cron"
        self.runner.STATE_FILE = (self.runner.CRON_DIR / "state" /
                                  "guard_bundle_last_run.json")

        # Small fixture manifest: A is short-interval, B is long-interval.
        self.runner.BUNDLES = {"15m": ["A", "B"]}
        self.runner.CHECKS = {
            "A": {"script": "check_a.sh", "interval": 60},
            "B": {"script": "check_b.py", "interval": 21600},
        }
        self.runner.BUDGETS = {"5m": 175, "15m": 240, "hourly": 420, "daily": 450}
        self.runner.DEFAULT_TIMEOUT = 600

        self.rtb.STATE = Path(os.environ["RTB_STATE"])
        self.rtb.RTB_QUIET_WINDOW_SEC = 0
        self.rtb.RTB_TIMEOUT = 600

        self.board_calls: list[list[str]] = []
        self.card_status: dict[str, str | None] = {}
        self.rtb.hermes = self._hermes_double()
        self.created_id = "t_deadbeef"

        self.producer_calls: list[list[str]] = []
        self.producer_stdout = ""
        self.producer_stderr = ""
        self.producer_rc = 0
        # Rebind the consumer module's OWN `subprocess` global to a shim.
        # Patching subprocess.run itself would leak into every other test in
        # this process that shells out (the E2E chain test caught exactly that).
        self.rtb.subprocess = _SubprocessShim(self._producer_double)

        self.check_calls: list[str] = []
        self.check_results: dict = {}
        self.runner.run_check = self._check_double

    # -- env -----------------------------------------------------------------
    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env_saved)

    def set_env(self, **kw):
        for key, value in kw.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)

    def opt_in(self, tick="15m", key="guard-bundle-15m", script=None,
               board="jarvis-os"):
        """Explicit opt-in exactly as the 15m shim performs it."""
        self.set_env(
            RTB_OBSERVATION_PROTOCOL="guard-bundle-v1",
            GUARD_TICK=tick,
            RTB_KEY=key,
            RTB_BOARD=board,
            RTB_SCRIPT=script or str(self.hermes_root / "scripts" /
                                     "guard_bundle_run.sh"),
        )

    def job_env(self, key="guard-bundle-15m", board="jarvis-os"):
        self.set_env(RTB_SCRIPT=str(self.hermes_root / "scripts" / "guard_bundle_run.sh"),
                     RTB_KEY=key, RTB_BOARD=board, RTB_TITLE="Guard bundle (15m)")

    # -- doubles -------------------------------------------------------------
    def _hermes_double(self):
        def fake(*a, timeout=90):
            args = [str(x) for x in a]
            self.board_calls.append(args)
            if args and args[0] == "kanban":
                if "show" in args:
                    cid = args[-1]
                    if cid not in self.card_status:
                        return 1, f"error: no such task {cid}"
                    return 0, f"task {cid}\n  board: jarvis-os\n  status: {self.card_status[cid]}\n"
                if "create" in args:
                    return 0, f"created {self.created_id}"
                return 0, "ok"
            return 0, ""
        return fake

    def verbs(self):
        return [next((v for v in ("show", "create", "comment", "complete", "archive")
                      if v in call), "?") for call in self.board_calls]

    def _producer_double(self, cmd, **kw):
        self.producer_calls.append([str(x) for x in cmd])

        class _Result:
            stdout = self.producer_stdout
            stderr = self.producer_stderr
            returncode = self.producer_rc
        return _Result()

    def _check_double(self, name, spec, timeout):
        self.check_calls.append(name)
        result = self.check_results.get(name, (0, "", True))
        return result(name) if callable(result) else result

    # -- runners -------------------------------------------------------------
    def run_runner(self, bundle="15m"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            argv = sys.argv
            sys.argv = ["cron_guard_bundle_runner.py", bundle]
            try:
                rc = self.runner.main()
            finally:
                sys.argv = argv
                # A real run is one process, so the single-instance lock is
                # intentionally held for the process lifetime. In-process
                # repeated calls must drop it explicitly or the second call
                # would (correctly) see a concurrent instance.
                self.release_runner_locks()
        return rc, out.getvalue(), err.getvalue()

    def release_runner_locks(self):
        while self.runner._held_locks:
            fd = self.runner._held_locks.pop()
            try:
                os.close(fd)
            except OSError:
                pass

    def run_consumer(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.rtb.main()
        return rc, out.getvalue(), err.getvalue()

    # -- state ---------------------------------------------------------------
    def write_run_state(self, payload):
        self.runner.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.runner.STATE_FILE.write_text(json.dumps(payload))

    def read_run_state(self):
        if not self.runner.STATE_FILE.exists():
            return {}
        return json.loads(self.runner.STATE_FILE.read_text())

    def sidecar_path(self, bundle="15m"):
        return self.runner._observation_path(bundle)

    def write_sidecar(self, payload, bundle="15m"):
        path = self.sidecar_path(bundle)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path

    def read_sidecar(self, bundle="15m"):
        return json.loads(self.sidecar_path(bundle).read_text())

    def seed_incident(self, card_id="t_11111111", board="jarvis-os", digest="abc"):
        self.rtb.STATE.parent.mkdir(parents=True, exist_ok=True)
        self.rtb.STATE.write_text(json.dumps({
            "guard-bundle-15m": {"card_id": card_id, "board": board,
                                 "at": "2026-09-22T00:00:00Z", "digest": digest}}))
        self.card_status[card_id] = "blocked"
        return card_id

    def read_rtb_state(self):
        if not self.rtb.STATE.exists():
            return {}
        return json.loads(self.rtb.STATE.read_text())

    def now(self):
        import time as _time
        return int(_time.time())


# ---------------------------------------------------------------------------
# Producer (runner)
# ---------------------------------------------------------------------------
class ProducerObservationTests(Harness):

    def test_no_due_checks_emits_no_due_record_not_silence(self):
        """r0x: an empty tick is NOT a quiet tick — it says so explicitly."""
        self.opt_in()
        now = self.now()
        self.write_run_state({"A": now - 5, "B": now - 5})  # nothing due
        rc, out, err = self.run_runner()
        self.assertEqual(0, rc)
        self.assertEqual(NO_DUE, out)
        self.assertEqual([], self.check_calls)
        # timestamps untouched
        self.assertEqual({"A": now - 5, "B": now - 5}, self.read_run_state())
        # sidecar bootstrapped with every member pending
        self.assertEqual(["A", "B"], self.read_sidecar()["pending_recheck"])

    def test_all_due_pass_emits_exactly_one_clean_and_clears_pending(self):
        self.opt_in()
        self.write_run_state({})
        self.check_results = {"A": (0, "", True), "B": (0, "", True)}
        rc, out, err = self.run_runner()
        self.assertEqual(0, rc)
        self.assertEqual(CLEAN, out)
        self.assertEqual(["A", "B"], self.check_calls)
        self.assertEqual([], self.read_sidecar()["pending_recheck"])
        self.assertIsNone(self.read_sidecar()["in_flight"])

    def test_due_failure_is_a_report_never_a_record(self):
        self.opt_in()
        self.write_run_state({})
        self.check_results = {"A": (1, "[A] boom", True), "B": (0, "", True)}
        rc, out, err = self.run_runner()
        self.assertEqual(1, rc)
        self.assertIn("failed check(s)", out)
        self.assertIn("[A] boom", out)
        self.assertNotIn("GUARD_BUNDLE_OBSERVATION_V1", out)
        # A failed -> still owed
        self.assertEqual(["A"], self.read_sidecar()["pending_recheck"])

    def test_preflight_refusal_defers_without_advancing_the_window(self):
        """r09: a check we refused to launch is neither pass nor fail."""
        self.opt_in()
        now = self.now()
        self.write_run_state({"A": now - 120})
        self.runner.CHECKS = {
            "A": {"script": "check_a.sh", "interval": 60, "min_timeout": 360},
            "B": {"script": "check_b.py", "interval": 21600},
        }
        self.runner.BUDGETS = {"15m": 240}
        rc, out, err = self.run_runner()
        self.assertEqual(0, rc)
        self.assertEqual(DEFERRED, out)
        # A was refused (never launched); B was due and ran.
        self.assertEqual(["B"], self.check_calls)
        state = self.read_run_state()
        self.assertEqual(now - 120, state["A"])  # refused: window NOT advanced
        self.assertGreaterEqual(state["B"], now)
        self.assertIn("A", self.read_sidecar()["pending_recheck"])
        # window NOT advanced: A is still due next tick
        self.assertEqual(now - 120, self.read_run_state()["A"])
        self.assertEqual(["A"], self.read_sidecar()["pending_recheck"])

    def test_partial_pass_then_refusal_clears_only_what_passed(self):
        """r10: partial run — A cleared, B still owed, no CLEAN."""
        self.opt_in()
        now = self.now()
        self.write_run_state({"A": now - 120})
        self.runner.CHECKS = {
            "A": {"script": "check_a.sh", "interval": 60},
            "B": {"script": "check_b.py", "interval": 30, "min_timeout": 999},
        }
        self.runner.BUDGETS = {"15m": 60}
        self.check_results = {"A": (0, "", True)}
        rc, out, err = self.run_runner()
        self.assertEqual(0, rc)
        self.assertEqual(DEFERRED, out)
        # A ran and passed; B was refused (never launched)
        self.assertEqual(["A"], self.check_calls)
        self.assertEqual(["B"], self.read_sidecar()["pending_recheck"])

    def test_budget_expiry_defers_and_never_clears_debt(self):
        self.opt_in()
        self.write_run_state({})
        self.runner.BUDGETS = {"15m": 0}  # exhausted immediately
        self.check_results = {"A": (0, "", True), "B": (0, "", True)}
        rc, out, err = self.run_runner()
        self.assertEqual(0, rc)
        self.assertEqual(DEFERRED, out)
        self.assertEqual([], self.check_calls)
        self.assertEqual(["A", "B"], self.read_sidecar()["pending_recheck"])

    def test_debt_is_persisted_before_the_check_is_launched(self):
        """r13: a kill mid-check must leave the identity owed, not forgotten."""
        self.opt_in()
        self.write_run_state({})

        def exploding(name, spec, timeout):
            self.check_calls.append(name)
            # The prelaunch write already happened; simulate SIGKILL mid-check.
            raise SystemExit(137)

        self.runner.run_check = exploding
        with self.assertRaises(SystemExit):
            self.run_runner()
        sidecar = self.read_sidecar()
        self.assertEqual("A", sidecar["in_flight"])
        self.assertIn("A", sidecar["pending_recheck"])

    def test_stale_in_flight_prevents_a_later_clean(self):
        self.opt_in()
        now = self.now()
        # A "crashed" mid-check last tick; nothing is due this tick.
        self.write_sidecar({"version": 1, "bundle": "15m", "members": ["A", "B"],
                            "pending_recheck": [], "in_flight": "A"})
        self.write_run_state({"A": now - 1, "B": now - 1})
        rc, out, err = self.run_runner()
        self.assertEqual(0, rc)
        self.assertEqual(NO_DUE, out)
        self.assertEqual(["A"], self.read_sidecar()["pending_recheck"])

    def test_membership_change_invalidates_positive_evidence(self):
        """r14: a newly added check cannot inherit a stale CLEAN."""
        self.opt_in()
        self.write_sidecar({"version": 1, "bundle": "15m", "members": ["A"],
                            "pending_recheck": [], "in_flight": None})
        self.write_run_state({})
        self.check_results = {"A": (0, "", True), "B": (0, "", True)}
        rc, out, err = self.run_runner()
        self.assertEqual(0, rc)
        self.assertEqual(CLEAN, out)  # both ran this tick
        self.assertEqual(["A", "B"], sorted(self.read_sidecar()["members"]))
        # the NEW member is pending until it has actually run
        self.write_run_state({"A": self.now(), "B": self.now()})
        self.check_results = {}
        rc2, out2, _ = self.run_runner()
        self.assertEqual(NO_DUE, out2)
        self.assertEqual(["A", "B"], sorted(self.read_sidecar()["members"]))

    def test_new_manifest_member_is_pending_until_it_runs(self):
        """r15: a check added to the manifest cannot inherit a stale CLEAN."""
        self.opt_in()
        now = self.now()
        self.write_sidecar({"version": 1, "bundle": "15m", "members": ["A", "B"],
                            "pending_recheck": [], "in_flight": None})
        self.runner.BUNDLES = {"15m": ["A", "B", "C"]}
        self.runner.CHECKS["C"] = {"script": "check_c.sh", "interval": 21600}
        self.write_run_state({"B": now - 5, "C": now - 5})  # only A is due
        self.check_results = {"A": (0, "", True)}
        rc, out, err = self.run_runner()
        self.assertEqual(DEFERRED, out)
        self.assertEqual(["A"], self.check_calls)
        self.assertEqual(["A", "B", "C"], sorted(self.read_sidecar()["members"]))
        self.assertEqual(["B", "C"], self.read_sidecar()["pending_recheck"])

    def test_malformed_sidecar_is_a_visible_failure(self):
        """r11: never recover from unreadable evidence."""
        self.opt_in()
        self.write_run_state({})
        self.sidecar_path().write_text("{ this is not json")
        rc, out, err = self.run_runner()
        self.assertEqual(1, rc)
        self.assertIn("observation state failure", out)
        self.assertNotIn("GUARD_BUNDLE_OBSERVATION_V1", out)
        self.assertEqual([], self.check_calls)

    def test_sidecar_with_wrong_shape_is_a_visible_failure(self):
        self.opt_in()
        self.write_run_state({})
        self.write_sidecar({"version": 1, "bundle": "15m", "members": ["B"]})
        rc, out, err = self.run_runner()
        self.assertEqual(1, rc)
        self.assertIn("observation state", out)

    def test_unreadable_timestamp_state_is_loud_when_opted_in(self):
        self.opt_in()
        self.write_run_state({"A": "not-a-timestamp"})
        rc, out, err = self.run_runner()
        self.assertEqual(1, rc)
        self.assertIn("timestamp state validation failure", out)
        self.assertNotIn("GUARD_BUNDLE_OBSERVATION_V1", out)

    def test_unbound_protocol_env_is_a_configuration_error(self):
        """r18: an opt-in that is not bound to its job cannot emit a record."""
        self.opt_in(tick="5m", key="guard-bundle-5m")  # wrong job for the 15m run
        self.write_run_state({})
        rc, out, err = self.run_runner()
        self.assertEqual(2, rc)
        self.assertIn("configuration error", out)
        self.assertNotIn("GUARD_BUNDLE_OBSERVATION_V1", out)
        self.assertEqual([], self.check_calls)

    def test_protocol_env_on_a_bundle_without_a_binding_is_a_config_error(self):
        self.opt_in(tick="5m", key="guard-bundle-5m")
        self.write_run_state({})
        self.runner.BUNDLES = {"5m": ["A"]}
        rc, out, err = self.run_runner(bundle="5m")
        self.assertEqual(2, rc)
        self.assertIn("no guard-bundle-v1 binding", out)

    def test_single_instance_guard_reports_reason_not_a_record(self):
        """A concurrent tick exits silently and makes no observation."""
        import fcntl
        self.runner.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        path = self.runner.STATE_FILE.parent / "guard_bundle_15m.lock"
        held = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, held)
        self.opt_in()
        rc, out, err = self.run_runner()
        self.assertEqual(0, rc)
        self.assertEqual("", out)  # a skipped tick is not an observation
        self.assertEqual([], self.check_calls)


# ---------------------------------------------------------------------------
# Consumer (report-to-board)
# ---------------------------------------------------------------------------
class ConsumerObservationTests(Harness):

    def test_clean_is_the_only_record_that_closes_the_incident(self):
        self.opt_in()
        self.job_env()
        card = self.seed_incident()
        self.producer_stdout, self.producer_rc = CLEAN, 0
        rc, out, err = self.run_consumer()
        self.assertEqual(0, rc)
        self.assertEqual(["show", "complete", "archive"], self.verbs())
        self.assertEqual({}, self.read_rtb_state())
        self.assertNotIn("GUARD_BUNDLE_OBSERVATION_V1", out)

    def test_no_due_checks_leaves_the_incident_and_the_mapping_alone(self):
        """r01: the false-recovery this whole card exists for."""
        self.opt_in()
        self.job_env()
        card = self.seed_incident()
        before = self.read_rtb_state()
        self.producer_stdout, self.producer_rc = NO_DUE, 0
        rc, out, err = self.run_consumer()
        self.assertEqual(0, rc)
        self.assertEqual("", out)
        self.assertEqual([], self.board_calls)
        self.assertEqual(before, self.read_rtb_state())
        self.assertIn("NO_DUE_CHECKS", err)

    def test_deferred_leaves_the_incident_alone(self):
        self.opt_in()
        self.job_env()
        self.seed_incident()
        before = self.read_rtb_state()
        self.producer_stdout, self.producer_rc = DEFERRED, 0
        rc, out, err = self.run_consumer()
        self.assertEqual(0, rc)
        self.assertEqual("", out)
        self.assertEqual([], self.board_calls)
        self.assertEqual(before, self.read_rtb_state())

    def test_protocol_clean_wording_is_qualified(self):
        """r23: only the qualification changes."""
        self.opt_in()
        self.job_env()
        self.seed_incident()
        self.producer_stdout, self.producer_rc = CLEAN, 0
        rc, out, err = self.run_consumer()
        complete = next(c for c in self.board_calls if "complete" in c)
        summary = complete[complete.index("--summary") + 1]
        self.assertIn("completed due checks passed and pending rechecks cleared", summary)
        self.assertNotIn("reported nothing this run", summary)

    def test_non_opted_job_wording_is_unchanged(self):
        self.job_env()
        self.seed_incident()
        self.producer_stdout, self.producer_rc = "", 0
        rc, out, err = self.run_consumer()
        complete = next(c for c in self.board_calls if "complete" in c)
        summary = complete[complete.index("--summary") + 1]
        self.assertIn("reported nothing this run", summary)
        self.assertNotIn("completed due checks passed", summary)

    def test_owner_owned_card_gets_a_qualified_comment_not_a_close(self):
        self.opt_in()
        self.job_env()
        card = self.seed_incident()
        self.card_status[card] = "running"
        self.producer_stdout, self.producer_rc = CLEAN, 0
        rc, out, err = self.run_consumer()
        self.assertEqual(0, rc)
        self.assertEqual(["show", "comment"], self.verbs())
        comment = next(c for c in self.board_calls if "comment" in c)
        body = comment[-1]
        self.assertIn("RESOLVED", body)
        self.assertIn("Qualified observation (guard-bundle-v1)", body)
        self.assertIn(card, self.read_rtb_state()["guard-bundle-15m"]["card_id"])

    def test_unknown_card_status_is_loud_and_keeps_the_mapping(self):
        """r17: a clear we cannot apply is not a clear."""
        self.opt_in()
        self.job_env()
        self.seed_incident()
        del self.card_status["t_11111111"]  # every show now fails
        self.producer_stdout, self.producer_rc = CLEAN, 0
        rc, out, err = self.run_consumer()
        self.assertEqual(1, rc)
        self.assertEqual(["show"], self.verbs())
        self.assertIn("t_11111111", self.read_rtb_state()["guard-bundle-15m"]["card_id"])

    def test_archive_failure_is_loud_and_keeps_the_mapping(self):
        self.opt_in()
        self.job_env()
        card = self.seed_incident()
        self.card_status[card] = "done"

        def hermes(*a, timeout=90):
            args = [str(x) for x in a]
            self.board_calls.append(args)
            if "show" in args:
                return 0, f"task {card}\n  status: done\n"
            if "archive" in args:
                return 1, "error: archive rejected"
            return 0, "ok"
        self.rtb.hermes = hermes
        self.producer_stdout, self.producer_rc = CLEAN, 0
        rc, out, err = self.run_consumer()
        self.assertEqual(1, rc)
        self.assertIn("archive failed", err)
        self.assertIn(card, self.read_rtb_state()["guard-bundle-15m"]["card_id"])

    def test_nonzero_exit_dominates_even_with_no_stdout(self):
        """r05/r06: applies to legacy jobs too — "" is not evidence."""
        self.job_env()  # NOT opted in
        self.seed_incident()
        self.producer_stdout, self.producer_rc, self.producer_stderr = "", 1, "boom: nope"
        rc, out, err = self.run_consumer()
        verb_list = self.verbs()
        self.assertIn("comment", verb_list)          # reported, not closed
        self.assertNotIn("complete", verb_list)
        self.assertNotIn("archive", verb_list)
        body = next(c for c in self.board_calls if "comment" in c)[-1]
        self.assertIn("PRODUCER EXIT 1", body)
        self.assertIn("boom: nope", body)
        self.assertIn("guard-bundle-15m", self.read_rtb_state())

    def test_nonzero_exit_beats_a_clean_marker(self):
        """r04/r08: CLEAN + exit 1 must not clear anything."""
        self.opt_in()
        self.job_env()
        self.seed_incident()
        self.producer_stdout, self.producer_rc = CLEAN, 1
        rc, out, err = self.run_consumer()
        verbs = self.verbs()
        self.assertIn("comment", verbs)
        self.assertNotIn("complete", verbs)
        self.assertNotIn("archive", verbs)
        body = next(c for c in self.board_calls if "comment" in c)[-1]
        self.assertIn("PRODUCER EXIT 1", body)
        self.assertNotIn("GUARD_BUNDLE_OBSERVATION_V1 CLEAN", body)

    def test_missing_record_when_opted_in_is_a_contract_error(self):
        self.opt_in()
        self.job_env()
        self.seed_incident()
        self.producer_stdout, self.producer_rc = "", 0
        rc, out, err = self.run_consumer()
        self.assertEqual(1, rc)
        self.assertEqual(["show", "comment"], self.verbs())
        body = next(c for c in self.board_calls if "comment" in c)[-1]
        self.assertIn("OBSERVATION CONTRACT ERROR", body)

    def test_marker_buried_in_a_report_is_just_a_report(self):
        self.opt_in()
        self.job_env()
        self.seed_incident()
        self.producer_stdout = f"some real failure text\n{CLEAN}"
        rc, out, err = self.run_consumer()
        self.assertEqual(1, rc)
        self.assertNotIn("complete", self.verbs())
        body = next(c for c in self.board_calls if "comment" in c)[-1]
        self.assertIn("OBSERVATION CONTRACT ERROR", body)

    def test_control_records_are_never_echoed(self):
        """r22: RTB_ECHO_STDOUT echoes human reports only."""
        self.opt_in()
        self.job_env()
        self.set_env(RTB_ECHO_STDOUT="1")
        self.seed_incident()
        self.producer_stdout, self.producer_rc = CLEAN, 0
        rc, out, err = self.run_consumer()
        self.assertEqual("", out)
        self.producer_stdout, self.producer_rc = NO_DUE, 0
        rc, out, err = self.run_consumer()
        self.assertEqual("", out)

    def test_opted_in_clean_still_needs_due_work_from_the_producer(self):
        """r02/r03: one complete+archive, no board call for a no-due tick."""
        self.opt_in()
        self.job_env()
        self.seed_incident()
        self.producer_stdout, self.producer_rc = NO_DUE, 0
        self.run_consumer()
        first = list(self.board_calls)
        self.assertEqual([], first)
        self.producer_stdout, self.producer_rc = CLEAN, 0
        self.run_consumer()
        self.assertEqual(3, len(self.board_calls) - len(first))

    def test_same_key_lock_spans_producer_and_application(self):
        """r13/r21: a second same-job run cannot interleave a stale verdict."""
        self.opt_in()
        self.job_env()
        self.seed_incident()
        held = self.rtb._acquire_observation_lock("jarvis-os", "guard-bundle-15m")
        self.assertIsInstance(held, int)
        self.addCleanup(self.rtb._release_observation_lock, held)
        rc, out, err = self.run_consumer()
        self.assertEqual(0, rc)
        self.assertEqual([], self.producer_calls)
        self.assertEqual([], self.board_calls)

    def test_lock_error_is_loud(self):
        self.opt_in()
        self.job_env()
        self.seed_incident()

        def boom(board, key):
            raise OSError("permission denied")
        self.rtb._acquire_observation_lock = boom
        rc, out, err = self.run_consumer()
        self.assertEqual(1, rc)
        self.assertIn("observation lock setup failed", err)
        self.assertEqual([], self.producer_calls)

    def test_repeat_failure_does_not_duplicate_the_incident(self):
        """r16: only CLEAN produces durable closure claims."""
        self.opt_in()
        self.job_env()
        self.seed_incident()
        self.card_status[self.created_id] = "blocked"
        self.producer_stdout, self.producer_rc = "[A] boom", 1
        self.run_consumer()
        self.run_consumer()
        verbs = self.verbs()
        self.assertEqual(0, verbs.count("create"))   # one card per incident
        self.assertEqual(1, verbs.count("comment"))  # refresh, not a duplicate
        self.assertNotIn("complete", verbs)
        self.assertNotIn("archive", verbs)

    def test_state_and_cards_never_contain_a_control_record(self):
        """r16: records are transport, not content."""
        self.opt_in()
        self.job_env()
        self.seed_incident()
        self.producer_stdout, self.producer_rc = CLEAN, 0
        self.run_consumer()
        blob = json.dumps(self.read_rtb_state())
        for call in self.board_calls:
            blob += " ".join(call)
        self.assertNotIn("GUARD_BUNDLE_OBSERVATION_V1", blob)

    def test_toggle_restores_pre_fix_behavior_when_not_opted_in(self):
        """Controlled substitution: with the opt-in removed, behaviour is the
        pre-fix behaviour — proving the change is genuinely scoped."""
        self.job_env()  # no RTB_OBSERVATION_PROTOCOL
        self.seed_incident()
        self.producer_stdout, self.producer_rc = "", 0
        rc, out, err = self.run_consumer()
        self.assertEqual(0, rc)
        self.assertEqual(["show", "complete", "archive"], self.verbs())
        self.assertEqual({}, self.read_rtb_state())


# ---------------------------------------------------------------------------
# Real entrypoint chain (shim -> wrapper -> runner -> consumer)
# ---------------------------------------------------------------------------
class RealEntrypointChainTests(unittest.TestCase):
    """Executes the real scripts, pointed at a throwaway hermes root.

    Uses the REAL BUNDLES manifest, so it also proves the copies are wired as
    the named addresses and that the chain stays silent with empty stdout.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="guard-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "hermes"
        for sub in ("scripts", "profiles/jarvis/scripts",
                    "profiles/jarvis/cron/state", "bin"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        shutil.copy2(CONSUMER_PATH, self.root / "scripts" / "report-to-board.py")
        shutil.copy2(WRAPPER, self.root / "scripts" / "guard_bundle_run.sh")
        shutil.copy2(RUNNER_PATH, self.root / "profiles/jarvis/scripts" /
                     "cron_guard_bundle_runner.py")
        shutil.copy2(SHIM_15M, self.root / "profiles/jarvis/scripts" /
                     "guard_bundle_tick_15m.sh")
        # Copies must be byte-identical to the reviewed sources.
        for src, dst in ((CONSUMER_PATH, self.root / "scripts" / "report-to-board.py"),
                         (WRAPPER, self.root / "scripts" / "guard_bundle_run.sh"),
                         (RUNNER_PATH, self.root / "profiles/jarvis/scripts" /
                          "cron_guard_bundle_runner.py"),
                         (SHIM_15M, self.root / "profiles/jarvis/scripts" /
                          "guard_bundle_tick_15m.sh")):
            self.assertEqual(src.read_bytes(), dst.read_bytes())
        self.runner_mod = load_module(RUNNER_PATH, "guard_runner_e2e")
        self.checks = self.runner_mod.CHECKS
        for name, spec in self.checks.items():
            script = spec["script"]
            # SCRIPTS_DIR is <HERMES_HOME>/scripts = profiles/jarvis/scripts.
            # Hermetic stubs (never the live guards) so the chain is exercised
            # end-to-end without touching fleet state.
            path = self.root / "profiles/jarvis/scripts" / script
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.suffix in (".sh", ".bash"):
                    path.write_text("#!/usr/bin/env bash\nexit 0\n")
                else:
                    path.write_text("import sys\nsys.exit(0)\n")
                path.chmod(0o755)
        self.fake_bin = self.root / "bin"
        (self.fake_bin / "hermes").write_text(
            "#!/usr/bin/env bash\n"
            'echo "$@" >> "$HERMES_FAKE_LOG"\n'
            'case "$*" in\n'
            '  *" show "*) echo "  status: blocked" ;;\n'
            '  *" create "*) echo "created t_deadbeef" ;;\n'
            "esac\n"
            "exit 0\n")
        (self.fake_bin / "hermes").chmod(0o755)
        self.log = self.tmp / "hermes-calls.log"
        self.log.write_text("")
        self.rtb_state = self.tmp / "report-to-board.json"
        self.rtb_state.write_text(json.dumps({
            "guard-bundle-15m": {"card_id": "t_12345678", "board": "jarvis-os",
                                 "at": "2026-09-22T00:00:00Z", "digest": "x"}}))

    # t_d14445d2 F4 (isolation): scope isolation for the child env.
    #
    # An inherited kanban-worker scope is dangerous here: the shim exports
    # HERMES_HOME and execs wrapper -> runner -> consumer -> `hermes`, so a
    # leaked HERMES_KANBAN_TASK/_BOARD/_DB would let report-to-board.py act on a
    # REAL worker card under test. The dict built below is a fresh allow-list,
    # never `dict(os.environ)`; the strip below is therefore belt-and-braces on
    # purpose, so a later refactor that starts inheriting os.environ fails
    # closed (AssertionError) instead of silently re-opening that hole.
    CHILD_ENV_DENY_PREFIXES = ("HERMES_KANBAN_", "HERMES_TENANT")

    @classmethod
    def strip_inherited_kanban_scope(cls, env):
        """Drop any inherited worker scope from `env` and refuse to return a
        mapping that still carries one."""
        for key in [k for k in env if k.startswith(cls.CHILD_ENV_DENY_PREFIXES)]:
            env.pop(key, None)
        leaked = sorted(k for k in env if k.startswith(cls.CHILD_ENV_DENY_PREFIXES))
        if leaked:
            raise AssertionError(
                "kanban worker scope leaked into the child env: " + ", ".join(leaked))
        return env

    def _env(self, **extra):
        env = {
            "PATH": f"{self.fake_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "GUARD_BUNDLE_ROOT": str(self.root),
            "HERMES_FAKE_LOG": str(self.log),
            "RTB_STATE": str(self.rtb_state),
        }
        env.update({k: str(v) for k, v in extra.items()})
        return self.strip_inherited_kanban_scope(env)

    def _run_shim(self, env=None):
        return subprocess.run(
            ["bash", str(self.root / "profiles/jarvis/scripts/guard_bundle_tick_15m.sh")],
            capture_output=True, text=True, env=env or self._env(), cwd=str(self.root),
            timeout=300)

    def test_clean_chain_closes_the_incident_and_is_silent(self):
        proc = self._run_shim()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("", proc.stdout,
                         f"stderr={proc.stderr!r} log={self.log.read_text()!r}")
        calls = self.log.read_text()
        self.assertIn("show t_12345678", calls)
        self.assertIn("complete t_12345678", calls)
        self.assertIn("archive t_12345678", calls)
        self.assertEqual({}, json.loads(self.rtb_state.read_text()))
        sidecar = json.loads((self.root / "profiles/jarvis/cron/state" /
                              "guard_bundle_observations_15m.json").read_text())
        self.assertEqual([], sidecar["pending_recheck"])

    def test_no_due_chain_leaves_the_incident_open_and_silent(self):
        import time as _time
        members = sorted(self.runner_mod.BUNDLES["15m"])
        now = int(_time.time())
        (self.root / "profiles/jarvis/cron/state" /
         "guard_bundle_last_run.json").write_text(
            json.dumps({name: now for name in members}))
        before = self.rtb_state.read_text()
        proc = self._run_shim()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("", proc.stdout)
        self.assertEqual("", self.log.read_text())
        self.assertEqual(before, self.rtb_state.read_text())

    # -----------------------------------------------------------------------
    # F4 (isolation): no inherited kanban-worker scope reaches a child process
    # -----------------------------------------------------------------------
    # The parent pytest process may itself be a kanban worker (HERMES_KANBAN_TASK
    # etc. exported). These tests set that scope in the parent and then prove it
    # cannot reach the shim -> wrapper -> runner -> consumer -> `hermes` chain.
    LEAKY = {
        "HERMES_KANBAN_TASK": "t_deadbeef",
        "HERMES_KANBAN_BOARD": "jarvis-os",
        "HERMES_KANBAN_DB": "/tmp/live-kanban.db",
        "HERMES_KANBAN_WORKSPACE": "/tmp/live-workspace",
        "HERMES_TENANT": "jarvis-os",
    }

    def test_child_env_builder_strips_and_asserts(self):
        """The builder strips the scope, and refuses a mapping that still has it."""
        with mock.patch.dict(os.environ, self.LEAKY, clear=False):
            env = self._env()
        self.assertEqual([], sorted(k for k in env if k.startswith(
            ("HERMES_KANBAN_", "HERMES_TENANT"))), env)
        # Nailed-down behaviour: even a future inheriting builder is corrected,
        # and an un-correctable mapping raises instead of leaking.
        dirty = dict(os.environ)
        dirty["HERMES_KANBAN_TASK"] = "t_deadbeef"
        self.assertNotIn("HERMES_KANBAN_TASK",
                         self.strip_inherited_kanban_scope(dirty))
        # ...while the allow-listed channel the chain actually needs survives.
        self.assertEqual(str(self.rtb_state), env["RTB_STATE"])

    def test_real_chain_child_process_cannot_see_inherited_kanban_scope(self):
        """End-to-end: the REAL shim chain runs with a leaked parent scope and
        still hands `hermes` an environment without it."""
        envlog = self.tmp / "child-env.log"
        # Local probe double: same contract, but dumps the env it was handed.
        (self.fake_bin / "hermes").write_text(
            "#!/usr/bin/env bash\n"
            'env | sort > "$HERMES_ENV_LOG"\n'
            'echo "$@" >> "$HERMES_FAKE_LOG"\n'
            'case "$*" in\n'
            '  *" show "*) echo "  status: blocked" ;;\n'
            '  *" create "*) echo "created t_deadbeef" ;;\n'
            "esac\n"
            "exit 0\n")
        (self.fake_bin / "hermes").chmod(0o755)
        self.log.write_text("")
        with mock.patch.dict(os.environ, self.LEAKY, clear=False):
            proc = self._run_shim(self._env(HERMES_ENV_LOG=str(envlog)))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertTrue(envlog.exists(),
                        f"the chain never reached the hermes double: {proc.stderr!r}")
        child_env = envlog.read_text()
        leaked = [line for line in child_env.splitlines()
                  if line.startswith(("HERMES_KANBAN_", "HERMES_TENANT"))]
        self.assertEqual([], leaked, f"scope leaked into the real child chain: {leaked}")
        # Non-vacuous: the probe really did run inside the chain, and the
        # allow-listed vars it needs are intact.
        self.assertIn("show t_12345678", self.log.read_text())
        self.assertIn(f"GUARD_BUNDLE_ROOT={self.root}", child_env)


if __name__ == "__main__":
    unittest.main()