#!/usr/bin/env python3
"""
Unit tests for calibration-drift-monitor.py — defect fixes.

Covers (no DB required):
  - Defect 2: sample-weighted MCE headline + significant-bucket figure
  - Defect 1: MEASUREMENT_UNAVAILABLE marker, fail-loud report/card on probe
    failure (never renders 0pp for an unmeasured metric)
  - Defect 3: estimator_provenance parses the weights_version split and
    returns None on probe failure
  - Wilson CI helper matches the team's cited 95% intervals
"""

import os
import sys
import importlib.util
import unittest
import unittest.mock as mock

# The production module is named with hyphens (calibration-drift-monitor.py)
# and is exec'd by path by the cron shim, so it can't be imported by name.
# Load it explicitly from its file path.
_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "calibration-drift-monitor.py")
_spec = importlib.util.spec_from_file_location("calibration_drift_monitor", _SCRIPT_PATH)
cdm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cdm)
import subprocess as _subprocess


# Buckets reproduced from the 2026-07-31 triage (t_48c60e86).
# error_pp kept at higher precision than the published 1dp table so the
# weighted mean lands near the cited 15.13pp.
TRIAGE_BUCKETS = [
    {"bucket": "20-30%", "n": 2,   "avg_predicted_p": 0.277, "actual_wr": 0.500, "error_pp": 22.3},
    {"bucket": "30-40%", "n": 15,  "avg_predicted_p": 0.358, "actual_wr": 0.133, "error_pp": 22.4},
    {"bucket": "40-50%", "n": 22,  "avg_predicted_p": 0.465, "actual_wr": 0.409, "error_pp": 5.6},
    {"bucket": "50-60%", "n": 124, "avg_predicted_p": 0.552, "actual_wr": 0.435, "error_pp": 11.7},
    {"bucket": "60-70%", "n": 49,  "avg_predicted_p": 0.627, "actual_wr": 0.367, "error_pp": 26.0},
    {"bucket": "70-80%", "n": 1,   "avg_predicted_p": 0.722, "actual_wr": 0.000, "error_pp": 72.2},
]


class TestMCE(unittest.TestCase):
    def test_unweighted_matches_legacy(self):
        mce = cdm.compute_mce(TRIAGE_BUCKETS, cdm.MIN_BUCKET_SIZE)
        # Legacy unweighted mean over n>=5 buckets = 16.41pp (triage).
        self.assertAlmostEqual(mce["mce_unweighted_pp"], 16.4, delta=0.2)
        # Sample-weighted headline should be BELOW the unweighted figure
        # (the huge 1/2-row buckets were previously inflating it).
        self.assertLess(mce["mce_pp"], mce["mce_unweighted_pp"])

    def test_sample_weighted_headline(self):
        mce = cdm.compute_mce(TRIAGE_BUCKETS, cdm.MIN_BUCKET_SIZE)
        eligible = [b for b in TRIAGE_BUCKETS if b["n"] >= cdm.MIN_BUCKET_SIZE]
        total_n = sum(b["n"] for b in eligible)
        expected = round(sum(b["n"] * b["error_pp"] for b in eligible) / total_n, 1)
        self.assertAlmostEqual(mce["mce_pp"], expected, delta=0.05)
        # Published cited value: 15.13pp.
        self.assertAlmostEqual(mce["mce_pp"], 15.1, delta=0.2)

    def test_significant_only_figure(self):
        mce = cdm.compute_mce(TRIAGE_BUCKETS, cdm.MIN_BUCKET_SIZE)
        # Only 50-60% and 60-70% predict outside their 95% Wilson CI.
        self.assertEqual(mce["significant_buckets"], 2)
        # Unweighted mean of those two buckets = (11.7+26.0)/2 = 18.85pp -> 18.8/18.9.
        self.assertAlmostEqual(mce["mce_significant_pp"], 18.8, delta=0.2)

    def test_empty_buckets_safe(self):
        mce = cdm.compute_mce([], cdm.MIN_BUCKET_SIZE)
        self.assertEqual(mce["mce_pp"], 0.0)
        self.assertEqual(mce["qualifying_buckets"], 0)


class TestWilsonCI(unittest.TestCase):
    def test_30_40_bucket_interval(self):
        # Triage cites [3.7, 37.9]% for n=15, WR=13.3%.
        k = round(0.133 * 15)
        lo = cdm.wilson_ci_lower(k, 15)
        hi = cdm.wilson_ci_upper(k, 15)
        self.assertAlmostEqual(lo, 0.037, delta=0.005)
        self.assertAlmostEqual(hi, 0.379, delta=0.005)

    def test_significant_flags(self):
        sig = [cdm.bucket_significant(b) for b in TRIAGE_BUCKETS]
        # Truth: only the 50-60% and 60-70% buckets (indices 3,4) are significant.
        self.assertEqual(sig, [False, False, False, True, True, False])


class TestProvenance(unittest.TestCase):
    def test_parse_split(self):
        fake = "conviction-score-v1|206\ncc-fit-v2|7\n"
        with mock.patch.object(cdm, "db_query", return_value=fake):
            prov = cdm.estimator_provenance()
        self.assertEqual(prov, {"conviction-score-v1": 206, "cc-fit-v2": 7})

    def test_probe_failure_returns_none(self):
        # A failed/timeouted probe must yield None so callers emit
        # MEASUREMENT_UNAVAILABLE — never silently succeed with empty data.
        with mock.patch.object(cdm, "db_query", return_value=None):
            self.assertIsNone(cdm.estimator_provenance())

    def test_version_split_rendered_when_data_present(self):
        # Defect 3 regression: when estimator_provenance() returns data, the
        # markdown report MUST render the weights_version split table (with
        # counts and shares) plus the conviction-error NOTE so a reader can
        # tell the MCE headline is conviction error, not CC calibration error.
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        overview = {"total_journeys": 1000, "labeled": 500, "labeled_with_pwin": 480,
                    "pct_labeled_with_pwin": 96.0, "earliest": "2026-07-24",
                    "latest": "2026-07-31"}
        overall = {"n": 213, "overall_wr_pct": 39.4, "avg_predicted_pct": 54.5,
                   "overall_bias_pp": -15.0}
        provenance = {"conviction-score-v1": 206, "cc-fit-v2": 7}
        report = cdm.build_report(now, overview, overall, TRIAGE_BUCKETS,
                                  cdm.compute_mce(TRIAGE_BUCKETS), provenance)

        # Section header and table header must be present.
        self.assertIn("## Estimator Provenance", report)
        self.assertIn("| weights_version | N | Share | Estimator |", report)
        # Each version renders its count, share, and estimator label.
        self.assertIn("| conviction-score-v1 | 206 | 96.7% | "
                      "raw ConvictionScore alias (uncalibrated) |", report)
        self.assertIn("| cc-fit-v2 | 7 | 3.3% | "
                      "genuine Composite Confidence isotonic fit |", report)
        # The NOTE must fire when the alias dominates (>50% of the cohort).
        self.assertIn("conviction error", report)
        self.assertIn("not Composite Confidence", report)
        self.assertIn("t_dc9684fe", report)

    def test_version_split_not_rendered_when_probe_fails(self):
        # Fail-loud: when the provenance probe itself fails (None), the report
        # must emit MEASUREMENT_UNAVAILABLE instead of an empty/absent table.
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        overview = {"total_journeys": 1000, "labeled": 500, "labeled_with_pwin": 480,
                    "pct_labeled_with_pwin": 96.0, "earliest": "2026-07-24",
                    "latest": "2026-07-31"}
        overall = {"n": 213, "overall_wr_pct": 39.4, "avg_predicted_pct": 54.5,
                   "overall_bias_pp": -15.0}
        report = cdm.build_report(now, overview, overall, TRIAGE_BUCKETS,
                                  cdm.compute_mce(TRIAGE_BUCKETS), None)
        self.assertIn("## Estimator Provenance", report)
        self.assertIn(cdm.MEASUREMENT_UNAVAILABLE, report)
        self.assertIn("estimator split probe failed", report)


class TestFailLoud(unittest.TestCase):
    def test_report_emits_unavailable_on_probe_failure(self):
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        overview = {"total_journeys": 1000, "labeled": 500, "labeled_with_pwin": 480,
                    "pct_labeled_with_pwin": 96.0, "earliest": "2026-07-24",
                    "latest": "2026-07-31"}
        # overall=None simulates a timed-out overall_calibration() probe.
        report = cdm.build_report(now, overview, None, TRIAGE_BUCKETS,
                                  cdm.compute_mce(TRIAGE_BUCKETS), {"conviction-score-v1": 206})
        # The section is present (not skipped) and shows MEASUREMENT_UNAVAILABLE.
        self.assertIn("## Overall Calibration", report)
        self.assertIn(cdm.MEASUREMENT_UNAVAILABLE, report)
        # It must NOT render a fabricated 0pp bias.
        self.assertNotIn("| Overall bias (WR - predicted) | 0pp |", report)
        # Estimator provenance section is present.
        self.assertIn("## Estimator Provenance", report)
        self.assertIn("conviction-score-v1", report)

    def test_card_emits_unavailable_on_probe_failure(self):
        stats = {
            "mce": cdm.compute_mce(TRIAGE_BUCKETS),
            "labeled": 500, "labeled_with_pwin": 480,
            "buckets": TRIAGE_BUCKETS,
            "overall": None,  # failed probe
            "report_path": "/tmp/x.md",
        }
        # create_calibration_review_task calls `hermes kanban create --body <body>`.
        # Capture the args it passes so we can assert on the rendered body
        # without actually raising a ticket.
        captured = {}
        def _fake_run(cmd, **kwargs):
            captured["args"] = cmd
            return _subprocess.CompletedProcess(cmd, 0, "/t/1")
        with mock.patch.object(_subprocess, "run", side_effect=_fake_run):
            cdm.create_calibration_review_task(stats)
        args = captured.get("args", [])
        # --body is the value following the "--body" flag.
        self.assertIn("--body", args)
        body = args[args.index("--body") + 1]
        self.assertIn(cdm.MEASUREMENT_UNAVAILABLE, body)
        self.assertNotIn("Overall bias: 0pp", body)


class TestDefect1Timeout(unittest.TestCase):
    """Regression tests for Defect 1 (t_86edb1ff): a timed-out/failed
    db_query must NEVER render unmeasured metrics as numbers. It must produce
    an explicit MEASUREMENT_UNAVAILABLE marker (or abort the tick loudly), and
    the timeout must sit comfortably above the observed ~25s worst case."""

    def test_timeout_setting_above_observed_worst_case(self):
        # Observed 22.35s/24.87s/22.90s under 05:00 UTC load; the old 30s
        # ceiling was routinely crossed. Pin >= 60s so nobody lowers it back.
        self.assertGreaterEqual(cdm.DB_QUERY_TIMEOUT, 60)

    def test_db_query_returns_none_on_timeout(self):
        # A slow psql that exceeds the timeout must surface as None (probe
        # failure), not as an empty string that looks like a real result.
        def _slow(cmd, **kwargs):
            raise _subprocess.TimeoutExpired(cmd, timeout=cdm.DB_QUERY_TIMEOUT)
        with mock.patch.object(_subprocess, "run", side_effect=_slow):
            self.assertIsNone(cdm.db_query("SELECT 1"))

    def test_db_query_returns_none_on_nonzero_exit(self):
        def _fail(cmd, **kwargs):
            return _subprocess.CompletedProcess(cmd, 1, "error", "boom")
        with mock.patch.object(_subprocess, "run", side_effect=_fail):
            self.assertIsNone(cdm.db_query("SELECT 1"))

    def test_overall_calibration_returns_none_not_empty_dict(self):
        # Contract: a failed probe returns None (distinguishable from a
        # genuine zero-row result which returns {}). The old code coerced
        # None -> {} which made main()'s `overall is None` diagnostic dead
        # and hid the failure behind an empty dict.
        with mock.patch.object(cdm, "db_query", return_value=None):
            self.assertIsNone(cdm.overall_calibration())
        with mock.patch.object(cdm, "db_query", return_value=""):
            self.assertEqual(cdm.overall_calibration(), {})

    def test_overview_and_buckets_return_none_on_failure(self):
        # Early probes use the same contract so main() can abort loudly
        # instead of silently skipping (fail-quiet).
        with mock.patch.object(cdm, "db_query", return_value=None):
            self.assertIsNone(cdm.sampling_overview())
            self.assertIsNone(cdm.calibration_buckets())

    def test_report_never_renders_0pp_when_overall_probe_fails(self):
        # Full-path check: mock the slow/failing db_query itself, run the
        # real overall_calibration() (returns None), then render the report.
        # Must contain the marker and NEVER "0pp" / "0%".
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        overview = {"total_journeys": 1000, "labeled": 500, "labeled_with_pwin": 480,
                    "pct_labeled_with_pwin": 96.0, "earliest": "2026-07-24",
                    "latest": "2026-07-31"}
        with mock.patch.object(cdm, "db_query", return_value=None):
            overall = cdm.overall_calibration()
        self.assertIsNone(overall)
        report = cdm.build_report(now, overview, overall, TRIAGE_BUCKETS,
                                  cdm.compute_mce(TRIAGE_BUCKETS), {"conviction-score-v1": 206})
        self.assertIn("## Overall Calibration", report)
        self.assertIn(cdm.MEASUREMENT_UNAVAILABLE, report)
        # Never render an unmeasured metric as a number (the prose explaining
        # the marker legitimately mentions "0pp", so assert on the metric rows).
        self.assertNotIn("| Overall bias (WR - predicted) | 0pp |", report)
        self.assertNotIn("| Overall WR | 0% |", report)
        self.assertNotIn("| Avg predicted p_win | 0% |", report)

    def test_main_aborts_loudly_when_overview_probe_fails(self):
        # The overview probe is the first query each tick. If it fails, main()
        # must emit MEASUREMENT_UNAVAILABLE on stdout (so the no_agent cron
        # delivers it) and exit nonzero — not return 0 silently.
        import io
        from contextlib import redirect_stdout, redirect_stderr
        with mock.patch.object(cdm, "db_query", return_value=None):
            buf, err = io.StringIO(), io.StringIO()
            with redirect_stdout(buf), redirect_stderr(err):
                rc = cdm.main()
        self.assertEqual(rc, 3)
        self.assertIn(cdm.MEASUREMENT_UNAVAILABLE, buf.getvalue())
        self.assertIn("probe failed/timeout", buf.getvalue())

    def test_main_aborts_loudly_when_bucket_probe_fails(self):
        # Same contract for the bucket probe: nonzero exit + marker on stdout.
        # Overview succeeds (valid dict); the bucket probe itself fails.
        import io
        from contextlib import redirect_stdout, redirect_stderr
        overview = {"total_journeys": 1000, "labeled": 500, "labeled_with_pwin": 480,
                    "pct_labeled_with_pwin": 96.0, "earliest": "2026-07-24",
                    "latest": "2026-07-31"}
        with mock.patch.object(cdm, "sampling_overview", return_value=overview):
            with mock.patch.object(cdm, "calibration_buckets", return_value=None):
                buf, err = io.StringIO(), io.StringIO()
                with redirect_stdout(buf), redirect_stderr(err):
                    rc = cdm.main()
        self.assertEqual(rc, 3)
        self.assertIn(cdm.MEASUREMENT_UNAVAILABLE, buf.getvalue())


if __name__ == "__main__":
    unittest.TextTestRunner(verbosity=2).run(unittest.TestLoader().loadTestsFromModule(__import__(__name__)))

