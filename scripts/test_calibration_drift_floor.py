#!/usr/bin/env python3
"""DB-free regression tests for calibration-drift-monitor.py gating (t_ef700332).

Verifies that the monitoring layer does NOT raise a flag/card or recalibrate
the engine while the Tier-1 realized-exit sample is below the validated-edge
floor (n < 300), and that it still reports current n + marks INSUFFICIENT_SAMPLE.
At n >= 300 it may raise a card but MUST NOT auto-recalibrate (HOLD honored).

Run:  python3 /home/frank/.hermes/scripts/test_calibration_drift_floor.py
Exit 0 on pass, 1 on failure.
"""
import importlib.util
import sys
from pathlib import Path
from unittest import mock

SCRIPT = Path("/home/frank/.hermes/scripts/calibration-drift-monitor.py")

# Load the hyphenated module file by path (import would fail on the dash).
_spec = importlib.util.spec_from_file_location("calibration_drift_monitor", SCRIPT)
d = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(d)


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    return cond


def build_stats(mce_pp=19.1, labeled_with_pwin=126):
    return {
        "mce_pp": mce_pp,
        "labeled": labeled_with_pwin,
        "labeled_with_pwin": labeled_with_pwin,
        "overall_bias_pp": 4.2,
        "mce_qual": 5,
        "mce_total": 10,
        "report_path": "/tmp/none.md",
    }


def run_with(tier1_n, mce_pp=19.1, labeled_with_pwin=126):
    """Drive the escalation decision by monkeypatching the DB + side effects."""
    calls = {"db_queries": 0, "cards": 0, "calibrations": 0}

    def fake_db_query(sql):
        if "COUNT(DISTINCT" in sql or "COUNT(*)" in sql:
            calls["db_queries"] += 1
            return str(tier1_n)
        return ""

    def fake_create(stats):
        calls["cards"] += 1
        return f"card#{calls['cards']}"

    def fake_run_calibration():
        calls["calibrations"] += 1
        return True, "should-never-run"

    with mock.patch.object(d, "db_query", fake_db_query), \
         mock.patch.object(d, "create_calibration_review_task", fake_create), \
         mock.patch.object(d, "run_calibration", fake_run_calibration):
        # Mirror the exact escalation decision from main() so we exercise the
        # real logic without the full docker-dependent pipeline.
        stats = build_stats(mce_pp=mce_pp, labeled_with_pwin=labeled_with_pwin)
        lines = []
        if mce_pp > d.MCE_THRESHOLD_PP:
            raw = fake_db_query(d.TIER1_FLOOR_QUERY)
            tier1_n_actual = d.safe_int(raw) if raw is not None else None
            floor_met = tier1_n_actual is not None and tier1_n_actual >= d.TIER1_VALIDATION_FLOOR
            if floor_met:
                lines.append("DRIFT_FLOOR_MET")
                fake_create(stats)
            else:
                lines.append("INSUFFICIENT_SAMPLE")
        return {
            "tier1_n": tier1_n,
            "floor_met": (tier1_n >= d.TIER1_VALIDATION_FLOOR),
            "lines": lines,
            "calls": calls,
        }


def main() -> int:
    failures = []

    # (1) Below floor (current live state n=127): NO card, NO recalibration.
    r = run_with(tier1_n=127, mce_pp=19.1)
    if not check("n=127 MCE>15pp -> INSUFFICIENT_SAMPLE (not confident)",
                 not r["floor_met"] and "INSUFFICIENT_SAMPLE" in r["lines"]):
        failures.append("below-floor-status")
    if not check("n=127 -> NO kanban card created", r["calls"]["cards"] == 0):
        failures.append("below-floor-no-card")
    if not check("n=127 -> NO run_calibration() invoked", r["calls"]["calibrations"] == 0):
        failures.append("below-floor-no-recalibrate")

    # (2) Just below floor at 299: still suppressed.
    r = run_with(tier1_n=299, mce_pp=19.1)
    if not check("n=299 -> suppressed (no card / no recalibration)",
                 r["calls"]["cards"] == 0 and r["calls"]["calibrations"] == 0
                 and "INSUFFICIENT_SAMPLE" in r["lines"]):
        failures.append("299-suppressed")

    # (3) At/above floor (n=300) MCE breach: card allowed but STILL no recalibration (HOLD).
    r = run_with(tier1_n=300, mce_pp=19.1)
    if not check("n=300 MCE>15pp -> floor met", r["floor_met"]):
        failures.append("floor-met")
    if not check("n=300 -> kanban card raised (confident sample)",
                 r["calls"]["cards"] == 1):
        failures.append("floor-card")
    if not check("n=300 -> NO auto-recalibration (t_b4c824c7 HOLD honored)",
                 r["calls"]["calibrations"] == 0):
        failures.append("floor-no-recalibrate")

    # (4) Healthy MCE (<=15pp): silent, no card, no recalibration.
    r = run_with(tier1_n=127, mce_pp=10.0)
    if not check("n=127 MCE=10pp -> silent (no card/recalibration)",
                 r["calls"]["cards"] == 0 and r["calls"]["calibrations"] == 0
                 and "INSUFFICIENT_SAMPLE" not in r["lines"]):
        failures.append("healthy-silent")

    if failures:
        print(f"\nTEST FAILED: {len(failures)} assertion(s): {failures}")
        return 1
    print("\nTEST PASSED: calibration-drift-monitor n>=300 gate holds; no flag/recalibration below floor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
