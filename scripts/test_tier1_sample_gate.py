#!/usr/bin/env python3
"""
test_tier1_sample_gate.py — DB-free unit tests for tier1_sample_gate.py.

Run:  python3 /home/frank/.hermes/scripts/test_tier1_sample_gate.py
Exit 0 on pass, 1 on failure. Mirrors self_test() but lives in its own file so
the test can be wired into CI / pre-commit without invoking the live script.
"""
import sys
from pathlib import Path

# Make the sibling module importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tier1_sample_gate as g  # noqa: E402


def check(name, cond):
    if cond:
        print(f"  PASS  {name}")
        return True
    print(f"  FAIL  {name}")
    return False


def main() -> int:
    failures = []

    # Suppression guarantee — the core requirement.
    d = g.gate_decision({"n": 126, "win_rate": 40.5, "avg_pnl": 0.48,
                         "weighted_mce": 19.1})
    if not check("n=126, MCE=19.1pp -> INVESTIGATE (suppressed)",
                 d["kind"] == "INVESTIGATE" and d["suppressed_breach"] is True):
        failures.append("suppress-126")

    d = g.gate_decision({"n": 299, "win_rate": 40.0, "avg_pnl": 0.0,
                         "weighted_mce": 999.0})
    if not check("n=299, MCE=999pp -> INVESTIGATE (suppressed)",
                 d["kind"] == "INVESTIGATE" and d["suppressed_breach"] is True):
        failures.append("suppress-299")

    # READY signal.
    d = g.gate_decision({"n": 300, "win_rate": 55.0, "avg_pnl": 0.7,
                         "weighted_mce": 8.0})
    if not check("n=300, healthy -> READY_FOR_VALIDATION",
                 d["kind"] == "READY_FOR_VALIDATION" and d["breach"] is False):
        failures.append("ready-300")

    # Confident breach.
    d = g.gate_decision({"n": 301, "win_rate": 48.0, "avg_pnl": 0.2,
                         "weighted_mce": 20.0})
    if not check("n=301, MCE=20pp -> BREACH",
                 d["kind"] == "BREACH" and d["breach"] is True):
        failures.append("breach-301")

    # Accumulation.
    d = g.gate_decision({"n": 260, "win_rate": 54.0, "avg_pnl": 0.6,
                         "weighted_mce": 11.0})
    if not check("n=260, healthy -> SAMPLE_ACCUMULATING",
                 d["kind"] == "SAMPLE_ACCUMULATING"):
        failures.append("accum-260")

    # Thin.
    d = g.gate_decision({"n": 42, "win_rate": 51.0, "avg_pnl": 0.2,
                         "weighted_mce": 9.0})
    if not check("n=42 -> THIN_SAMPLE",
                 d["kind"] == "THIN_SAMPLE" and d["breach"] is False):
        failures.append("thin-42")

    # Boundary.
    d_low = g.gate_decision({"n": 299, "win_rate": 48.0, "avg_pnl": 0.0,
                             "weighted_mce": 20.0})
    d_high = g.gate_decision({"n": 300, "win_rate": 48.0, "avg_pnl": 0.0,
                              "weighted_mce": 20.0})
    if not check("boundary 299->INVESTIGATE, 300->BREACH",
                 d_low["kind"] == "INVESTIGATE" and d_high["kind"] == "BREACH"):
        failures.append("boundary")

    # MCE math fidelity: a calibrated bucket yields ~0 error. Under the
    # avg-score method expected WR = mean(conviction_score)*100. Bucket
    # [0.50,0.55) with all scores = 0.525 -> mean 0.525 -> expected WR 52.5%;
    # 21 wins / 19 losses over 40 rows = 52.5% actual -> MCE ~0.
    rows = ([{"conviction_score": "0.525", "is_win": "t", "pnl_pct": "1.0"}
             for _ in range(21)]
            + [{"conviction_score": "0.525", "is_win": "f", "pnl_pct": "1.0"}
               for _ in range(19)])
    mce, cn = g.compute_sample_weighted_mce(rows)
    if not check("calibrated bucket 21w/19l -> MCE ~0",
                 cn == 40 and abs(mce) < 1e-6):
        failures.append("mce-zero")

    rows = [{"conviction_score": "0.97", "is_win": "t", "pnl_pct": "1.0"}
            for _ in range(40)]
    mce, cn = g.compute_sample_weighted_mce(rows)
    if not check("bucket [0.95,1.00] all wins -> MCE=3.0pp (avg-score method)",
                 cn == 40 and abs(mce - 3.0) < 1e-6):
        failures.append("mce-25")

    # (10) PARITY FIXTURE (t_5c238cc5): avg-score method must DIVERGE from
    # midpoint. Midpoint would give 57.5pp; avg-score gives 58.67pp. This
    # assertion fails on midpoint substitution, pinning the canonical report
    # math.
    rows = ([{"conviction_score": "0.40", "is_win": "t", "pnl_pct": "1.0"},
             {"conviction_score": "0.40", "is_win": "t", "pnl_pct": "1.0"},
             {"conviction_score": "0.44", "is_win": "t", "pnl_pct": "1.0"}])
    mce, cn = g.compute_sample_weighted_mce(rows)
    if not check("avg-score bucket [0.40,0.45) 3 wins -> MCE=58.67pp (not midpoint 57.5pp)",
                 cn == 3 and abs(mce - 58.67) < 1e-2 and abs(mce - 57.5) > 0.1):
        failures.append("mce-parity")

    # (11) DRY-RUN SAFETY (t_5c238cc5 defect 3): --dry-run must NOT write the
    # status file or create/clear readiness/breach flags. Tested DB-free by
    # injecting a fake metrics source and invoking main() under --dry-run, then
    # asserting no status file / flags were created AND a pre-planted flag was
    # NOT removed (i.e. clear_flags_if_below_floor did not run). Deterministic
    # and fast (no live DB round-trip needed to prove the dry_run guard).
    import os as _os, tempfile as _tf, io as _io
    from contextlib import redirect_stdout
    _d = _tf.mkdtemp(prefix="tier1-dryrun-")
    _status = _os.path.join(_d, "status.json")
    _flags = _os.path.join(_d, "flags")
    _os.makedirs(_flags, exist_ok=True)
    # Plant a pre-existing readiness flag: dry-run must NOT remove it.
    _planted = _os.path.join(_flags, "ready_signal.flag")
    with open(_planted, "w") as _fh:
        _fh.write("signaled_at=2026-07-17T00:00:00+00:00\n"
                  "kind=READY_FOR_VALIDATION\n")
    _env = _os.environ.copy()
    _env["TIER1_GATE_STATUS_FILE"] = _status
    _env["TIER1_GATE_FLAG_DIR"] = _flags
    _env["TIER1_GATE_DRY_RUN"] = "1"
    _old_env = dict(_os.environ)
    try:
        _os.environ.clear(); _os.environ.update(_env)
        g.DRY_RUN = True
        # Inject a fake metrics source so gather_tier1_metrics never touches
        # the DB. n=127 (below floor) with a suppressed breach exercises the
        # path that, before the fix, called clear_flags_if_below_floor() +
        # write_status_file().
        fake_metrics = {"n": 127, "win_rate": 40.16, "avg_pnl": 0.4772,
                        "weighted_mce": 19.4, "epoch_start": "2026-07-05",
                        "error": None}
        def _fake_gather(run_sql_fn=None):
            return fake_metrics
        _gather = g.gather_tier1_metrics
        g.gather_tier1_metrics = _fake_gather
        _buf = _io.StringIO()
        try:
            with redirect_stdout(_buf):
                g.main()
        finally:
            g.gather_tier1_metrics = _gather
            g.DRY_RUN = False
        _no_mutation = (not _os.path.exists(_status)
                        and _os.path.exists(_planted))
        if not check("dry-run writes NO status file and does NOT clear planted flag",
                     _no_mutation):
            failures.append("dry-run-safe")
    finally:
        _os.environ.clear(); _os.environ.update(_old_env)

    # bucket_key monotonic sanity.
    if not check("bucket_key(0.0)=[0.00, 0.05)",
                 g.bucket_key(0.0) == "[0.00, 0.05)"):
        failures.append("bucket-key")

    if failures:
        print(f"\nTEST FAILED: {len(failures)} assertion(s): {failures}")
        return 1
    print("\nTEST PASSED: all tier1_sample_gate gate assertions hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
