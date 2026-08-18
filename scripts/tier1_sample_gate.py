#!/usr/bin/env python3
"""
tier1_sample_gate.py
=====================
Tier-1 sample-accumulation monitor + alert gate for the Fusion Engine
Calibration pipeline (sycode-trading, paper-only).

WHY THIS EXISTS
---------------
Per [[operations/runbooks/fusion-calibration-alert-runbook.md]] and the
t_5c6b69fb task, we need an EXPLICIT, verifiable gate that:
  * tracks Tier-1 clean unique realized-exit journeys count (n); and
  * emits a 'READY_FOR_VALIDATION' signal when n >= 300; and
  * SUPPRESSES confident MCE-breach alerts while n < 300 (any MCE breach on a
    sub-floor sample is routed to INVESTIGATE, never to a confident BREACH).

This is the standing nervous-system monitor that closes the gap:
  * calibration_watchdog.py emits SAMPLE_ACCUMULATING / INVESTIGATE / BREACH
    but has NO explicit READY_FOR_VALIDATION signal;
  * fusion_recalibration_hold_monitor.py (working tree, untracked) is NOT wired
    to any cron, so no live monitor currently emits the n>=300 signal. This
    script is the canonical, wired gate.

WHAT IT DOES (read-only, paper-only, no recalibration)
-----------------------------------------------------
1. Recomputes the Tier-1 realized-exit clean sample (n) and the sample-weighted
   MCE exactly per fusion_calibration_report_v2.py Section 2 math, bounded to
   the certified clean epoch (data_epoch_registry 'clean-candidate-599f58e7e')
   and a rolling 30-day trailing window on outcome resolution time. Synthetic
   Tier-2 candle-replay rows are EXCLUDED.
2. gate_decision(n, mce, win_rate, avg_pnl) returns exactly one status:
     - THIN_SAMPLE          n < MIN_CLEAN_N (100): no alert, status file only
     - INVESTIGATE         100 <= n < 300 with a genuine breach (SUPPRESSED BREACH)
     - SAMPLE_ACCUMULATING n < 300 with no breach: tracker only, no alert
     - READY_FOR_VALIDATION n >= 300 AND no breach: explicit validation-ready
     - BREACH              n >= 300 AND a genuine breach: confident alert
   => Suppression guarantee: while n < 300, a genuine MCE breach is NEVER
      returned as BREACH; it is INVESTIGATE.
3. Emits a durable signal (stdout line + signal file, idempotent per crossing)
   for READY_FOR_VALIDATION and BREACH. All other statuses stay silent on stdout
   (no_agent cron delivers only non-empty stdout) but are recorded to a status
   file for observability.
4. NEVER changes the 15pp threshold, NEVER recalibrates the engine, NEVER
   touches live trading / credentials / deploys / trade_intents.

EXIT / OUTPUT CONTRACT (no_agent cron)
--------------------------------------
- Exits 0 always (so the cron never records "script failed").
- --dry-run: compute + print the decision WITHOUT writing any flag or signal.
- --self-test: run the DB-free unit assertions; exit 0 on pass, 1 on fail.
- Fail-open: if the DB is unreachable, print a THIN_SAMPLE diagnostic and exit 0
  (never wedges the cron). BUT (t_817e4ded hardening) the outage is made
  operator-visible: a distinct DB_BLIND status is written to the status file and
  a non-empty WARN is printed to stdout so the no_agent cron delivers it. Unlike
  the prior silent fail-open, a persistent DB outage no longer leaves the monitor
  blind with zero signal (mirrors calibration_watchdog.py CRITICAL SQL-failure).

Faithful replication of the report's math:
  - clean_pred:  contaminated=false AND is_counterfactual=false
                 AND label_source NOT IN ('interim_1h','interim_4h')
                 AND abs(pnl_percent) <= 1000
  - epoch_pred:  resolved_at >= epoch_start (data_epoch_registry
                 'clean-candidate-599f58e7e')
  - window_pred: resolved_at >= now() - interval '30 days'
  - resolved_at = COALESCE(d.finalized_at, d.decided_at, d.created_at)
  - Sample-weighted MCE = sum_bucket( |actual_WR - expected_WR| * bucket_n / clean_n )
      bucket width = 0.05; expected_WR = avg(conviction_score) * 100
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys

# --- Tunables ---------------------------------------------------------------
MIN_CLEAN_N = int(os.environ.get("TIER1_GATE_MIN_N", "100"))
VALIDATION_FLOOR_N = int(os.environ.get("TIER1_GATE_FLOOR_N", "300"))
WIN_RATE_THRESHOLD = float(os.environ.get("TIER1_GATE_WR_THRESH", "40.0"))
PNL_THRESHOLD = float(os.environ.get("TIER1_GATE_PNL_THRESH", "0.0"))
MCE_THRESHOLD_PP = float(os.environ.get("TIER1_GATE_MCE_THRESH", "15.0"))
WINDOW_DAYS = int(os.environ.get("TIER1_GATE_WINDOW_DAYS", "30"))
EPOCH_NAME = os.environ.get("TIER1_GATE_EPOCH_NAME", "clean-candidate-599f58e7e")
FLAG_DIR = os.environ.get("TIER1_GATE_FLAG_DIR", "/tmp/tier1-gate")
DRY_RUN = os.environ.get("TIER1_GATE_DRY_RUN", "0").lower() in ("1", "true", "yes")
STATUS_FILE = os.environ.get(
    "TIER1_GATE_STATUS_FILE",
    "/home/frank/.hermes/var/tier1_gate_status.json",
)

# --- DB connection (mirrors fusion_calibration_report_v2.py) ----------------
PGHOST = os.environ.get("PGHOST", "localhost")
PGPORT = os.environ.get("PGPORT", "5432")
PGUSER = os.environ.get("PGUSER", "postgres")
PGDB = os.environ.get("PGDB", "postgres")
PGPASS = os.environ.get("PGPASS", os.environ.get("PGPASSWORD", "postgres"))

INTERIM_LANES = ("interim_1h", "interim_4h")
PSQL_CMD = ["psql", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, "-d", PGDB,
            "-X", "-A", "-F", "|", "--pset", "footer=off"]


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable, no DB / IO)
# ---------------------------------------------------------------------------
def bucket_key(score: float) -> str:
    """Faithful bucket label (mirrors fusion_calibration_report_v2.py)."""
    start = int(score * 20) / 20.0
    end = start + 0.05
    if end >= 1.0:
        return f"[{start:.2f}, 1.00]"
    return f"[{start:.2f}, {end:.2f})"


def is_true(v) -> bool:
    return str(v).lower() in ("true", "t", "1", "yes")


def compute_sample_weighted_mce(rows: list[dict]) -> tuple[float, int]:
    """Faithful sample-weighted MCE over the deduped Tier-1 rows.

    Returns (mce_pp, clean_n). The per-bucket *expected* win rate is the
    AVERAGE conviction score in that bucket (``score_sum / total``), EXACTLY
    matching fusion_calibration_report_v2.py Section 2 (the canonical living
    report). A prior midpoint method (expected = bucket midpoint) diverged from
    the report by ~0.13pp at n=127 (gate 19.53pp vs report 19.40pp); this method
    reproduces 19.40pp (t_5c238cc5 parity fix).
    """
    clean_n = len(rows)
    if clean_n == 0:
        return 0.0, 0
    buckets: dict[str, dict] = {}
    for r in rows:
        try:
            score = float(r.get("conviction_score") or 0)
        except (ValueError, TypeError):
            score = 0.0
        key = bucket_key(score)
        b = buckets.setdefault(
            key, {"total": 0, "wins": 0, "score_sum": 0.0})
        b["total"] += 1
        if is_true(r.get("is_win", "")):
            b["wins"] += 1
        b["score_sum"] += score

    weighted_mce = 0.0
    for key, b in buckets.items():
        b_wr = (b["wins"] / b["total"] * 100) if b["total"] else 0.0
        avg_score = (b["score_sum"] / b["total"]) if b["total"] else 0.0
        expected_wr = avg_score * 100
        error = abs(b_wr - expected_wr)
        weighted_mce += error * (b["total"] / clean_n)
    return round(weighted_mce, 2), clean_n


def gate_decision(metrics: dict) -> dict:
    """Pure, side-effect-free classifier. Encodes the suppression guarantee.

    Returns a dict with keys: kind, breach (bool), suppressed_breach (bool),
    n, weighted_mce, win_rate, avg_pnl, lines (list of stdout lines when the
    status should be delivered).
    """
    n = metrics.get("n")
    win_rate = metrics.get("win_rate")
    avg_pnl = metrics.get("avg_pnl")
    weighted_mce = metrics.get("weighted_mce")

    def fmt(v, suffix=""):
        return f"{v:.1f}{suffix}" if isinstance(v, (int, float)) else "--"

    breaches = []
    if win_rate is not None and win_rate < WIN_RATE_THRESHOLD:
        breaches.append(
            f"Win Rate: {win_rate:.1f}% (Threshold: < {WIN_RATE_THRESHOLD:.1f}%)")
    if avg_pnl is not None and avg_pnl < PNL_THRESHOLD:
        breaches.append(
            f"Average PnL%: {avg_pnl:.2f}% (Threshold: < {PNL_THRESHOLD:.2f}%)")
    if weighted_mce is not None and weighted_mce > MCE_THRESHOLD_PP:
        breaches.append(
            f"Sample-weighted MCE: {weighted_mce:.1f}pp "
            f"(Threshold: > {MCE_THRESHOLD_PP:.1f}pp)")

    breach = bool(breaches)
    mce_breach = any("MCE" in b for b in breaches)

    # THIN_SAMPLE: below the watchdog's own stability floor. No alert.
    if n is None or n < MIN_CLEAN_N:
        return {
            "kind": "THIN_SAMPLE",
            "breach": breach,
            "suppressed_breach": False,
            "n": n, "weighted_mce": weighted_mce,
            "win_rate": win_rate, "avg_pnl": avg_pnl,
            "lines": [],
        }

    # VALIDATION FLOOR MET (n >= 300).
    if n >= VALIDATION_FLOOR_N:
        if breach:
            lines = [
                "🚨 FUSION CALIBRATION TIER-1 GATE — BREACH (confident sample) 🚨",
                f"Tier-1 realized-exit n={n} >= {VALIDATION_FLOOR_N} (confident).",
                "Genuine breach on a validated sample:",
            ] + [f"- {b}" for b in breaches] + [
                "",
                "Response Runbook: [[operations/runbooks/fusion-calibration-alert-runbook.md]]",
            ]
            return {
                "kind": "BREACH", "breach": True, "suppressed_breach": False,
                "n": n, "weighted_mce": weighted_mce,
                "win_rate": win_rate, "avg_pnl": avg_pnl, "lines": lines,
            }
        lines = [
            "🟢 FUSION CALIBRATION TIER-1 GATE — READY_FOR_VALIDATION 🟢",
            f"Tier-1 realized-exit clean unique journeys n={n} >= "
            f"{VALIDATION_FLOOR_N}.",
            "Sample is now large enough to validate a calibration edge. "
            "Recalibration remains Frank/PM-gated (t_b4c824c7); this gate does "
            "NOT auto-recalibrate.",
            f"(win_rate={fmt(win_rate, '%')}, avg_pnl={fmt(avg_pnl, '%')}, "
            f"sample_weighted_mce={fmt(weighted_mce, 'pp')})",
        ]
        return {
            "kind": "READY_FOR_VALIDATION", "breach": False,
            "suppressed_breach": False,
            "n": n, "weighted_mce": weighted_mce,
            "win_rate": win_rate, "avg_pnl": avg_pnl, "lines": lines,
        }

    # BELOW FLOOR (100 <= n < 300).
    if breach:
        # SUPPRESS the confident BREACH; route to INVESTIGATE.
        lines = [
            "🔎 FUSION CALIBRATION TIER-1 GATE — INVESTIGATE (breach suppressed) 🔎",
            f"Genuine breach on a statistically-thin Tier-1 sample (n={n} < "
            f"{VALIDATION_FLOOR_N}).",
            "Confident BREACH alert SUPPRESSED (VALIDATED_EDGE_STATUS: "
            "INSUFFICIENT_SAMPLE). Route to INVESTIGATE, accumulate more "
            "Tier-1 outcomes. Breached metric(s):",
        ] + [f"- {b}" for b in breaches]
        if mce_breach:
            lines.append(
                "MCE breach is genuine but NOT conclusive in magnitude at this "
                "n (see t_cab6f5c1 bootstrap: 95% CI straddles 15pp).")
        return {
            "kind": "INVESTIGATE", "breach": True, "suppressed_breach": True,
            "n": n, "weighted_mce": weighted_mce,
            "win_rate": win_rate, "avg_pnl": avg_pnl, "lines": lines,
        }

    # Below floor, no breach: pure accumulation tracker.
    return {
        "kind": "SAMPLE_ACCUMULATING", "breach": False,
        "suppressed_breach": False,
        "n": n, "weighted_mce": weighted_mce,
        "win_rate": win_rate, "avg_pnl": avg_pnl, "lines": [],
    }


# ---------------------------------------------------------------------------
# DB-backed metrics gathering (injected for testability)
# ---------------------------------------------------------------------------
def run_sql(sql: str, timeout: int = 120) -> list[dict]:
    env = os.environ.copy()
    env["PGPASSWORD"] = PGPASS
    try:
        res = subprocess.run(PSQL_CMD + ["-c", sql], capture_output=True,
                             text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        print("  [ERROR] SQL timed out", file=sys.stderr)
        return []
    if res.returncode != 0:
        print(f"  [ERROR] SQL failed: {res.stderr.strip() or res.returncode}",
              file=sys.stderr)
        return []
    raw = res.stdout.strip()
    if not raw:
        return []
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    if len(lines) < 2:
        return []
    cols = [c.strip() for c in lines[0].split("|")]
    rows = []
    for line in lines[1:]:
        vals = [v.strip() for v in line.split("|")]
        if len(vals) == len(cols):
            rows.append(dict(zip(cols, vals)))
    return rows


def get_clean_epoch_start() -> str | None:
    row = run_sql(
        f"SELECT starts_at::text AS s FROM data_epoch_registry "
        f"WHERE name = '{EPOCH_NAME}' LIMIT 1;", timeout=30)
    if not row or not row[0].get("s"):
        return None
    return row[0]["s"]


def gather_tier1_metrics(run_sql_fn=run_sql) -> dict:
    """Read-only recompute of Tier-1 realized-exit n, MCE, win rate, avg PnL.

    Returns a metrics dict; on any DB failure returns {"error": ...} with n=None.
    """
    try:
        epoch_start = run_sql_fn(
            f"SELECT starts_at::text AS s FROM data_epoch_registry "
            f"WHERE name = '{EPOCH_NAME}' LIMIT 1;", timeout=30)
        epoch_start = epoch_start[0]["s"] if epoch_start and epoch_start[0].get("s") else None
        if not epoch_start:
            return {"error": f"clean epoch '{EPOCH_NAME}' not found",
                    "n": None, "weighted_mce": None,
                    "win_rate": None, "avg_pnl": None}
    except Exception as e:  # noqa: BLE001
        return {"error": f"epoch lookup failed: {e}", "n": None,
                "weighted_mce": None, "win_rate": None, "avg_pnl": None}

    resolved_at = "COALESCE(d.finalized_at, d.decided_at, d.created_at)"
    window_pred = f"{resolved_at} >= now() - interval '{WINDOW_DAYS} days'"
    clean_pred = (
        "d.contaminated = false "
        "AND d.is_counterfactual = false "
        "AND d.label_source NOT IN ('interim_1h', 'interim_4h') "
        "AND abs(d.pnl_percent::numeric) <= 1000"
    )
    epoch_pred = f"{resolved_at} >= '{epoch_start}'"

    dedup_sql = f"""
        SELECT DISTINCT ON (sj.id)
            sj.id AS journey_id,
            COALESCE(ts.conviction_score::numeric,
                     sj.composite_confidence_score::numeric) AS conviction_score,
            d.outcome_class,
            d.is_win,
            d.pnl_percent::numeric AS pnl_pct
        FROM signal_journeys sj
        JOIN decision_outcomes d ON d.journey_id = sj.id
        LEFT JOIN trade_setups ts ON ts.signal_id = sj.id::text
        WHERE d.outcome_class IN ('WIN', 'LOSS')
          AND {clean_pred}
          AND {window_pred}
          AND {epoch_pred}
        ORDER BY sj.id,
                 d.is_final DESC,
                 {resolved_at} DESC,
                 (ts.signal_id IS NOT NULL) DESC,
                 ts.generated_at DESC;
    """
    try:
        rows = run_sql_fn(dedup_sql, timeout=120)
    except Exception as e:  # noqa: BLE001
        return {"error": f"dedup query failed: {e}", "n": None,
                "weighted_mce": None, "win_rate": None, "avg_pnl": None}

    clean_n = len(rows)
    wins = sum(1 for r in rows if is_true(r.get("is_win", "")))
    wr = (wins / clean_n * 100) if clean_n else None
    avg_pnl = (sum(float(r.get("pnl_pct") or 0) for r in rows) / clean_n) \
        if clean_n else None
    mce_pp, _ = compute_sample_weighted_mce(rows)
    return {
        "n": clean_n,
        "win_rate": round(wr, 2) if wr is not None else None,
        "avg_pnl": round(avg_pnl, 4) if avg_pnl is not None else None,
        "weighted_mce": mce_pp,
        "epoch_start": epoch_start,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Side-effects: idempotent signals + status file
# ---------------------------------------------------------------------------
def write_status_file(decision: dict, dry_run: bool) -> None:
    try:
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        payload = {
            "kind": decision["kind"],
            "n": decision["n"],
            "weighted_mce": decision["weighted_mce"],
            "win_rate": decision["win_rate"],
            "avg_pnl": decision["avg_pnl"],
            "breach": decision["breach"],
            "suppressed_breach": decision["suppressed_breach"],
            "error": decision.get("error"),
            "at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "dry_run": dry_run,
        }
        with open(STATUS_FILE, "w") as fh:
            json.dump(payload, fh, indent=2)
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] could not write status file: {e}", file=sys.stderr)


def maybe_emit_signal(decision: dict, dry_run: bool) -> None:
    """Emit a once-per-crossing durable signal for READY_FOR_VALIDATION /
    BREACH. Idempotent via flag files; flags clear when n drops below floor so
    a future re-crossing re-fires."""
    kind = decision["kind"]
    if kind not in ("READY_FOR_VALIDATION", "BREACH"):
        return

    n = decision["n"]
    ready_flag = os.path.join(FLAG_DIR, "ready_signal.flag")
    breach_flag = os.path.join(FLAG_DIR, "breach_signal.flag")

    if kind == "READY_FOR_VALIDATION":
        flag = ready_flag
    else:
        flag = breach_flag

    already = os.path.exists(flag)
    if not dry_run and not already:
        try:
            os.makedirs(FLAG_DIR, exist_ok=True)
            with open(flag, "w") as fh:
                fh.write(
                    f"signaled_at={_dt.datetime.now(_dt.timezone.utc).isoformat()}\n"
                    f"kind={kind}\n"
                    f"tier1_n={n}\n"
                    f"sample_weighted_mce_pp={decision['weighted_mce']}\n"
                )
        except Exception as e:  # noqa: BLE001
            print(f"  [WARN] could not write signal flag: {e}", file=sys.stderr)

    # Print (carried by deliver target when configured).
    for line in decision["lines"]:
        print(line)

    # Durable card on the sycode-trading board (idempotent: only on first
    # crossing, i.e. when the flag was just created). Mirrors the watchdog's
    # alert-card pattern; never opens a recalibration path.
    if not dry_run and not already:
        if kind == "READY_FOR_VALIDATION":
            title = (f"READY_FOR_VALIDATION: Fusion Tier-1 sample floor reached "
                     f"(n={n} >= {VALIDATION_FLOOR_N})")
            body = (
                f"The Tier-1 realized-exit clean unique journey sample reached "
                f"the validation floor: **n={n} >= {VALIDATION_FLOOR_N}** "
                f"(win_rate={decision['win_rate']}%, "
                f"avg_pnl={decision['avg_pnl']}%, "
                f"sample_weighted_mce={decision['weighted_mce']}pp).\n\n"
                f"This is a SAMPLE-READINESS signal only. Recalibration remains "
                f"a Frank/PM-gated governed change (t_b4c824c7). The gate "
                f"(tier1_sample_gate.py) does NOT auto-recalibrate.\n\n"
                f"Response Runbook: "
                f"[[operations/runbooks/fusion-calibration-alert-runbook.md]]"
            )
            create_kanban_card(title, body, id_key=f"tier1-gate-ready-{n}")
        else:
            title = (f"BREACH: Fusion Tier-1 confident sample "
                     f"(n={n}, MCE={decision['weighted_mce']}pp)")
            body = (
                f"Genuine calibration breach on a confident Tier-1 sample "
                f"(n={n} >= {VALIDATION_FLOOR_N}):\n\n"
                + "\n".join(f"- {b}" for b in decision.get("lines", [])[3:])
                + f"\n\nResponse Runbook: "
                  f"[[operations/runbooks/fusion-calibration-alert-runbook.md]]"
            )
            create_kanban_card(title, body, id_key=f"tier1-gate-breach-{n}")

    if dry_run:
        print(f"  [DRY-RUN] would set {flag} + create kanban card "
              f"(signal not persisted).")


HERMES_CLI = os.environ.get("HERMES_CLI", "/home/frank/.local/bin/hermes")
KANBAN_BOARD = os.environ.get("TIER1_GATE_BOARD", "sycode-trading")


def create_kanban_card(title: str, body: str, id_key: str) -> None:
    """Open an automated kanban card via the Hermes CLI (idempotency via key)."""
    try:
        res = subprocess.run([
            HERMES_CLI, "kanban", "create",
            "--triage",
            "--assignee", "sycode-trading-pm",
            "--priority", "70",
            "--idempotency-key", id_key,
            "--body", body,
            title,
        ], capture_output=True, text=True, timeout=60)
        print(f"[tier1-gate] kanban card rc={res.returncode} "
              f"stderr={res.stderr.strip()[:160]}")
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] failed to create kanban card: {e}", file=sys.stderr)


def clear_flags_if_below_floor(n: int | None) -> None:
    """Reset idempotency flags when the sample drops below the validation floor,
    so a future re-crossing re-fires the signal."""
    if n is not None and n >= VALIDATION_FLOOR_N:
        return
    for flag in ("ready_signal.flag", "breach_signal.flag"):
        path = os.path.join(FLAG_DIR, flag)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Unit assertions (mirrors test_tier1_gate.py)
# ---------------------------------------------------------------------------
def self_test() -> int:
    failures = []

    def check(name, cond):
        if cond:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name}")
            failures.append(name)

    # (1) Suppression guarantee: genuine MCE breach below floor -> INVESTIGATE.
    d = gate_decision({"n": 126, "win_rate": 40.5, "avg_pnl": 0.48,
                       "weighted_mce": 19.1})
    check("n=126, MCE=19.1pp -> INVESTIGATE (not BREACH)",
          d["kind"] == "INVESTIGATE" and d["suppressed_breach"] is True)

    # (2) Even a massive MCE breach below floor is suppressed (never BREACH).
    d = gate_decision({"n": 299, "win_rate": 40.0, "avg_pnl": 0.0,
                       "weighted_mce": 999.0})
    check("n=299, MCE=999pp -> INVESTIGATE (suppressed)",
          d["kind"] == "INVESTIGATE" and d["suppressed_breach"] is True)

    # (3) Floor met + no breach -> READY_FOR_VALIDATION.
    d = gate_decision({"n": 300, "win_rate": 55.0, "avg_pnl": 0.7,
                       "weighted_mce": 8.0})
    check("n=300, healthy -> READY_FOR_VALIDATION",
          d["kind"] == "READY_FOR_VALIDATION" and d["breach"] is False)

    # (4) Floor met (just over) + MCE breach -> BREACH.
    d = gate_decision({"n": 301, "win_rate": 48.0, "avg_pnl": 0.2,
                       "weighted_mce": 20.0})
    check("n=301, MCE=20pp -> BREACH (confident)",
          d["kind"] == "BREACH" and d["breach"] is True)

    # (5) Below floor, no breach -> SAMPLE_ACCUMULATING.
    d = gate_decision({"n": 260, "win_rate": 54.0, "avg_pnl": 0.6,
                       "weighted_mce": 11.0})
    check("n=260, healthy -> SAMPLE_ACCUMULATING",
          d["kind"] == "SAMPLE_ACCUMULATING")

    # (6) Below stability floor -> THIN_SAMPLE (no alert).
    d = gate_decision({"n": 42, "win_rate": 51.0, "avg_pnl": 0.2,
                       "weighted_mce": 9.0})
    check("n=42 -> THIN_SAMPLE",
          d["kind"] == "THIN_SAMPLE" and d["breach"] is False)

    # (7) Boundary: n=299 breach -> INVESTIGATE; n=300 breach -> BREACH.
    d_low = gate_decision({"n": 299, "win_rate": 48.0, "avg_pnl": 0.0,
                           "weighted_mce": 20.0})
    d_high = gate_decision({"n": 300, "win_rate": 48.0, "avg_pnl": 0.0,
                            "weighted_mce": 20.0})
    check("boundary 299 -> INVESTIGATE, 300 -> BREACH",
          d_low["kind"] == "INVESTIGATE" and d_high["kind"] == "BREACH")

    # (8) MCE math fidelity: a CALIBRATED bucket yields ~0 error.
    # Under the avg-score method expected WR = mean(conviction_score)*100.
    # Bucket [0.50,0.55) with all scores = 0.525 -> mean 0.525 -> expected
    # WR 52.5%. 21 wins / 19 losses over 40 rows = 52.5% actual -> error 0
    # -> MCE ~0 (pinned to the canonical report math, t_5c238cc5).
    rows = ([{"conviction_score": "0.525", "is_win": "t", "pnl_pct": "1.0"}
             for _ in range(21)]
            + [{"conviction_score": "0.525", "is_win": "f", "pnl_pct": "1.0"}
               for _ in range(19)])
    mce, cn = compute_sample_weighted_mce(rows)
    check("calibrated bucket [0.50,0.55) 21w/19l -> MCE ~0",
          cn == 40 and abs(mce) < 1e-6)

    # (9) MCE math: bucket [0.95,1.00] all wins -> avg_score=0.97 ->
    #     expected WR 97% vs actual 100% -> |100-97|=3.0pp (report-faithful;
    #     the old midpoint method gave 2.5pp and is now rejected, t_5c238cc5).
    rows = [{"conviction_score": "0.97", "is_win": "t", "pnl_pct": "1.0"}
            for _ in range(40)]
    mce, cn = compute_sample_weighted_mce(rows)
    check("bucket [0.95,1.00] all wins -> MCE=3.0pp (avg-score method)",
          cn == 40 and abs(mce - 3.0) < 1e-6)

    # (10) PARITY FIXTURE (t_5c238cc5): a bucket whose average conviction score
    # differs from the bucket midpoint. Midpoint would report 57.5pp; the
    # report-faithful average-score method reports 58.67pp. This assertion
    # FAILS on midpoint substitution, pinning the canonical report math.
    rows = ([{"conviction_score": "0.40", "is_win": "t", "pnl_pct": "1.0"},
             {"conviction_score": "0.40", "is_win": "t", "pnl_pct": "1.0"},
             {"conviction_score": "0.44", "is_win": "t", "pnl_pct": "1.0"}])
    mce, cn = compute_sample_weighted_mce(rows)
    check("avg-score bucket [0.40,0.45) 3 wins -> MCE=58.67pp (not midpoint 57.5pp)",
          cn == 3 and abs(mce - 58.67) < 1e-2 and abs(mce - 57.5) > 0.1)

    if failures:
        print(f"\nSELF-TEST FAILED: {len(failures)} assertion(s): {failures}")
        return 1
    print("\nSELF-TEST PASSED: all t_5c6b69fb gate assertions hold.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Tier-1 sample-accumulation gate")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute + print decision; do NOT write flags/signals")
    ap.add_argument("--self-test", action="store_true",
                    help="run DB-free unit assertions")
    ap.add_argument("--db-blindness-test", action="store_true",
                    help="force the DB-failure path and verify the DB_BLIND "
                         "escalation (status file + delivered WARN)")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    dry_run = args.dry_run or DRY_RUN
    now = _dt.datetime.now(_dt.timezone.utc)
    print(f"[tier1-gate] {now.isoformat()} starting "
          f"(floor_n={VALIDATION_FLOOR_N}, mce_threshold={MCE_THRESHOLD_PP}pp, "
          f"window={WINDOW_DAYS}d, dry_run={dry_run})")

    if args.db_blindness_test:
        # Test-only: force the DB-failure path without touching the real DB.
        import tempfile as _tf
        _tmp = os.path.join(_tf.gettempdir(), "tier1_gate_status_dbgtest.json")
        globals()["STATUS_FILE"] = _tmp

        def _fail(sql, timeout=120):
            raise RuntimeError("injected DB failure (db-blindness-test)")
        metrics = gather_tier1_metrics(run_sql_fn=_fail)
    else:
        metrics = gather_tier1_metrics(run_sql_fn=run_sql)

    if metrics.get("error"):
        # DB-BLINDNESS ESCALATION (t_817e4ded hardening):
        # Fail-open (never wedge the cron) BUT make the outage operator-visible.
        # Previously this gate failed open to *silent* THIN_SAMPLE (exit 0, no
        # delivery), leaving the monitor blind with zero signal. Now emit a
        # distinct DB_BLIND status to the status file and print a non-empty WARN
        # to stdout so the no_agent cron delivers it to the operator (mirrors
        # calibration_watchdog.py's CRITICAL SQL-failure path).
        err = metrics["error"]
        print(
            f"[tier1-gate][DB_BLIND] WARNING: Tier-1 gate cannot read the "
            f"database ({err}). The monitor is BLIND - sample-accumulation and "
            f"breach suppression are NOT being evaluated. Investigate DB "
            f"connectivity. Non-fatal WARN; the cron continues.",
            file=sys.stdout,
        )
        print(f"  [DB_BLIND] {err}", file=sys.stderr)
        write_status_file({
            "kind": "DB_BLIND", "breach": False, "suppressed_breach": False,
            "n": None, "weighted_mce": None, "win_rate": None, "avg_pnl": None,
            "error": err,
        }, dry_run)
        if args.db_blindness_test:
            try:
                with open(STATUS_FILE) as fh:
                    sd = json.load(fh)
                ok = sd.get("kind") == "DB_BLIND" and sd.get("n") is None
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"  [DB_BLIND-TEST] status read failed: {e}")
            if ok:
                print("DB_BLINDNESS-TEST PASSED: DB outage produces DB_BLIND "
                      "status + delivered WARN, exit 0.")
                return 0
            print("DB_BLINDNESS-TEST FAILED")
            return 1
        return 0

    decision = gate_decision(metrics)
    print(f"[tier1-gate] Tier-1 n={decision['n']} "
          f"(win_rate={decision['win_rate']}, "
          f"avg_pnl={decision['avg_pnl']}, "
          f"sample_weighted_mce={decision['weighted_mce']}pp) -> "
          f"{decision['kind']}"
          + (" [SUPPRESSED BREACH]" if decision["suppressed_breach"] else ""))

    # Dry-run must NOT mutate readiness/breach flags or the status store
    # (t_5c238cc5 defect 3): --dry-run computes + prints only, and never
    # clears idempotency flags or writes the status file.
    if not dry_run:
        write_status_file(decision, dry_run)
        clear_flags_if_below_floor(decision["n"])

    if decision["kind"] in ("READY_FOR_VALIDATION", "BREACH"):
        maybe_emit_signal(decision, dry_run)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:  # noqa: BLE001
        print(f"[tier1-gate] FATAL: {e}", file=sys.stderr)
        raise SystemExit(0)
