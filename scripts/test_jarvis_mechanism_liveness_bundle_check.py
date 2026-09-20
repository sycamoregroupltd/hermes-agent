#!/usr/bin/env python3
"""Regression tests for t_a781c1f2 / t_dac2f057 / t_93ec8c2f: per-check bundle status.

Bug: jarvis_mechanism_liveness_collect.py's bundle_check classification used
to call classify_job() on the WHOLE guard-bundle cron job record, so any
sibling check's failure that tick flipped every other (healthy) check in the
same bundle to DEAD too. cron_guard_bundle_runner.py now persists each
check's OWN last_status/last_error into guard_bundle_last_run.json; the
collector must classify from that per-check state, not the bundle aggregate.

t_93ec8c2f: remap also used to require the paused CONDENSE source row. When
that row was pruned, BUNDLE_ALIASES keys (registered-implies-ticking,
black-hole-weekly, leak-guard) classified as missing-job DEAD even though
the live bundle check was healthy.

Covers:
  (a) bundle-mapped check with its own status=ok while a sibling in the same
      bundle tick failed -> collector reports OK (regression test for the
      exact reported bug).
  (b) bundle-mapped check with its own status=error -> still DEAD, with its
      OWN error text (not a stale/wrong sibling's).
  (c) missing per-check state entry (never run / old-format state file
      without the new keys) -> still DEAD, never a KeyError/crash; must
      degrade safely on state files that predate this fix.
  (d) t_93ec8c2f: BUNDLE_ALIASES key whose CONDENSE source row is gone still
      classifies from the live bundle check (false-DEAD missing-job fix).
  (e) non-absorbed missing job stays DEAD (no silent remap).
  (f) source-gone + failed own check still DEAD.

Run:
  python3 scripts/test_jarvis_mechanism_liveness_bundle_check.py
  python3 -m pytest scripts/test_jarvis_mechanism_liveness_bundle_check.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent
MODULE_PATH = REPO / "jarvis_mechanism_liveness_collect.py"

spec = importlib.util.spec_from_file_location("mech_collect_under_test", MODULE_PATH)
assert spec is not None and spec.loader is not None
mech: Any = importlib.util.module_from_spec(spec)
# dataclasses._is_type() looks the defining module up via sys.modules[cls.__module__];
# it must be registered there BEFORE exec_module() runs the @dataclass decorator.
sys.modules[spec.name] = mech
spec.loader.exec_module(mech)


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


class BundleCheckStatusTests(unittest.TestCase):
    """Direct unit tests against bundle_check_status() (pure function, no I/O)."""

    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.bundle_job = {
            "id": "fixturebundle01",
            "enabled": True,
            "state": "scheduled",
            "last_run_at": self.now.isoformat(),
        }

    def test_healthy_check_ok_despite_failed_sibling_in_same_tick(self) -> None:
        """(a) THE regression: a clean check must not inherit a sibling's failure."""
        now_ts = _now_epoch()
        state = {
            # This check's own run: clean.
            "leak-guard-check": now_ts,
            "leak-guard-check:last_status": "ok",
            # A different, unrelated sibling check in the SAME bundle tick: failed.
            "black-hole-check": now_ts,
            "black-hole-check:last_status": "error",
            "black-hole-check:last_error": "[standing-no-black-holes-detector] exited 1",
        }
        status, reason, age = mech.bundle_check_status(
            "leak-guard-check", self.bundle_job, self.now, max_age_minutes=8 * 24 * 60, state=state,
        )
        self.assertEqual(status, "OK", reason)
        self.assertNotIn("black-hole", reason)

    def test_check_with_own_failure_is_dead_with_own_error(self) -> None:
        """(b) A check that genuinely failed its own run stays DEAD with its own text."""
        now_ts = _now_epoch()
        state = {
            "svc-gate-check": now_ts,
            "svc-gate-check:last_status": "error",
            "svc-gate-check:last_error": "[dgx-service-gate-escalation] exited 1: boom",
            # sibling healthy, must not mask the real failure either
            "other-check": now_ts,
            "other-check:last_status": "ok",
        }
        status, reason, age = mech.bundle_check_status(
            "svc-gate-check", self.bundle_job, self.now, max_age_minutes=90, state=state,
        )
        self.assertEqual(status, "DEAD")
        self.assertIn("boom", reason)
        self.assertNotIn("other-check", reason)

    def test_missing_per_check_state_entry_is_dead_not_crash(self) -> None:
        """(c) Check never present in state at all -> DEAD, no exception."""
        state: dict[str, Any] = {}
        status, reason, age = mech.bundle_check_status(
            "never-run-check", self.bundle_job, self.now, max_age_minutes=90, state=state,
        )
        self.assertEqual(status, "DEAD")
        self.assertIn("never-run-check", reason)

    def test_old_format_state_file_missing_status_key_degrades_to_dead(self) -> None:
        """(c) Old-format state file (timestamp only, pre-fix) -> DEAD, no KeyError."""
        now_ts = _now_epoch()
        state = {
            "legacy-check": now_ts,
            # no "legacy-check:last_status" key at all -- simulates a state
            # file written by the pre-t_a781c1f2 runner.
        }
        status, reason, age = mech.bundle_check_status(
            "legacy-check", self.bundle_job, self.now, max_age_minutes=90, state=state,
        )
        self.assertEqual(status, "DEAD")
        self.assertIn("predates per-check status tracking", reason)

    def test_bundle_job_disabled_is_dead_regardless_of_check_state(self) -> None:
        state = {"any-check": _now_epoch(), "any-check:last_status": "ok"}
        disabled_job = dict(self.bundle_job, enabled=False)
        status, reason, age = mech.bundle_check_status(
            "any-check", disabled_job, self.now, max_age_minutes=90, state=state,
        )
        self.assertEqual(status, "DEAD")
        self.assertIn("paused/disabled", reason)

    def test_bundle_job_never_run_is_dead_regardless_of_check_state(self) -> None:
        state = {"any-check": _now_epoch(), "any-check:last_status": "ok"}
        never_run_job = dict(self.bundle_job, last_run_at=None)
        status, reason, age = mech.bundle_check_status(
            "any-check", never_run_job, self.now, max_age_minutes=90, state=state,
        )
        self.assertEqual(status, "DEAD")
        self.assertIn("never run", reason)

    def test_stale_own_run_past_max_age_is_dead(self) -> None:
        stale_ts = _now_epoch() - 999999
        state = {"stale-check": stale_ts, "stale-check:last_status": "ok"}
        status, reason, age = mech.bundle_check_status(
            "stale-check", self.bundle_job, self.now, max_age_minutes=90, state=state,
        )
        self.assertEqual(status, "DEAD")
        self.assertIn("age", reason)


class RowForExpectedIntegrationTests(unittest.TestCase):
    """End-to-end through row_for_expected() with a fixture cron store."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.jarvis_home = root / "profiles" / "jarvis"
        (self.jarvis_home / "cron" / "state").mkdir(parents=True)
        (self.jarvis_home / "scripts").mkdir(parents=True)
        (root / "kanban" / "boards").mkdir(parents=True)

        # load_bundle_check() imports cron_guard_bundle_runner.py from
        # JARVIS_HOME/scripts to read the CHECKS manifest (script existence
        # check). Stub a minimal one in the fixture tree with the check
        # scripts these tests reference actually present on disk.
        (self.jarvis_home / "scripts" / "sycode_leak_guard_v2_watchdog.py").write_text("#!/usr/bin/env python3\n")
        (self.jarvis_home / "scripts" / "service_gate_escalation_watchdog.py").write_text("#!/usr/bin/env python3\n")
        (self.jarvis_home / "scripts" / "cron_health_canary_wrapper.sh").write_text("#!/bin/sh\n")
        (self.jarvis_home / "scripts" / "no_black_holes_detector.py").write_text("#!/usr/bin/env python3\n")
        runner = (
            "CHECKS = {\n"
            "    'sycode-canonical-leak-guard-v2-weekly': {'script': 'sycode_leak_guard_v2_watchdog.py'},\n"
            "    'dgx-service-gate-escalation': {'script': 'service_gate_escalation_watchdog.py'},\n"
            "    'cron-health-canary': {'script': 'cron_health_canary_wrapper.sh'},\n"
            "    'standing-no-black-holes-detector': {'script': 'no_black_holes_detector.py'},\n"
            "}\n"
        )
        (self.jarvis_home / "scripts" / "cron_guard_bundle_runner.py").write_text(runner)

        # Monkeypatch the module's path constants to our fixture tree.
        self._orig = {
            "ROOT": mech.ROOT, "PROFILES": mech.PROFILES, "JARVIS_HOME": mech.JARVIS_HOME,
            "BOARDS": mech.BOARDS, "STATE_DIR": mech.STATE_DIR, "OUTPUT_ROOT": mech.OUTPUT_ROOT,
        }
        mech.ROOT = root
        mech.PROFILES = root / "profiles"
        mech.JARVIS_HOME = self.jarvis_home
        mech.BOARDS = root / "kanban" / "boards"
        mech.STATE_DIR = root / "cron" / "state"
        mech.OUTPUT_ROOT = self.jarvis_home / "cron" / "output"

    def tearDown(self) -> None:
        for k, v in self._orig.items():
            setattr(mech, k, v)

    def _write_jobs(self, jobs: list[dict[str, Any]]) -> None:
        path = self.jarvis_home / "cron" / "jobs.json"
        path.write_text(json.dumps({"jobs": jobs}))

    def _write_state(self, state: dict[str, Any]) -> None:
        path = self.jarvis_home / "cron" / "state" / "guard_bundle_last_run.json"
        path.write_text(json.dumps(state))

    def _fixture_source_row_and_bundle(self, source_name: str, bundle_name: str) -> tuple[dict, dict]:
        now_iso = datetime.now(timezone.utc).isoformat()
        source_row = {
            "id": "src01", "name": source_name, "script": "whatever.py",
            "enabled": True, "state": "paused",
            "paused_reason": "CONDENSE 1/4 (kanban t_db689c47): absorbed into bundle",
        }
        bundle_row = {
            "id": "bundle01", "name": bundle_name, "script": "cron_guard_bundle_runner.py",
            "enabled": True, "state": "scheduled", "last_run_at": now_iso,
            "last_status": "error",  # aggregate reflects an unrelated sibling failure
            "last_error": "GUARD BUNDLE [daily] -- 1 failed check(s): [some-other-check] exited 1",
        }
        return source_row, bundle_row

    def test_leak_guard_ok_when_own_check_clean_even_if_bundle_aggregate_error(self) -> None:
        source_row, bundle_row = self._fixture_source_row_and_bundle(
            "sycode-canonical-leak-guard-v2-weekly", "guard-bundle-tick-daily",
        )
        self._write_jobs([source_row, bundle_row])
        now_ts = _now_epoch()
        self._write_state({
            "sycode-canonical-leak-guard-v2-weekly": now_ts,
            "sycode-canonical-leak-guard-v2-weekly:last_status": "ok",
            "some-other-check": now_ts,
            "some-other-check:last_status": "error",
            "some-other-check:last_error": "[some-other-check] exited 1",
        })
        exp = next(e for e in mech.EXPECTED if e.key == "leak-guard")
        row = mech.row_for_expected(exp, datetime.now(timezone.utc))
        self.assertEqual(row["status"], "OK", row["reason"])
        self.assertNotIn("some-other-check", row["reason"])

    def test_escalation_notifier_dead_when_own_check_failed(self) -> None:
        source_row, bundle_row = self._fixture_source_row_and_bundle(
            "dgx-service-gate-escalation", "guard-bundle-tick-15m",
        )
        self._write_jobs([source_row, bundle_row])
        now_ts = _now_epoch()
        self._write_state({
            "dgx-service-gate-escalation": now_ts,
            "dgx-service-gate-escalation:last_status": "error",
            "dgx-service-gate-escalation:last_error": "[dgx-service-gate-escalation] exited 1: real failure",
        })
        exp = next(e for e in mech.EXPECTED if e.key == "escalation-notifier-service-gate")
        row = mech.row_for_expected(exp, datetime.now(timezone.utc))
        self.assertEqual(row["status"], "DEAD")
        self.assertIn("real failure", row["reason"])

    def test_missing_state_file_entirely_degrades_to_dead_not_crash(self) -> None:
        source_row, bundle_row = self._fixture_source_row_and_bundle(
            "sycode-canonical-leak-guard-v2-weekly", "guard-bundle-tick-daily",
        )
        self._write_jobs([source_row, bundle_row])
        # No state file written at all.
        exp = next(e for e in mech.EXPECTED if e.key == "leak-guard")
        row = mech.row_for_expected(exp, datetime.now(timezone.utc))
        self.assertEqual(row["status"], "DEAD")
        self.assertIsInstance(row["reason"], str)

    def test_source_job_gone_still_ok_via_live_bundle_check(self) -> None:
        """(d) t_93ec8c2f: pruned CONDENSE source row must not false-DEAD."""
        _source_row, bundle_row = self._fixture_source_row_and_bundle(
            "cron-health-canary", "guard-bundle-tick-15m",
        )
        self._write_jobs([bundle_row])  # source row absent
        now_ts = _now_epoch()
        self._write_state({
            "cron-health-canary": now_ts,
            "cron-health-canary:last_status": "ok",
            "some-other-check": now_ts,
            "some-other-check:last_status": "error",
            "some-other-check:last_error": "[some-other-check] exited 1",
        })
        exp = next(e for e in mech.EXPECTED if e.key == "registered-implies-ticking")
        row = mech.row_for_expected(exp, datetime.now(timezone.utc))
        self.assertEqual(row["status"], "OK", row["reason"])
        self.assertIn("source job gone", row["reason"])
        self.assertEqual(row["job_name"], "guard-bundle-tick-15m")
        self.assertEqual(row["bundle_check"], "cron-health-canary")
        self.assertNotIn("some-other-check", row["reason"])

    def test_source_job_gone_black_hole_and_leak_guard_ok(self) -> None:
        """(d) weekly absorbed keys with source rows gone stay OK from own check."""
        now_iso = datetime.now(timezone.utc).isoformat()
        daily = {
            "id": "bundle-daily", "name": "guard-bundle-tick-daily",
            "script": "cron_guard_bundle_runner.py", "enabled": True,
            "state": "scheduled", "last_run_at": now_iso, "last_status": "ok",
        }
        self._write_jobs([daily])
        now_ts = _now_epoch()
        self._write_state({
            "standing-no-black-holes-detector": now_ts,
            "standing-no-black-holes-detector:last_status": "ok",
            "sycode-canonical-leak-guard-v2-weekly": now_ts,
            "sycode-canonical-leak-guard-v2-weekly:last_status": "ok",
        })
        now = datetime.now(timezone.utc)
        black = mech.row_for_expected(next(e for e in mech.EXPECTED if e.key == "black-hole-weekly"), now)
        leak = mech.row_for_expected(next(e for e in mech.EXPECTED if e.key == "leak-guard"), now)
        self.assertEqual(black["status"], "OK", black["reason"])
        self.assertEqual(leak["status"], "OK", leak["reason"])
        self.assertIn("source job gone", black["reason"])
        self.assertIn("source job gone", leak["reason"])

    def test_non_absorbed_missing_job_stays_dead(self) -> None:
        """(e) keys without BUNDLE_ALIASES must not remap to a bundle."""
        now_iso = datetime.now(timezone.utc).isoformat()
        self._write_jobs([{
            "id": "bundle01", "name": "guard-bundle-tick-15m",
            "script": "cron_guard_bundle_runner.py", "enabled": True,
            "state": "scheduled", "last_run_at": now_iso, "last_status": "ok",
        }])
        now_ts = _now_epoch()
        self._write_state({
            "cron-health-canary": now_ts,
            "cron-health-canary:last_status": "ok",
        })
        exp = next(e for e in mech.EXPECTED if e.key == "verdict-router")
        row = mech.row_for_expected(exp, datetime.now(timezone.utc))
        self.assertEqual(row["status"], "DEAD")
        self.assertIn("expected job not found", row["reason"])

    def test_source_job_gone_own_check_failed_still_dead(self) -> None:
        """(f) source-gone remap still DEAD when the live check itself failed."""
        _source_row, bundle_row = self._fixture_source_row_and_bundle(
            "sycode-canonical-leak-guard-v2-weekly", "guard-bundle-tick-daily",
        )
        self._write_jobs([bundle_row])
        now_ts = _now_epoch()
        self._write_state({
            "sycode-canonical-leak-guard-v2-weekly": now_ts,
            "sycode-canonical-leak-guard-v2-weekly:last_status": "error",
            "sycode-canonical-leak-guard-v2-weekly:last_error": "[sycode-canonical-leak-guard-v2-weekly] exited 1: leak",
        })
        exp = next(e for e in mech.EXPECTED if e.key == "leak-guard")
        row = mech.row_for_expected(exp, datetime.now(timezone.utc))
        self.assertEqual(row["status"], "DEAD")
        self.assertIn("leak", row["reason"])
        self.assertIn("source job gone", row["reason"])


if __name__ == "__main__":
    unittest.main()
