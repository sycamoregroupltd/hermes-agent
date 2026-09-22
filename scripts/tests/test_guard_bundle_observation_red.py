#!/usr/bin/env python3
"""Frozen-baseline RED / GREEN proof for the guard-bundle observation protocol.

WHY THIS FILE EXISTS (t_d14445d2)
---------------------------------
The candidate in this worktree is built on the LIVE executed copies, so the only
honest way to show the fix is real is to run the SAME scenario twice:

  RED   - against the frozen pre-fix bytes kept in
          scripts/tests/fixtures/guard_bundle_pre_fix/ (hashed in SHA256SUMS).
          The false recovery is REPRODUCED there.
  GREEN - against the candidate entrypoints in this worktree. The false
          recovery is GONE.

The RED bytes are committed because the live runner is untracked in git: the
fixture copy is the durable record of the baseline this splice was reconciled
against (os-reviewer re-verification section 9).

The GREEN assertions are additionally checked for SENSITIVITY: the same
assertions must FAIL against the frozen chain. A test that cannot fail proves
nothing, so `test_*_green_check_is_sensitive_to_the_fix` asserts exactly that.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "guard_bundle_pre_fix"
RUNNER = ROOT / "profiles" / "jarvis" / "scripts" / "cron_guard_bundle_runner.py"
CONSUMER = ROOT / "scripts" / "report-to-board.py"
FROZEN_RUNNER = FIXTURES / "cron_guard_bundle_runner.py"
FROZEN_CONSUMER = FIXTURES / "report-to-board.py"

# Hashes of the LIVE executed copies at reconciliation time. SHA256SUMS in the
# fixture dir carries the same values; the literal copy here makes an accidental
# fixture refresh impossible to miss.
EXPECTED_LIVE_HASHES = {
    "cron_guard_bundle_runner.py":
        "df97be3b9faf975bb00d56c1ac099eb0163af8f571048a38dffbd0bffe7d56a0",
    "guard_bundle_run.sh":
        "8a7375f2a54b28ac7c8fbb7a7e35cac8f8ad842aa89c8c0ace0522f7354292a9",
    "guard_bundle_tick_15m.sh":
        "0d1cbcc5c8761856ff5cbe8cfc06c23cfe7eebdffc23bf8278d60e6bdebd3db3",
    "report-to-board.py":
        "b9df8a6345eed9aa27bd37010d3025eeef9d01a9ed742409085dd4bd17719294",
}

VERBS = ("show", "create", "comment", "complete", "archive")


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # Never leave __pycache__ beside the frozen baseline fixtures.
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved
    return module


class _SubprocessShim:
    """Rebinds only the loaded consumer's own `subprocess` global."""

    TimeoutExpired = __import__("subprocess").TimeoutExpired

    def __init__(self, run):
        self.run = run


def verbs_of(calls):
    return [next((v for v in VERBS if v in call), "?") for call in calls]


def assert_no_false_recovery(case, result):
    """The invariant the whole card exists for: no closure without CLEAN."""
    verbs = verbs_of(result["cards"])
    case.assertNotIn("complete", verbs, f"closed a card: {result}")
    case.assertNotIn("archive", verbs, f"archived a card: {result}")
    case.assertIn("guard-bundle-15m", result["rtb_state"],
                  f"incident mapping was dropped: {result}")


class Chain:
    """One producer+consumer pair driven exactly like the live chain.

    The consumer normally shells out to the producer; here the producer's
    recorded stdout/rc is handed to the consumer's own subprocess double, so a
    unit under test can be frozen bytes OR the candidate without either of them
    knowing which.
    """

    def __init__(self, tmp: Path, runner_path: Path, consumer_path: Path,
                 opt_in: bool = False, tag: str = "chain"):
        self.tmp = tmp
        self.root = tmp / "hermes"
        (self.root / "scripts").mkdir(parents=True, exist_ok=True)
        (self.root / "cron" / "state").mkdir(parents=True, exist_ok=True)
        self.runner = load_module(runner_path, f"red_runner_{tag}")
        self.consumer = load_module(consumer_path, f"red_consumer_{tag}")
        self.runner.HERMES_HOME = self.root
        self.runner.SCRIPTS_DIR = self.root / "scripts"
        self.runner.CRON_DIR = self.root / "cron"
        self.runner.STATE_FILE = self.root / "cron" / "state" / "guard_bundle_last_run.json"
        self.runner.BUNDLES = {"15m": ["A", "B"]}
        self.runner.CHECKS = {"A": {"script": "check_a.sh", "interval": 60},
                              "B": {"script": "check_b.py", "interval": 21600}}
        self.runner.BUDGETS = {"15m": 240}
        self.runner.DEFAULT_TIMEOUT = 600
        self.check_results: dict = {}
        # The frozen producer returns (rc, out); the candidate adds `attempted`.
        self.triple = hasattr(self.runner, "load_observation_state")
        self.runner.run_check = self._check_double

        self.card_status = {"t_cafe0001": "blocked"}
        self.board_calls: list = []
        self.consumer.hermes = self._hermes_double
        self.consumer.STATE = tmp / "report-to-board.json"
        self.consumer.STATE.write_text(json.dumps({
            "guard-bundle-15m": {"card_id": "t_cafe0001", "board": "jarvis-os",
                                 "at": "2026-09-22T00:00:00Z", "digest": "seed"}}))
        self.producer_stdout, self.producer_stderr, self.producer_rc = "", "", 0
        self.consumer.subprocess = _SubprocessShim(self._producer_double)

        self._env_saved = dict(os.environ)
        for key in list(os.environ):
            if key.startswith(("RTB_", "GUARD_")):
                os.environ.pop(key, None)
        os.environ.update(
            RTB_SCRIPT=str(self.root / "scripts" / "guard_bundle_run.sh"),
            RTB_KEY="guard-bundle-15m", RTB_BOARD="jarvis-os",
            RTB_TITLE="Guard bundle (15m)",
        )
        if opt_in:
            os.environ.update(RTB_OBSERVATION_PROTOCOL="guard-bundle-v1",
                              GUARD_TICK="15m")

    # -- doubles -------------------------------------------------------------
    def _check_double(self, name, spec, timeout):
        rc, out, attempted = self.check_results.get(name, (0, "", True))
        return (rc, out, attempted) if self.triple else (rc, out)

    def _producer_double(self, cmd, **kwargs):
        class _Result:
            stdout = self.producer_stdout
            stderr = self.producer_stderr
            returncode = self.producer_rc
        return _Result()

    def _hermes_double(self, *a, timeout=90):
        args = [str(x) for x in a]
        self.board_calls.append(args)
        if args and args[0] == "kanban":
            if "show" in args:
                card = args[-1]
                if card not in self.card_status:
                    return 1, f"error: no such task {card}"
                return 0, f"task {card}\n  board: jarvis-os\n  status: {self.card_status[card]}\n"
            if "create" in args:
                return 0, "t_deadbeef created"
            return 0, "ok"
        return 0, ""

    # -- driver --------------------------------------------------------------
    def restore(self):
        os.environ.clear()
        os.environ.update(self._env_saved)

    def _drop_locks(self):
        locks = getattr(self.runner, "_held_locks", None)
        while locks:
            fd = locks.pop()
            try:
                os.close(fd)
            except OSError:
                pass

    def nothing_due(self):
        now = int(time.time())
        self.runner.STATE_FILE.write_text(
            json.dumps({"A": now, "B": now}))

    def tick(self):
        """Run the producer (like the shim does), then the consumer."""
        out, err = io.StringIO(), io.StringIO()
        argv = sys.argv
        sys.argv = ["cron_guard_bundle_runner.py", "15m"]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = self.runner.main()
        finally:
            sys.argv = argv
            self._drop_locks()
        self.producer_stdout, self.producer_rc = out.getvalue(), rc
        result = self.consume()
        result["runner_rc"] = rc
        result["runner_out"] = out.getvalue()
        return result

    def consume(self):
        self.board_calls = []
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.consumer.main()
        return {"consumer_rc": rc, "consumer_out": out.getvalue(),
                "consumer_err": err.getvalue(), "cards": list(self.board_calls),
                "rtb_state": json.loads(self.consumer.STATE.read_text())}


class ChainTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="guard-red-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.chains: list = []

    def chain(self, runner_path, consumer_path, opt_in=False, tag="c"):
        ch = Chain(self.tmp / f"r{len(self.chains)}", runner_path, consumer_path,
                   opt_in=opt_in, tag=f"{tag}{len(self.chains)}")
        self.chains.append(ch)
        self.addCleanup(ch.restore)
        return ch

    def frozen(self, opt_in=False):
        return self.chain(FROZEN_RUNNER, FROZEN_CONSUMER, opt_in, tag="frozen")

    def candidate(self, opt_in=False):
        return self.chain(RUNNER, CONSUMER, opt_in, tag="cand")


class FrozenBaselineTests(unittest.TestCase):
    """The RED baseline must be exactly the live bytes it claims to be."""

    def test_fixture_matches_the_recorded_live_hashes(self):
        import hashlib
        sums = {}
        for line in (FIXTURES / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split()
            sums[name] = digest
        self.assertEqual(EXPECTED_LIVE_HASHES, sums)
        for name, expected in EXPECTED_LIVE_HASHES.items():
            actual = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
            self.assertEqual(expected, actual, name)

    def test_candidate_files_are_present_and_differ_from_the_baseline(self):
        import hashlib
        for src, frozen in ((RUNNER, FROZEN_RUNNER), (CONSUMER, FROZEN_CONSUMER)):
            self.assertTrue(src.is_file(), src)
            self.assertNotEqual(
                hashlib.sha256(src.read_bytes()).hexdigest(),
                hashlib.sha256(frozen.read_bytes()).hexdigest(), src)


class FalseRecoveryTests(ChainTestCase):
    """R01 (no due work) and R05/R06 (exit != 0 with empty stdout)."""

    def test_r01_frozen_chain_closes_the_incident_on_a_no_due_tick(self):
        """RED: the pre-fix bytes really do claim recovery from silence."""
        ch = self.frozen()
        ch.nothing_due()
        result = ch.tick()
        self.assertEqual(0, result["runner_rc"])
        self.assertEqual("", result["runner_out"])       # no verdict at all
        verbs = verbs_of(result["cards"])
        self.assertIn("complete", verbs)                 # ... yet the card closed
        self.assertIn("archive", verbs)
        self.assertNotIn("guard-bundle-15m", result["rtb_state"])

    def test_r01_candidate_chain_keeps_the_incident_open(self):
        """GREEN: the candidate states NO_DUE_CHECKS and touches nothing."""
        ch = self.candidate(opt_in=True)
        ch.nothing_due()
        result = ch.tick()
        self.assertEqual(0, result["runner_rc"])
        self.assertEqual("GUARD_BUNDLE_OBSERVATION_V1 NO_DUE_CHECKS\n",
                         result["runner_out"])
        self.assertEqual([], result["cards"])            # no board call at all
        assert_no_false_recovery(self, result)

    def test_r01_green_check_is_sensitive_to_the_fix(self):
        """The GREEN assertion MUST fail against the frozen chain."""
        ch = self.frozen()
        ch.nothing_due()
        with self.assertRaises(AssertionError):
            assert_no_false_recovery(self, ch.tick())

    def test_r05_frozen_consumer_closes_on_exit_1_with_empty_stdout(self):
        """RED: a crashed producer with no output read as 'condition cleared'."""
        ch = self.frozen()
        ch.producer_stdout, ch.producer_rc = "", 1
        result = ch.consume()
        verbs = verbs_of(result["cards"])
        self.assertIn("complete", verbs)
        self.assertIn("archive", verbs)
        self.assertNotIn("guard-bundle-15m", result["rtb_state"])

    def test_r05_candidate_consumer_reports_instead_of_closing(self):
        """GREEN: applies to a NON-opted job too (global rc != 0 dominance)."""
        ch = self.candidate(opt_in=False)
        ch.producer_stdout, ch.producer_rc, ch.producer_stderr = "", 1, "boom"
        result = ch.consume()
        verbs = verbs_of(result["cards"])
        self.assertIn("comment", verbs)
        body = next(c for c in result["cards"] if "comment" in c)[-1]
        self.assertIn("PRODUCER EXIT 1", body)
        self.assertIn("boom", body)
        assert_no_false_recovery(self, result)

    def test_r05_green_check_is_sensitive_to_the_fix(self):
        ch = self.frozen()
        ch.producer_stdout, ch.producer_rc = "", 1
        with self.assertRaises(AssertionError):
            assert_no_false_recovery(self, ch.consume())

    def test_r24_non_opted_consumer_path_is_unchanged(self):
        """A legacy clean tick still closes: the fix is strictly opt-in."""
        results = []
        for chain in (self.frozen(), self.candidate(opt_in=False)):
            chain.producer_stdout, chain.producer_rc = "", 0
            results.append(chain.consume())
        for result in results:
            verbs = verbs_of(result["cards"])
            self.assertIn("complete", verbs)
            self.assertIn("archive", verbs)
            self.assertEqual({}, result["rtb_state"])
        self.assertEqual(results[0]["consumer_rc"], results[1]["consumer_rc"])
        self.assertEqual(verbs_of(results[0]["cards"]), verbs_of(results[1]["cards"]))


class LegacyEquivalenceTests(ChainTestCase):
    """With the opt-in absent, the candidate must be indistinguishable."""

    TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def _failing_scenario(self):
        runs = []
        for chain in (self.frozen(), self.candidate(opt_in=False)):
            chain.check_results = {"A": (1, "[A] exact failure text", True)}
            runs.append(chain.tick())
        return runs

    def test_non_opted_runner_output_is_byte_identical_on_a_failing_tick(self):
        first, second = self._failing_scenario()
        self.assertEqual(1, first["runner_rc"])
        self.assertEqual(first["runner_rc"], second["runner_rc"])
        self.assertEqual(self.TS.sub("<ts>", first["runner_out"]),
                         self.TS.sub("<ts>", second["runner_out"]))
        self.assertIn("[A] exact failure text", second["runner_out"])
        self.assertNotIn("GUARD_BUNDLE_OBSERVATION_V1", second["runner_out"])

    def test_non_opted_runner_state_is_equivalent_on_a_failing_tick(self):
        self._failing_scenario()
        a, b = self._states()
        self.assertEqual(sorted(a), sorted(b))
        for key in a:
            if key.endswith(":last_error"):
                self.assertEqual(a[key], b[key])
            elif key.endswith(":last_status"):
                self.assertEqual(a[key], b[key])
            else:
                self.assertLessEqual(abs(int(a[key]) - int(b[key])), 2)

    def _states(self):
        out = []
        for chain in self.chains:
            path = chain.runner.STATE_FILE
            out.append(json.loads(path.read_text()) if path.exists() else {})
        return out

    def test_non_opted_runner_is_silent_on_an_all_pass_tick(self):
        runs = []
        for chain in (self.frozen(), self.candidate(opt_in=False)):
            runs.append(chain.tick())
        self.assertEqual("", runs[0]["runner_out"])
        self.assertEqual(runs[0]["runner_out"], runs[1]["runner_out"])
        self.assertEqual(0, runs[1]["runner_rc"])

    def test_verdict_appears_only_when_the_job_opts_in(self):
        off = self.candidate(opt_in=False)
        off_result = off.tick()
        off.restore()  # env is process-wide: be explicit about which one runs
        on = self.candidate(opt_in=True)
        on_result = on.tick()
        self.assertEqual("", off_result["runner_out"])
        self.assertEqual("GUARD_BUNDLE_OBSERVATION_V1 CLEAN\n", on_result["runner_out"])

    def test_non_opted_consumer_keeps_the_legacy_close_wording(self):
        chain = self.candidate(opt_in=False)
        chain.producer_stdout, chain.producer_rc = "", 0
        chain.consume()
        summary = next(c for c in chain.board_calls if "complete" in c)[-1]
        self.assertIn("reported nothing this run", summary)
        self.assertNotIn("due checks passed", summary)
        self.assertNotIn("guard-bundle-v1", summary)

    def test_opted_in_consumer_qualifies_the_close_wording(self):
        chain = self.candidate(opt_in=True)
        chain.producer_stdout, chain.producer_rc = "GUARD_BUNDLE_OBSERVATION_V1 CLEAN\n", 0
        chain.consume()
        summary = next(c for c in chain.board_calls if "complete" in c)[-1]
        self.assertIn("completed due checks passed and pending rechecks cleared",
                      summary)
        self.assertNotIn("GUARD_BUNDLE_OBSERVATION_V1", summary)