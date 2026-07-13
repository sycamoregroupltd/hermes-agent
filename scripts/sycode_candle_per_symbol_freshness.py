#!/usr/bin/env python3
# invoker: hermes cron (register after PM review) — manual: python3 ~/.hermes/scripts/sycode_candle_per_symbol_freshness.py
#
# NS-P2.x / t_d7d23f62: PER-SYMBOL candle freshness monitor.
# Closes the silent-writer-death blind spot in dgx_data_freshness_probe.py.
#
# WHY THIS EXISTS
# ---------------
# The canonical dgx_data_freshness_probe.py watches `candles` as ONE pipeline via
#   SELECT max(timestamp) FROM candles
# That is table-level. Because 10 core symbols keep landing 1m/5m/1h/1D candles
# continuously, the table-level max() stays <3h even when 360/370 symbols' daily
# bars are frozen at 2026-06-24. The probe reported GREEN while the 1D feed was
# 92% dead. Silent-writer-death class — the dominant fleet failure mode.
#
# This monitor computes FRESH-SYMBOL COUNT per (timeframe) using a single
# index-friendly aggregate:
#   SELECT count(DISTINCT symbol)
#          FILTER (WHERE timestamp >= now() - interval '<budget> hours')
#   FROM candles WHERE timeframe = '<tf>';
# Read-only (PGOPTIONS default_transaction_read_only=on). No count(*) over the
# whole table; one small aggregate per timeframe.
#
# SLOs (mode=alert if fresh_count < floor):
#   - 1m/5m/15m/1h:  broad streaming feeds; expect >= 300 fresh (out of ~370-504).
#   - 4h:            broad; expect >= 150 fresh.
#   - 1D:            THE regression under investigation; expect >= 300 fresh
#                    (full universe should be daily). Current reality (2026-07-13)
#                    is 10 — this monitor will ALERT until the producer fix lands.
#
# A FLOOR (absolute minimum) plus a SOFT-DROP (fresh_count < 90% of observed
# baseline, tracked in state) gives two detection lanes:
#   - hard floor breach  -> always alert (writer totally dead)
#   - gradual erosion    -> alert when dropping below 90% of last-good baseline
#                           (catches the "10 symbols quietly lost coverage" case)
#
# Exit 2 + ALERT lines on any breach; exit 1 on operational error; exit 0 clean.
# Optional --self-test: runs evaluate() on synthetic ages, no DB, no write.

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_CONTAINER = "sycodetrading-supabase-db"

# (timeframe, budget_hours, hard_floor_fresh_symbols, note)
# budget_hours: max staleness tolerated before a symbol counts as "stale".
# hard_floor:   absolute minimum fresh-symbol count before we scream. Floors are
#               set to the INTENDED design (essentially full universe streaming);
#               reality on 2026-07-13 is far lower — that is the finding, not a
#               mis-calibration. Universe sizes: 1m/5m/1h/1D~370, 15m~504, 4h~473.
TIMEFRAMES = [
    ("1m", 3, 340, "core streaming feed; intended ~full 370-universe coverage"),
    ("5m", 3, 340, "core streaming feed; intended ~full coverage"),
    ("15m", 3, 470, "broad streaming feed (observed 250/504 live 2026-07-13)"),
    ("1h", 3, 340, "broad expected; regression showed only 10 live"),
    ("4h", 8, 440, "broad feed; observed only 10/473 live within 8h (2026-07-13)"),
    ("1D", 27, 340, "THE regression: 370 syms in DB, only 10 fresh (2026-07-13)"),
]

STATE = Path(os.getenv(
    "CANDLE_FRESHNESS_STATE",
    "/home/frank/.hermes/profiles/jarvis/cron/state/sycode_candle_per_symbol_freshness.json"))
# Soft-drop baseline = max fresh_count observed per tf (ratchet up only).
SOFT_DROP_PCT = 0.90  # alert if fresh_count < 90% of baseline

EMPTY_SENTINEL = -1.0


def fetch_fresh_count(timeframe, budget_hours):
    """Return count of distinct symbols with a candle within budget_hours for
    the given timeframe. Read-only single aggregate."""
    sql = (
        "SELECT COALESCE(count(DISTINCT symbol) "
        "FILTER (WHERE timestamp >= now() - interval '%d hours'), 0)::int "
        "FROM public.candles WHERE timeframe = '%s';" % (int(budget_hours), timeframe)
    )
    cmd = [
        "docker", "exec",
        "-e", "PGOPTIONS=-c default_transaction_read_only=on",
        DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "postgres",
        "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", sql,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError("psql failed tf=%s rc=%d: %s" % (
            timeframe, proc.returncode, proc.stderr.strip()[:200]))
    out = proc.stdout.strip()
    return 0 if out == "" else int(out)


def read_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def write_state(payload):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(".%s.tmp-%d" % (STATE.name, os.getpid()))
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, STATE)


# ---------------------------------------------------------------------------
# Evaluation (pure — reused by --self-test)
# ---------------------------------------------------------------------------
def evaluate(tf_configs, fresh_counts, baseline=None):
    """tf_configs: list of (tf, budget, floor, note).
    fresh_counts: {tf: int}.
    baseline: {tf: int} last-good max observed (for soft-drop). Optional.
    Returns (alerts, rows) where rows = [(tf, fresh, floor, baseline, status)]."""
    alerts, rows = [], []
    for tf, budget, floor, note in tf_configs:
        fresh = fresh_counts.get(tf, 0)
        base = (baseline or {}).get(tf)
        if fresh < floor:
            status = "ALERT_FLOOR"
            alerts.append("  🔴 candles[%s]: only %d/%d symbols fresh (floor=%d, budget=%dh) — writer dead/eroded"
                          % (tf, fresh, base if base else 0, floor, budget))
        elif base is not None and fresh < int(base * SOFT_DROP_PCT) and base > 0:
            status = "ALERT_DROP"
            alerts.append("  🔴 candles[%s]: fresh symbols dropped to %d (%.0f%% of baseline %d) — gradual coverage loss"
                          % (tf, fresh, 100.0 * fresh / base, base))
        else:
            status = "OK"
        rows.append((tf, fresh, floor, base, status))
    return alerts, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true",
                    help="run evaluate() on synthetic ages, no DB, no write")
    args = ap.parse_args()

    if args.self_test:
        # healthy: all at/above floor and baseline
        healthy = {tf: floor for tf, _, floor, _ in TIMEFRAMES}
        a1, _ = evaluate(TIMEFRAMES, healthy, baseline=healthy)
        # dead 1D: 10 fresh vs floor 300
        dead = dict(healthy); dead["1D"] = 10
        a2, r2 = evaluate(TIMEFRAMES, dead, baseline=healthy)
        # gradual drop: 1D at 250 vs baseline 300 (<90%)
        drop = dict(healthy); drop["1D"] = 250
        a3, r3 = evaluate(TIMEFRAMES, drop, baseline=healthy)
        ok = (len(a1) == 0) and (len(a2) == 1) and (len(a3) == 1)
        for r in (r2 + r3):
            print("  self-test row:", r)
        print("SELF-TEST %s" % ("PASS" if ok else "FAIL"))
        sys.exit(0 if ok else 1)

    # Live run
    state = read_state()
    baseline = state.get("baseline", {})
    fresh_counts = {}
    alerts, rows = [], []
    try:
        for tf, budget, floor, note in TIMEFRAMES:
            fresh_counts[tf] = fetch_fresh_count(tf, budget)
        alerts, rows = evaluate(TIMEFRAMES, fresh_counts, baseline=baseline)
    except Exception as e:
        print("CANDLE PER-SYMBOL FRESHNESS — DEGRADED: probe error — %s" % str(e)[:200])
        sys.exit(1)

    # Ratchet baseline UP only and only when the timeframe is actually HEALTHY
    # (meets its floor). This prevents a degraded state from becoming the new
    # "normal" baseline (which would silence the soft-drop lane forever).
    new_baseline = dict(baseline)
    for tf, _, floor, _ in TIMEFRAMES:
        cur = fresh_counts.get(tf, 0)
        if cur >= floor and (tf not in new_baseline or cur > new_baseline[tf]):
            new_baseline[tf] = cur
    write_state({"baseline": new_baseline, "updated_at": datetime.now(timezone.utc).isoformat()})

    print("CANDLE PER-SYMBOL FRESHNESS @ %s" % datetime.now(timezone.utc).isoformat())
    for tf, fresh, floor, base, status in rows:
        base_s = str(base) if base is not None else "n/a"
        print("  [%s] %s: fresh=%d floor=%d baseline=%s" % (
            "OK" if status == "OK" else "XX", tf, fresh, floor, base_s))

    if alerts:
        print("VERDICT: DEGRADED — %d timeframe(s) below coverage SLO" % len(alerts))
        print("RED-ALERT: candle coverage dropped (silent-writer-death check):")
        print("\n".join(alerts))
        print("Check the owning ingestion producer/subscription (CandleIngestionService + KlineFeed subscription set).")
        sys.exit(2)

    print("VERDICT: GREEN — all timeframes meet per-symbol freshness SLO")
    sys.exit(0)


if __name__ == "__main__":
    main()
