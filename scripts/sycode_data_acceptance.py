#!/usr/bin/env python3
"""
sycode_data_acceptance.py — shared, read-only acceptance scorecard for the sycode-trading data estate.

WHY THIS EXISTS
---------------
On 2026-08-06 two seats (fable, grok-quant-seat) audited the same estate within hours of each other and
produced contradictory status. Neither was lying: grok was APPLYING SQL fix batches while fable was
MEASURING, so every number went stale inside two hours. Worse, the 2026-08-05 kanban plan asserted
"P1 FRESHNESS LOOPS: ALL DONE" while 7 of 11 acceptance criteria were failing live.

The fix is structural: status is a MEASUREMENT, not a claim in a note. Both seats run this script and
quote its output. Nobody writes a status number into a document without a run of this behind it.

USAGE
    python3 ~/.hermes/scripts/sycode_data_acceptance.py            # markdown table (default)
    python3 ~/.hermes/scripts/sycode_data_acceptance.py --json     # machine readable
    python3 ~/.hermes/scripts/sycode_data_acceptance.py --only P0  # filter by phase prefix

EXIT CODES
    0  all criteria pass
    1  one or more P0 criteria fail
    2  only non-P0 criteria fail
    3  harness error (could not measure) -- NOT the same as a pass. A criterion that cannot be
       measured is reported UNKNOWN and never counted as green.

SAFETY
    Strictly read-only. SELECT only. No DDL, no DML. Sets its own statement_timeout because the
    `postgres` role default is 60s (verified 2026-08-06 via pg_db_role_setting) which is too short
    for several of these predicates.
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone

CONTAINER = "sycodetrading-supabase-db"
STMT_TIMEOUT = "240s"

# Advisory cross-seat mutation lock. Any seat about to mutate live data (SQL fix batch, backfill,
# cron rewire, producer restart) writes this file first; it is removed when the mutation completes.
# This scorecard does NOT enforce anything -- it reports. The point is that a measurement taken
# while another seat is mid-backfill is provenance, not status, and must never be quoted as status.
# This is the exact failure of 2026-08-06: fable measured attribution at 1.49% and clean-labels at
# 83% while grok-quant-seat was mid-backfill; both figures were obsolete within two hours.
MUTATION_LOCK = "/home/frank/.hermes/locks/sycode-data-mutation.lock"


def read_mutation_lock():
    """Return the active mutation record, or None. Never raises."""
    try:
        with open(MUTATION_LOCK) as fh:
            raw = fh.read().strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw[:300]}
    except FileNotFoundError:
        return None
    except OSError as e:
        return {"raw": f"lock unreadable: {e}"}


def q(sql: str, timeout: int = 300):
    """Run a read-only SELECT. Returns (value, error). Never raises."""
    if any(w in sql.upper() for w in (" INSERT ", " UPDATE ", " DELETE ", " DROP ", " ALTER ",
                                      " CREATE ", " TRUNCATE ", " VACUUM ", " REINDEX ", " GRANT ")):
        return None, "refused: statement is not read-only"
    cmd = ["docker", "exec", CONTAINER, "psql", "-U", "postgres", "-d", "postgres", "-At",
           "-c", f"SET statement_timeout='{STMT_TIMEOUT}'; {sql}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"harness timeout after {timeout}s"
    if r.returncode != 0:
        return None, (r.stderr or "").strip()[:300]
    lines = [l for l in r.stdout.strip().splitlines() if l.strip() and l.strip() != "SET"]
    return (lines[-1].strip() if lines else None), None


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def hours_since(ts_text):
    """Age in hours from a postgres timestamp string. None if unparseable."""
    if not ts_text or ts_text in ("NEVER", ""):
        return None
    s = ts_text.split("+")[0].split(".")[0].strip()
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, f).replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
        except ValueError:
            continue
    return None


# Each criterion: (id, name, target, sql, evaluator)
# evaluator(raw) -> (ok: bool|None, measured: str).  ok=None means UNKNOWN (never counted green).
CRITERIA = [
    ("P0.1", "clean-label coverage, settled 25h-7d", ">=99%",
     """with s as (select clean_outcome_binary_24h is not null as lab from signal_journeys
        where created_at > now()-interval '7 days' and created_at < now()-interval '25 hours')
        select round(100.0*count(*) filter (where lab)/nullif(count(*),0),2)::text||'|'||count(*)::text from s""",
     lambda r: _pct_ge(r, 99.0)),

    # CONTINUITY criterion. Must sit JUST PAST the 24h label horizon: a journey younger than 24h
    # cannot carry a 24h outcome, so a "last 24h" window would read ~0% forever and be a broken
    # check, not a broken pipeline. 25-49h is the youngest cohort that SHOULD be fully labeled if
    # the labeler runs continuously. This is the criterion that distinguishes a live producer from
    # a historical backfill -- the all-time figure (P0.1) cannot.
    ("P0.1b", "clean-label coverage, 25-49h cohort (CONTINUITY, not backfill)", ">=95%",
     """with s as (select clean_outcome_binary_24h is not null as lab from signal_journeys
        where created_at > now()-interval '49 hours' and created_at < now()-interval '25 hours')
        select round(100.0*count(*) filter (where lab)/nullif(count(*),0),2)::text||'|'||count(*)::text from s""",
     lambda r: _pct_ge(r, 95.0)),

    ("P0.2", "ml_retraining_events completed in last 24h", ">=1",
     "select count(*)::text from ml_retraining_events where status='completed' and created_at>now()-interval '24 hours'",
     lambda r: (num(r) is not None and num(r) >= 1, f"{r} completed")),

    ("P0.3", "filter_correct fill, LAST 24h cohort", ">=50%",
     """select round(100.0*count(*) filter (where filter_correct is not null)/nullif(count(*),0),2)::text||'|'||count(*)::text
        from filter_attribution_facts where created_at>now()-interval '24 hours'""",
     lambda r: _pct_ge(r, 50.0)),

    ("P0.3b", "filter_correct fill, all-time (backfill health)", ">=50%",
     """select round(100.0*count(*) filter (where filter_correct is not null)/nullif(count(*),0),2)::text||'|'||count(*)::text
        from filter_attribution_facts""",
     lambda r: _pct_ge(r, 50.0)),

    ("P0.4", "confidence_calibration_stats freshness", "<7d",
     "select coalesce(max(created_at)::text,'NEVER') from confidence_calibration_stats",
     lambda r: _age_lt(r, 24 * 7)),

    ("P1.1", "signal_fingerprints lag vs now", "<2h",
     "select coalesce(max(created_at)::text,'NEVER') from signal_fingerprints",
     lambda r: _age_lt(r, 2)),

    ("P1.2", "r_multiple_labels freshness", "<48h",
     "select coalesce(max(computed_at)::text,'NEVER') from r_multiple_labels",
     lambda r: _age_lt(r, 48)),

    ("P1.2b", "r_multiple_labels 7d throughput", ">28000",
     "select count(*)::text from r_multiple_labels where computed_at>now()-interval '7 days'",
     lambda r: (num(r) is not None and num(r) > 28000, f"{r} rows/7d")),

    ("P1.3", "bandit_arms last_updated", "<24h",
     "select coalesce(max(last_updated)::text,'NEVER') from bandit_arms",
     lambda r: _age_lt(r, 24)),

    ("P1.4", "sweet_spot_calibration freshness", "<30d",
     "select coalesce(max(last_calibrated)::text,'NEVER') from sweet_spot_calibration",
     lambda r: _age_lt(r, 24 * 30)),

    ("P2.1", "tick universe, SUSTAINED symbols (>=720/1440 min in 24h)", ">=50",
     """select count(*)::text from (select exchange,symbol from tick_trades
        where created_at>now()-interval '24 hours' group by 1,2
        having count(distinct date_trunc('minute',created_at))>=720) x""",
     lambda r: (num(r) is not None and num(r) >= 50, f"{r} sustained symbols")),

    ("P3.1", "liquidation_events rate", ">=24 rows/24h",
     "select count(*)::text from liquidation_events where created_at>now()-interval '24 hours'",
     lambda r: (num(r) is not None and num(r) >= 24, f"{r} rows/24h")),

    ("P5.1", "pending_writes backlog", "<=10000",
     "select count(*)::text from pending_writes",
     lambda r: (num(r) is not None and num(r) <= 10000, f"{int(num(r)):,} rows" if num(r) is not None else str(r))),

    # --- integrity criteria added from the 2026-08-06 estate audit (register GAP-/PERF- ids) ---
    ("X1", "execution_events writer alive (GAP: dead since 2026-07-29)", "<24h",
     "select coalesce(max(created_at)::text,'NEVER') from execution_events",
     lambda r: _age_lt(r, 24)),

    ("X2", "finalized_outcomes writer alive", "<24h",
     "select coalesce(max(created_at)::text,'NEVER') from finalized_outcomes",
     lambda r: _age_lt(r, 24)),

    ("X3", "signal_journeys genesis continuity (no multi-hour blackout)", "<2h",
     "select coalesce(max(created_at)::text,'NEVER') from signal_journeys",
     lambda r: _age_lt(r, 2)),

    # Measured on the REJECT subset specifically. Across ALL rows the constant dilutes below 100%
    # and the check passes while the defect is untouched -- the register finding is that REJECTED
    # rows carry decision_reason='HOLD' 100% of the time, i.e. no rejection is attributable.
    ("X4", "decision_snapshots: REJECTs carry a real reason (not constant 'HOLD')", "<90% constant",
     """select round(100.0*count(*) filter (where decision_reason='HOLD')/nullif(count(*),0),2)::text||'|'||count(*)::text
        from decision_snapshots where created_at>now()-interval '24 hours'
        and decision->>'approved' = 'false'""",
     lambda r: _pct_le(r, 90.0)),

    ("X5", "candles 1m grid alignment (off-grid rows)", "0 off-grid in 24h",
     """select count(*)::text from candles where timeframe='1m'
        and timestamp > now()-interval '24 hours'
        and extract(epoch from timestamp)::numeric % 60 <> 0""",
     lambda r: (num(r) is not None and num(r) == 0, f"{r} off-grid rows")),
]


def _pct_ge(raw, thr):
    if raw is None:
        return None, "unmeasured"
    parts = raw.split("|")
    p = num(parts[0])
    n = parts[1] if len(parts) > 1 else "?"
    if p is None:
        return None, f"unmeasured (n={n})"
    return p >= thr, f"{p}% (n={n})"


def _pct_le(raw, thr):
    if raw is None:
        return None, "unmeasured"
    parts = raw.split("|")
    p = num(parts[0])
    n = parts[1] if len(parts) > 1 else "?"
    if p is None:
        return None, f"unmeasured (n={n})"
    return p <= thr, f"{p}% constant (n={n})"


def _age_lt(raw, hrs):
    if raw is None:
        return None, "unmeasured"
    if raw == "NEVER":
        return False, "NEVER written"
    a = hours_since(raw)
    if a is None:
        return None, f"unparseable: {raw}"
    return a < hrs, f"{a:.1f}h ago ({raw[:19]})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", default=None, help="filter by id prefix, e.g. P0")
    args = ap.parse_args()

    rows, harness_err = [], False
    for cid, name, target, sql, ev in CRITERIA:
        if args.only and not cid.startswith(args.only):
            continue
        raw, err = q(sql)
        if err:
            harness_err = True
            rows.append(dict(id=cid, name=name, target=target, measured=f"ERROR: {err}",
                             verdict="UNKNOWN", ok=None))
            continue
        ok, measured = ev(raw)
        rows.append(dict(id=cid, name=name, target=target, measured=measured,
                         verdict=("PASS" if ok else ("FAIL" if ok is False else "UNKNOWN")), ok=ok))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lock = read_mutation_lock()
    if args.json:
        print(json.dumps(dict(measured_at=stamp, mutation_in_progress=lock, rows=rows), indent=1))
    else:
        if lock:
            who = lock.get("seat", "unknown seat")
            what = lock.get("what", lock.get("raw", "unspecified"))
            since = lock.get("started", "?")
            print("> [!caution] MEASURED DURING AN ACTIVE MUTATION")
            print(f"> `{who}` is mutating live data since {since}: {what}")
            print("> These numbers are PROVENANCE, not status. Do not quote them, do not open or close")
            print("> a card on them, and re-run once the lock clears.\n")
        npass = sum(1 for r in rows if r["ok"] is True)
        nfail = sum(1 for r in rows if r["ok"] is False)
        nunk = sum(1 for r in rows if r["ok"] is None)
        print(f"# Sycode data acceptance scorecard — {stamp}")
        print(f"\n**{npass} PASS · {nfail} FAIL · {nunk} UNKNOWN**  "
              f"(UNKNOWN is never green — an unmeasurable criterion is a broken criterion)\n")
        print("| id | criterion | target | measured | verdict |")
        print("|---|---|---|---|---|")
        for r in rows:
            badge = {"PASS": "PASS", "FAIL": "**FAIL**", "UNKNOWN": "_UNKNOWN_"}[r["verdict"]]
            print(f"| {r['id']} | {r['name']} | {r['target']} | {r['measured']} | {badge} |")
        print("\n> Re-run before quoting any of these numbers. On 2026-08-06 the clean-label figure moved")
        print("> from 0% to 99.4% on the same cohort within two hours because another seat was applying")
        print("> fix batches mid-audit. A number in a note is provenance; only a fresh run is status.")

    if harness_err:
        return 3
    if any(r["ok"] is False and r["id"].startswith(("P0", "X")) for r in rows):
        return 1
    if any(r["ok"] is False for r in rows):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
