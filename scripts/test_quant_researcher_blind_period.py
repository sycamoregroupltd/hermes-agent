#!/usr/bin/env python3
"""Regression tests for t_26cdaf62: blind-period cohort mislabelling fix.

Acceptance criterion #3 of t_26cdaf62: a blind-period cohort must NEVER be
emitted under a 'Validated' label. During the 14-day clean-epoch blind period
(in_early_epoch=True) the fresh-N gate is softened (N>=50), but NO cohort may
be VALIDATED -- even one whose fresh N already clears the full N>=300 floor.

These tests exercise the pure gate logic (gate_cohort) and the report split
(validated vs early_cohorts vs failed) WITHOUT a database, by importing the
module functions directly. They pin the contract so a future change to
gate_cohort cannot silently re-route a blind-period cohort into VALIDATED.
"""

import importlib.util
import os

QR = "/home/frank/.hermes/scripts/quant_researcher_6h.py"


def _load():
    spec = importlib.util.spec_from_file_location("qr_blind_test", QR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_qr():
    try:
        return _load()
    except Exception as e:  # pragma: no cover
        raise SystemExit(f"SKIP: quant_researcher module not importable: {e}")


def test_blind_period_high_n_never_validated():
    """The exact defect: N=400, WR=59.66%, stale=0% during the blind period.

    Before the fix gate_cohort returned early_epoch_only=False here (because
    n_clean_fresh >= N_THRESH), so main() routed it into `validated` and the
    script printed it under '## Validated Cohorts (Passed All Gates)'. After
    the fix it MUST be EARLY-EPOCH only.
    """
    qr = load_qr()
    pass_gate, early_epoch_only = qr.gate_cohort(
        n_clean_fresh=400,
        wr_clean_fresh_v=59.66,
        clean_stale_share=0.0,
        kill_listed=False,
        in_early_epoch=True,
    )
    assert pass_gate is True, "cohort should pass the (softened) blind-period gate"
    assert early_epoch_only is True, (
        "blind-period passing cohort MUST be EARLY-EPOCH, never VALIDATED "
        "(N=400 clears full floor but epoch is still < 14d old)"
    )
    print("PASS test_blind_period_high_n_never_validated")


def test_post_blind_high_n_is_validated():
    """After the blind period expires (in_early_epoch=False) a high-N cohort
    that clears the full gate is VALIDATED (not early-epoch)."""
    qr = load_qr()
    pass_gate, early_epoch_only = qr.gate_cohort(
        n_clean_fresh=400,
        wr_clean_fresh_v=59.66,
        clean_stale_share=0.0,
        kill_listed=False,
        in_early_epoch=False,
    )
    assert pass_gate is True
    assert early_epoch_only is False, (
        "post-blind-period cohort clearing the full gate must be VALIDATED, "
        "not EARLY-EPOCH"
    )
    print("PASS test_post_blind_high_n_is_validated")


def test_blind_period_low_n_ramp_is_early_epoch():
    """A cohort passing ONLY via the softened N>=50 ramp (N=60) is also
    EARLY-EPOCH (sanity: the ramp still produces early-epoch cohorts)."""
    qr = load_qr()
    pass_gate, early_epoch_only = qr.gate_cohort(
        n_clean_fresh=60,
        wr_clean_fresh_v=55.0,
        clean_stale_share=2.0,
        kill_listed=False,
        in_early_epoch=True,
    )
    assert pass_gate is True
    assert early_epoch_only is True
    print("PASS test_blind_period_low_n_ramp_is_early_epoch")


def test_kill_listed_never_passes():
    qr = load_qr()
    pass_gate, early_epoch_only = qr.gate_cohort(
        n_clean_fresh=400,
        wr_clean_fresh_v=59.66,
        clean_stale_share=0.0,
        kill_listed=True,
        in_early_epoch=True,
    )
    assert pass_gate is False
    assert early_epoch_only is False
    print("PASS test_kill_listed_never_passes")


def test_report_split_blind_period_no_validated_header():
    """Replicate main()'s split logic: while in_early_epoch, a passing cohort
    must land in early_cohorts and leave `validated` empty, so the
    'Validated Cohorts' header is never emitted. This mirrors the exact
    DataFrame split at lines ~606-619 of the script."""
    qr = load_qr()
    # Simulate the 15m SHORT/LOW/RISK_OFF blind-period passing cohort.
    rows = [{
        "timeframe": "15m", "direction": "SHORT", "volatility": "LOW",
        "macro_regime": "RISK_OFF", "fav": True,
        "n_all": 400, "wr_all": 59.66, "n_fresh": 400, "wr_fresh": 59.66,
        "lag_min_med": 14.0, "fresh_window_min": 15, "fresh_lag_med": 14.0,
        "stale_share": 0.0, "n_clean_fresh": 400, "wr_clean_fresh": 59.66,
        "clean_stale_share": 0.0, "n_contam": 0, "contaminated": False,
        "kill_listed": False,
    }]
    df = qr.pl.DataFrame(rows)
    in_early_epoch = True  # blind period active

    # Inline mirror of main()'s gate + split (kept verbatim-shaped).
    gate_rows = []
    for r in df.to_dicts():
        pg, eeo = qr.gate_cohort(
            r["n_clean_fresh"], r["wr_clean_fresh"], r["clean_stale_share"],
            r["kill_listed"], in_early_epoch)
        r = dict(r)
        r["pass"] = pg
        r["early_epoch"] = eeo
        gate_rows.append(r)
    gdf = qr.pl.DataFrame(gate_rows)
    passed = gdf.filter(qr.pl.col("pass"))
    validated = passed.filter(~qr.pl.col("early_epoch"))
    early_cohorts = passed.filter(qr.pl.col("early_epoch"))

    assert validated.height == 0, (
        "blind-period passing cohort must NOT be in the VALIDATED bucket"
    )
    assert early_cohorts.height == 1, (
        "blind-period passing cohort must be in the EARLY-EPOCH bucket"
    )
    print("PASS test_report_split_blind_period_no_validated_header")


if __name__ == "__main__":
    test_blind_period_high_n_never_validated()
    test_post_blind_high_n_is_validated()
    test_blind_period_low_n_ramp_is_early_epoch()
    test_kill_listed_never_passes()
    test_report_split_blind_period_no_validated_header()
    print("ALL PASS")
