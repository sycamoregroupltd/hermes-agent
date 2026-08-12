#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. The cron shim in
# trading-devops/scripts/ execs THIS file. Owner: trading-data-oracle (t_fa200457).
#
# sycode_pipeline_anomaly_engine.py — Real-Time Pipeline Anomaly Detection Engine
# (t_fa200457, parent proposal t_9b331179 APPROVED 2026-07-06).
#
# Active, scheduled anomaly detection across the SycodeTrading data pipelines.
# Audits FIVE dimensions in one pass:
#   1. SURFACE FRESHNESS  — the 12 certified surfaces from
#      analytics/data-surface-register.md (NS-P2.3), each with its own SLO.
#      (SURFACES list below is kept in lockstep with the register + the
#      sycode_surface_freshness_monitor.py companion.)
#   2. FEATURE DENSITY    — consuming-field fill rates on signal_journeys
#      (NS-P2.5 pattern; catches producer-fresh-but-consumer-NULL outages).
#   3. PG STAT ACTIVITY   — connection utilization vs max_connections,
#      idle-in-transaction accumulation, long-running active queries.
#   4. TABLE BLOAT        — n_dead_tup ratio proxy per critical table.
#   5. CLEAN-COHORT ACCRUAL — clean-epoch outcome accrual vs the NS-P3
#      100 (fusion re-baseline) and 300 (research entry) bars.
#
# On ANY warning/breach finding, exports structured diagnostic evidence to
#   /tmp/data-integrity-diagnostics/<timestamp>/diagnostics.json
#   /tmp/data-integrity-diagnostics/<timestamp>/anomalies.csv
# (acceptance criterion #2). Exits 0 healthy, 1 operational error,
# 2 findings present (warnings and/or breaches — stdout carries the alert lines).
#
# SAFETY (paper-only, read-only):
#   * SELECT-only SQL, executed via docker exec with PGOPTIONS
#     default_transaction_read_only=on. NEVER count(*) on giant tables from a
#     hot path (37M-row seq-scan lesson from dgx_data_freshness_probe.py) —
#     freshness uses max(ts) only, bloat uses pg_stat_user_tables catalogs.
#   * Never prints or persists credentials, tokens, or .env content.
#   * Never writes to production tables; the ONLY filesystem writes are the
#     diagnostic export under /tmp/data-integrity-diagnostics/.
#
# Consumers: t_fa200457 review, t_c78495f8 auto-remediation lane (reads
# /tmp/data-integrity-diagnostics/), t_acb010ea kanban-routing lane, north-star
# sweep. The registering cron routes non-zero exit to #critical-alerts.

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_CONTAINER = "sycodetrading-supabase-db"
DIAG_DIR = Path(os.getenv("DATA_INTEGRITY_DIAG_DIR", "/tmp/data-integrity-diagnostics"))
REGISTER = "analytics/data-surface-register.md"
EPOCH_START = "2026-07-05 22:41:00+00"  # f553af43c redeploy (clean-epoch cutoff)
BAR_REBASELINE = 100
BAR_RESEARCH = 300

# ---------------------------------------------------------------------------
# CERTIFIED SURFACES — keep in lockstep with analytics/data-surface-register.md
# and sycode_surface_freshness_monitor.py. mode: alert|pending|gap (same
# semantics as the companion surface monitor).
# ---------------------------------------------------------------------------
SURFACES = [
    ("candles",                "candles",                  "timestamp", 1.0,  "alert",
     "1m feed lands every minute (~800 rows/h); 1h SLO catches a dead collector within the cron hour"),
    ("funding_rate_history",   "funding_rate_history",     "timestamp", 1.0,  "alert",
     "continuous (~50k rows/h); the surface whose silent outage class motivated the register"),
    ("oi_snapshots",           "oi_snapshots",             "timestamp", 1.0,  "alert",
     "continuous (~4.5k rows/h)"),
    ("orderbook_snapshots",    "orderbook_snapshots",      "captured_at", 1.0, "alert",
     "continuous (~500 rows/h); NOT covered by the dgx producer probe"),
    ("signal_journeys",        "signal_journeys",          "created_at", 3.0,  "alert",
     "signal-gated (~150-550/h observed); 3h budget tolerates quiet market stretches"),
    ("signal_journey_events",  "signal_journey_events",    "occurred_at", 3.0, "alert",
     "event stream for journeys; was frozen 15d once (2026-07-02 lesson)"),
    ("onchain_snapshots",      "onchain_snapshots",        "created_at", 27.0, "alert",
     "daily batch ~00:00-01:10Z; 27h budget = daily cadence + margin"),
    ("stablecoin_flow_hourly", "stablecoin_flow_hourly_v1", "hour_utc",  4.0,  "alert",
     "hourly builder; 4h budget tolerates builder lag"),
    ("trade_close_events",     "trade_close_events",       "created_at", 12.0, "alert",
     "event-driven; 0 closes can be legitimate under quarantine/paper halt — flat-book suppressed"),
    ("decision_outcomes",      "decision_outcomes",        "created_at", 8.0,  "alert",
     "journey_finalizer + trade_close labelers write continuously when up; flat-book suppressed"),
    ("r_multiple_labels",      "r_multiple_labels",        "computed_at", 26.0, "pending",
     "NS-P3.2 labeler merged but 0 clean-epoch closes yet; graduates to 26h SLO on first batch"),
    ("pattern_win_rate_registry", "pattern_win_rate_registry", "last_updated", 26.0, "alert",
     "Class C read-model refreshed by registry updater; advisory only for research"),
    ("liquidation_events",     "liquidation_events",       "timestamp", 0.0,  "gap",
     "capture graduated to pending 2026-08-05 (t_b7372598); reported, never alerts"),
]

# Flat-book suppression: closing-activity surfaces only advance when the book
# is actually closing positions (paper drought/halt is legitimate, not a writer
# death). Same semantic as sycode_surface_freshness_monitor.py.
FLAT_BOOK_SURFACES = {"decision_outcomes", "trade_close_events"}

# Consuming-field density features (NS-P2.5 companion list).
TRACKED_FEATURES = [
    "funding_rate_trend", "market_oi_delta_percent", "pattern_strength",
    "directional_conviction", "market_funding_rate", "market_funding_rate_annualized",
    "market_open_interest", "regime_volatility", "regime_trend", "regime_score",
    "regime_direction", "regime_key", "regime_favorable",
    "structure_levels", "liquidation_context",
]

# Critical tables for the bloat check (pg_stat_user_tables catalog, cheap).
BLOAT_TABLES = [
    "candles", "funding_rate_history", "oi_snapshots", "orderbook_snapshots",
    "signal_journeys", "signal_journey_events", "decision_outcomes",
    "trade_close_events", "r_multiple_labels", "onchain_snapshots",
]

# ---------------------------------------------------------------------------
# THRESHOLDS (warning vs breach) — from the approved proposal's integrity
# metrics table plus the register SLOs. severity = "warning" | "breach".
# ---------------------------------------------------------------------------
THRESHOLDS = {
    # Freshness: warning when age > SLO; breach when age > 2x SLO.
    "freshness_breach_mult": 2.0,
    # Density: warning = >10pp day-over-day drop OR <50% absolute (register);
    # breach = >25pp drop OR <30% absolute.
    "density_warn_drop_pp": 10.0,
    "density_breach_drop_pp": 25.0,
    "density_warn_floor_pct": 50.0,
    "density_breach_floor_pct": 30.0,
    "density_min_rows": 25,
    # pg_stat_activity: connection utilization (fraction of max_connections).
    "conn_util_warn": 0.80,
    "conn_util_breach": 0.95,
    # idle-in-transaction accumulation.
    "idle_in_txn_warn": 5,
    "idle_in_txn_breach": 20,
    # long-running active queries (seconds).
    "long_running_warn_s": 60,
    "long_running_breach_s": 300,
    # Table bloat (dead-tuple ratio %).
    "bloat_warn_pct": 20.0,
    "bloat_breach_pct": 40.0,
    # Clean cohort accrual (proposal: warn n<100, breach n<30).
    "cohort_warn_n": 100,
    "cohort_breach_n": 30,
}

EMPTY_SENTINEL = -1.0
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


# ---------------------------------------------------------------------------
# Data collection — run_sql is the single injection point for tests (monkeypatch
# it to return canned rows without a live DB).
# ---------------------------------------------------------------------------
def run_sql(sql, csv_mode=False, timeout=180):
    """Execute a read-only SQL statement against the Sycode DB container.

    csv_mode=True returns a list of dict rows (psql --csv); otherwise returns
    the trimmed stdout string. Read-only is enforced via PGOPTIONS. Raises
    RuntimeError on psql failure (fail-closed: an unreadable DB must never look
    healthy).
    """
    cmd = [
        "docker", "exec",
        "-e", "PGOPTIONS=-c default_transaction_read_only=on",
        DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "postgres",
        "-X", "-q", "-v", "ON_ERROR_STOP=1",
    ]
    if csv_mode:
        cmd.append("--csv")
    else:
        # tuples-only + unaligned: scalar results come back as bare values
        # (no header/footer noise) — same convention as the companion monitors.
        cmd += ["-t", "-A"]
    cmd += ["-c", sql]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed rc={proc.returncode}: {proc.stderr.strip()[:300]}")
    if csv_mode:
        return list(csv.DictReader(io.StringIO(proc.stdout)))
    return proc.stdout.strip()


def fetch_open_position_count():
    """Return count of currently-open positions (closed_at IS NULL). 0 on any
    error (fail-open: suppress under doubt — same semantic as the companion)."""
    try:
        rows = run_sql("SELECT count(*) AS n FROM public.managed_positions WHERE closed_at IS NULL;", csv_mode=True)
        return int(rows[0]["n"]) if rows else 0
    except Exception:
        return 0


def is_flat_book():
    return fetch_open_position_count() == 0


def collect_freshness():
    """Return {surface: age_hours or EMPTY_SENTINEL}."""
    ages = {}
    for surface, table, col, _slo, _mode, _note in SURFACES:
        if not _IDENT_RE.match(col):
            raise ValueError(f"illegal column identifier in config: {col!r}")
        q = (f"SELECT COALESCE(EXTRACT(EPOCH FROM (now()-max({col})))/3600.0, {EMPTY_SENTINEL}) "
             f"FROM public.{table};")
        raw = run_sql(q)
        try:
            ages[surface] = float(raw.strip().splitlines()[-1])
        except (ValueError, IndexError) as e:
            raise RuntimeError(f"freshness parse failed on {surface}: raw={raw!r} ({e})")
    return ages


def collect_density():
    """Return (days, rows_by_day, pct[day][feature]=fill%)."""
    for f in TRACKED_FEATURES:
        if not _IDENT_RE.match(f):
            raise ValueError(f"illegal feature identifier in config: {f!r}")
    pct_exprs = ",\n       ".join(
        f"round(100.0*count({f})/count(*), 1) AS {f}" for f in TRACKED_FEATURES
    )
    sql = (
        "SELECT (triggered_at AT TIME ZONE 'UTC')::date AS day,\n"
        "       count(*) AS n_rows,\n"
        f"       {pct_exprs}\n"
        "FROM public.signal_journeys\n"
        "WHERE triggered_at >= ((now() AT TIME ZONE 'UTC')::date - INTERVAL '6 days')\n"
        "GROUP BY 1 ORDER BY 1;"
    )
    rows = run_sql(sql, csv_mode=True)
    days, rows_by_day, pct = [], {}, {}
    for row in rows:
        day = row["day"]
        days.append(day)
        rows_by_day[day] = int(row["n_rows"])
        pct[day] = {f: float(row[f]) for f in TRACKED_FEATURES}
    return sorted(days), rows_by_day, pct


def collect_pg_activity():
    """Return (conn_stats dict, max_connections int)."""
    rows = run_sql(
        "SELECT count(*) AS total,\n"
        "       count(*) FILTER (WHERE state='active') AS active,\n"
        "       count(*) FILTER (WHERE state='idle') AS idle,\n"
        "       count(*) FILTER (WHERE state='idle in transaction') AS idle_in_txn,\n"
        "       count(*) FILTER (WHERE state='active' AND now()-query_start > interval '60 seconds') AS long_running\n"
        "FROM pg_stat_activity WHERE datname='postgres';",
        csv_mode=True,
    )
    conn = rows[0] if rows else {}
    max_conn_raw = run_sql("SHOW max_connections;")
    try:
        max_conns = int(max_conn_raw.strip().splitlines()[-1])
    except (ValueError, IndexError):
        max_conns = 100
    return {
        "total": int(conn.get("total") or 0),
        "active": int(conn.get("active") or 0),
        "idle": int(conn.get("idle") or 0),
        "idle_in_txn": int(conn.get("idle_in_txn") or 0),
        "long_running": int(conn.get("long_running") or 0),
        "max_connections": max_conns,
    }


def collect_bloat():
    """Return {table: dead_pct} from pg_stat_user_tables (dead/(live+dead))."""
    inlist = ", ".join(f"'{t}'" for t in BLOAT_TABLES)
    rows = run_sql(
        "SELECT relname, n_live_tup, n_dead_tup FROM pg_stat_user_tables "
        f"WHERE relname IN ({inlist});",
        csv_mode=True,
    )
    bloat = {}
    for r in rows:
        live = int(r.get("n_live_tup") or 0)
        dead = int(r.get("n_dead_tup") or 0)
        bloat[r["relname"]] = round(100.0 * dead / (live + dead + 1), 1)
    return bloat


def collect_cohort():
    """Return dict with clean-epoch accrual numbers (reuses NS-P3.5 queries)."""
    outcomes = run_sql(
        "SELECT count(*) AS n_outcomes,\n"
        "       count(*) FILTER (WHERE created_at > now() - interval '24 hours') AS n_last24h\n"
        f"FROM decision_outcomes WHERE created_at > '{EPOCH_START}' AND is_final\n"
        "  AND COALESCE(contaminated, false) = false;",
        csv_mode=True,
    )
    signals = run_sql(
        f"SELECT count(*) AS n FROM signal_journeys WHERE created_at > '{EPOCH_START}';",
        csv_mode=True,
    )
    o = outcomes[0] if outcomes else {}
    return {
        "n_clean_outcomes": int(o.get("n_outcomes") or 0),
        "n_clean_outcomes_last24h": int(o.get("n_last24h") or 0),
        "n_signals_since_epoch": int(signals[0]["n"]) if signals else 0,
        "bar_rebaseline": BAR_REBASELINE,
        "bar_research": BAR_RESEARCH,
    }


# ---------------------------------------------------------------------------
# Evaluation — pure functions, reused by --self-test and unit tests.
# Each returns (findings, rows) where findings is a list of dicts:
#   {check, surface, severity, value, threshold, message}
# ---------------------------------------------------------------------------
def evaluate_freshness(ages, flat_book=False):
    findings, rows = [], []
    for surface, _t, _c, slo_h, mode, _n in SURFACES:
        age = ages[surface]
        empty = age == EMPTY_SENTINEL
        if mode == "gap":
            status = "GAP"
        elif empty and mode == "pending":
            status = "PENDING"
        elif empty:
            status = "EMPTY"
            findings.append({
                "check": "surface_freshness", "surface": surface, "severity": "breach",
                "value": "EMPTY", "threshold": f"SLO {slo_h:g}h",
                "message": f"{surface} is unexpectedly EMPTY (certified surface with SLO {slo_h:g}h)",
            })
        elif surface in FLAT_BOOK_SURFACES and flat_book:
            status = "FLAT"
        elif age > slo_h * THRESHOLDS["freshness_breach_mult"]:
            status = "BREACH"
            findings.append({
                "check": "surface_freshness", "surface": surface, "severity": "breach",
                "value": f"{age:.1f}h", "threshold": f"SLO {slo_h:g}h x{THRESHOLDS['freshness_breach_mult']:g}",
                "message": f"{surface} stale {age:.1f}h (breach threshold {slo_h * THRESHOLDS['freshness_breach_mult']:.1f}h)",
            })
        elif age > slo_h:
            status = "STALE"
            findings.append({
                "check": "surface_freshness", "surface": surface, "severity": "warning",
                "value": f"{age:.1f}h", "threshold": f"SLO {slo_h:g}h",
                "message": f"{surface} stale {age:.1f}h (SLO {slo_h:g}h)",
            })
        else:
            status = "FRESH"
        rows.append({"surface": surface, "status": status, "age_h": None if empty else age,
                     "slo_h": slo_h, "mode": mode})
    return findings, rows


def evaluate_density(days, rows_by_day, pct):
    """Latest alert-eligible day (n>=min) vs previous eligible day. Returns
    (findings, eval_day, prev_day)."""
    eligible = [d for d in days if rows_by_day.get(d, 0) >= THRESHOLDS["density_min_rows"]]
    if not eligible:
        return [], None, None
    eval_day = eligible[-1]
    prev_day = eligible[-2] if len(eligible) >= 2 else None
    findings = []
    for f in TRACKED_FEATURES:
        cur = pct[eval_day].get(f)
        if cur is None:
            continue
        drop_pp = 0.0
        if prev_day is not None:
            prev = pct[prev_day].get(f)
            if prev is not None:
                drop_pp = prev - cur
        sev = None
        if cur < THRESHOLDS["density_breach_floor_pct"] or drop_pp > THRESHOLDS["density_breach_drop_pp"]:
            sev = "breach"
        elif cur < THRESHOLDS["density_warn_floor_pct"] or drop_pp > THRESHOLDS["density_warn_drop_pp"]:
            sev = "warning"
        if sev:
            findings.append({
                "check": "feature_density", "surface": f, "severity": sev,
                "value": f"{cur:.1f}%", "threshold": "floor/drop",
                "message": f"feature-density {sev}: {f} fill {cur:.1f}% on {eval_day}"
                           + (f" (dropped {drop_pp:.1f}pp from {prev_day})" if prev_day and drop_pp else ""),
            })
    return findings, eval_day, prev_day


def evaluate_pg_activity(conn):
    findings = []
    total, maxc = conn["total"], conn["max_connections"]
    util = total / maxc if maxc else 0.0
    if util >= THRESHOLDS["conn_util_breach"]:
        findings.append({
            "check": "pg_stat_activity", "surface": "connections", "severity": "breach",
            "value": f"{total}/{maxc}", "threshold": f"{THRESHOLDS['conn_util_breach']:.0%}",
            "message": f"connection utilization {util:.0%} ({total}/{maxc}) breaches {THRESHOLDS['conn_util_breach']:.0%}",
        })
    elif util >= THRESHOLDS["conn_util_warn"]:
        findings.append({
            "check": "pg_stat_activity", "surface": "connections", "severity": "warning",
            "value": f"{total}/{maxc}", "threshold": f"{THRESHOLDS['conn_util_warn']:.0%}",
            "message": f"connection utilization {util:.0%} ({total}/{maxc}) exceeds {THRESHOLDS['conn_util_warn']:.0%}",
        })
    if conn["idle_in_txn"] >= THRESHOLDS["idle_in_txn_breach"]:
        findings.append({
            "check": "pg_stat_activity", "surface": "idle_in_txn", "severity": "breach",
            "value": conn["idle_in_txn"], "threshold": THRESHOLDS["idle_in_txn_breach"],
            "message": f"{conn['idle_in_txn']} idle-in-transaction connections (breach >= {THRESHOLDS['idle_in_txn_breach']}) — likely leaked transactions",
        })
    elif conn["idle_in_txn"] >= THRESHOLDS["idle_in_txn_warn"]:
        findings.append({
            "check": "pg_stat_activity", "surface": "idle_in_txn", "severity": "warning",
            "value": conn["idle_in_txn"], "threshold": THRESHOLDS["idle_in_txn_warn"],
            "message": f"{conn['idle_in_txn']} idle-in-transaction connections (warn >= {THRESHOLDS['idle_in_txn_warn']})",
        })
    if conn["long_running"] >= 1:
        findings.append({
            "check": "pg_stat_activity", "surface": "long_running", "severity": "warning",
            "value": conn["long_running"], "threshold": THRESHOLDS["long_running_warn_s"],
            "message": f"{conn['long_running']} active queries running >{THRESHOLDS['long_running_warn_s']}s",
        })
    return findings


def evaluate_bloat(bloat):
    findings = []
    for table, dead_pct in sorted(bloat.items(), key=lambda kv: -kv[1]):
        if dead_pct >= THRESHOLDS["bloat_breach_pct"]:
            findings.append({
                "check": "table_bloat", "surface": table, "severity": "breach",
                "value": f"{dead_pct:.1f}%", "threshold": f"{THRESHOLDS['bloat_breach_pct']:.0f}%",
                "message": f"table bloat proxy {dead_pct:.1f}% dead tuples on {table} (breach >= {THRESHOLDS['bloat_breach_pct']:.0f}%) — VACUUM candidate",
            })
        elif dead_pct >= THRESHOLDS["bloat_warn_pct"]:
            findings.append({
                "check": "table_bloat", "surface": table, "severity": "warning",
                "value": f"{dead_pct:.1f}%", "threshold": f"{THRESHOLDS['bloat_warn_pct']:.0f}%",
                "message": f"table bloat proxy {dead_pct:.1f}% dead tuples on {table} (warn >= {THRESHOLDS['bloat_warn_pct']:.0f}%)",
            })
    return findings


def evaluate_cohort(cohort):
    findings = []
    n = cohort["n_clean_outcomes"]
    if n < THRESHOLDS["cohort_breach_n"]:
        findings.append({
            "check": "cohort_accrual", "surface": "clean_outcomes", "severity": "breach",
            "value": n, "threshold": THRESHOLDS["cohort_breach_n"],
            "message": f"clean-cohort accrual {n} < {THRESHOLDS['cohort_breach_n']} (breach) — halt calibration/promotion (NS-P3)",
        })
    elif n < THRESHOLDS["cohort_warn_n"]:
        findings.append({
            "check": "cohort_accrual", "surface": "clean_outcomes", "severity": "warning",
            "value": n, "threshold": THRESHOLDS["cohort_warn_n"],
            "message": f"clean-cohort accrual {n} < {THRESHOLDS['cohort_warn_n']} (warn) — below C4 fusion re-baseline bar",
        })
    return findings


# ---------------------------------------------------------------------------
# Diagnostics export — structured JSON + CSV evidence (acceptance criterion #2)
# ---------------------------------------------------------------------------
def export_diagnostics(findings, snapshot, export_dir=None):
    """Write diagnostics.json + anomalies.csv into a fresh timestamped dir
    under DIAG_DIR. Returns the dir path. Only called when findings exist."""
    export_dir = Path(export_dir) if export_dir else DIAG_DIR
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out = export_dir / stamp
    out.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "engine": "sycode_pipeline_anomaly_engine.py",
        "task": "t_fa200457",
        "finding_count": len(findings),
        "severity_summary": {
            "breach": sum(1 for f in findings if f["severity"] == "breach"),
            "warning": sum(1 for f in findings if f["severity"] == "warning"),
        },
        "findings": findings,
        "snapshot": snapshot,
    }
    (out / "diagnostics.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")

    with open(out / "anomalies.csv", "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["check", "surface", "severity", "value", "threshold", "message"])
        writer.writeheader()
        for f in findings:
            writer.writerow({k: f.get(k, "") for k in writer.fieldnames})
    return out


# ---------------------------------------------------------------------------
# Self-test — pure evaluation logic on synthetic data (no DB, no export)
# ---------------------------------------------------------------------------
def self_test():
    results = []

    # Freshness: fresh silent, stale warns, 2x stale breaches, empty breaches,
    # pending silent, gap silent, flat-book suppresses closing surfaces.
    ages = {
        "candles": 0.4,                     # FRESH
        "funding_rate_history": 1.5,        # STALE (warn; breach would be >2h)
        "oi_snapshots": 3.0,                # BREACH (3h > 2h)
        "orderbook_snapshots": EMPTY_SENTINEL,  # EMPTY breach
        "signal_journeys": 0.5,             # FRESH
        "signal_journey_events": 0.5,       # FRESH
        "onchain_snapshots": 26.0,          # FRESH (27h SLO)
        "stablecoin_flow_hourly": 0.5,      # FRESH
        "trade_close_events": 28.9,         # stale but flat-book -> FLAT
        "decision_outcomes": 28.9,          # stale but flat-book -> FLAT
        "r_multiple_labels": EMPTY_SENTINEL,  # PENDING silent
        "pattern_win_rate_registry": 30.0,  # BREACH (26h x2 = 52h? no: 30>52 false -> STALE warn actually)
        "liquidation_events": EMPTY_SENTINEL,  # GAP silent
    }
    findings, rows = evaluate_freshness(ages, flat_book=True)
    st = {r["surface"]: r["status"] for r in rows}
    sev = {(f["surface"], f["severity"]) for f in findings}
    results.append(("fresh surface silent", st["candles"] == "FRESH"))
    results.append(("stale surface warns", st["funding_rate_history"] == "STALE" and ("funding_rate_history", "warning") in sev))
    results.append(("2x-stale surface breaches", st["oi_snapshots"] == "BREACH" and ("oi_snapshots", "breach") in sev))
    results.append(("unexpected-empty breaches", st["orderbook_snapshots"] == "EMPTY" and ("orderbook_snapshots", "breach") in sev))
    results.append(("pending empty silent", st["r_multiple_labels"] == "PENDING"))
    results.append(("gap silent", st["liquidation_events"] == "GAP"))
    results.append(("flat-book closing surface silent", st["trade_close_events"] == "FLAT" and st["decision_outcomes"] == "FLAT"))
    results.append(("pattern registry warns at 30h", st["pattern_win_rate_registry"] == "STALE"))

    # Density: healthy silent; -15pp warns; <30% breaches.
    feats = TRACKED_FEATURES
    days = ["2026-01-01", "2026-01-02"]
    rowsd = {d: 1000 for d in days}
    base = {f: 99.0 for f in feats}
    p_ok = {days[0]: dict(base), days[1]: dict(base)}
    f_ok, _, _ = evaluate_density(days, rowsd, p_ok)
    results.append(("healthy density silent", f_ok == []))

    p_warn = {days[0]: dict(base), days[1]: dict(base)}
    p_warn[days[1]]["funding_rate_trend"] = 84.0  # -15pp
    f_warn, _, _ = evaluate_density(days, rowsd, p_warn)
    results.append((">10pp density drop warns",
                    any(x["severity"] == "warning" and x["surface"] == "funding_rate_trend" for x in f_warn)))

    p_br = {days[0]: dict(base), days[1]: dict(base)}
    p_br[days[1]]["market_oi_delta_percent"] = 25.0  # below 30% floor
    f_br, _, _ = evaluate_density(days, rowsd, p_br)
    results.append(("<30% density floor breaches",
                    any(x["severity"] == "breach" and x["surface"] == "market_oi_delta_percent" for x in f_br)))

    # pg_stat_activity thresholds.
    f_conn = evaluate_pg_activity({"total": 190, "max_connections": 200, "active": 5, "idle": 180,
                                   "idle_in_txn": 3, "long_running": 1})
    results.append(("95% conn utilization breaches",
                    any(x["severity"] == "breach" and x["surface"] == "connections" for x in f_conn)))
    f_conn2 = evaluate_pg_activity({"total": 170, "max_connections": 200, "active": 5, "idle": 160,
                                    "idle_in_txn": 3, "long_running": 0})
    results.append(("85% conn utilization warns",
                    any(x["severity"] == "warning" and x["surface"] == "connections" for x in f_conn2)))
    f_txn = evaluate_pg_activity({"total": 10, "max_connections": 200, "active": 1, "idle": 4,
                                  "idle_in_txn": 25, "long_running": 0})
    results.append(("idle-in-txn 25 breaches",
                    any(x["severity"] == "breach" and x["surface"] == "idle_in_txn" for x in f_txn)))

    # Bloat.
    f_bloat = evaluate_bloat({"candles": 0.5, "decision_outcomes": 71.5, "signal_journeys": 10.1, "trade_close_events": 5.5})
    results.append(("71.5% bloat breaches",
                    any(x["severity"] == "breach" and x["surface"] == "decision_outcomes" for x in f_bloat)))
    results.append(("10% bloat silent",
                    all(x["surface"] != "signal_journeys" for x in f_bloat)))

    # Cohort.
    f_cohort = evaluate_cohort({"n_clean_outcomes": 25, "n_clean_outcomes_last24h": 0,
                                "n_signals_since_epoch": 1000, "bar_rebaseline": 100, "bar_research": 300})
    results.append(("cohort 25 breaches",
                    any(x["severity"] == "breach" and x["surface"] == "clean_outcomes" for x in f_cohort)))
    f_cohort2 = evaluate_cohort({"n_clean_outcomes": 150, "n_clean_outcomes_last24h": 0,
                                 "n_signals_since_epoch": 1000, "bar_rebaseline": 100, "bar_research": 300})
    results.append(("cohort 150 silent", f_cohort2 == []))

    all_ok = all(ok for _, ok in results)
    for name, ok in results:
        print(f"SELF-TEST {'PASS' if ok else 'FAIL'}: {name}")
    print(f"SELF-TEST {'PASS' if all_ok else 'FAIL'}: overall")
    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
def run(export_dir=None):
    """Core engine body — called by main() after argparse and by tests directly."""
    try:
        ages = collect_freshness()
        days, rows_by_day, pct = collect_density()
        conn = collect_pg_activity()
        bloat = collect_bloat()
        cohort = collect_cohort()
    except Exception as e:
        print(f"ERROR anomaly engine: {e}", file=sys.stderr)
        sys.exit(1)

    flat_book = is_flat_book()
    findings = []
    f, fresh_rows = evaluate_freshness(ages, flat_book=flat_book)
    findings += f
    f, eval_day, prev_day = evaluate_density(days, rows_by_day, pct)
    findings += f
    findings += evaluate_pg_activity(conn)
    findings += evaluate_bloat(bloat)
    findings += evaluate_cohort(cohort)

    snapshot = {
        "freshness": fresh_rows,
        "density": {"days": days, "rows_by_day": rows_by_day, "pct": pct,
                    "eval_day": eval_day, "prev_day": prev_day},
        "pg_stat_activity": conn,
        "table_bloat": bloat,
        "cohort_accrual": cohort,
        "flat_book": flat_book,
    }

    breaches = sum(1 for x in findings if x["severity"] == "breach")
    warnings = sum(1 for x in findings if x["severity"] == "warning")

    # Operational/clean output -> STDERR so a no-agent watchdog cron stays SILENT
    # when healthy (empty stdout = no delivery). ALERT lines go to STDOUT.
    print(f"ANOMALY ENGINE @ {datetime.now(timezone.utc).isoformat()}", file=sys.stderr)
    print(f"freshness: {sum(1 for r in fresh_rows if r['status'] == 'FRESH')}/{len(fresh_rows)} within SLO "
          f"(density eval {eval_day or 'n/a'} vs {prev_day or 'n/a'}, conn {conn['total']}/{conn['max_connections']}, "
          f"cohort {cohort['n_clean_outcomes']})", file=sys.stderr)

    if findings:
        out = export_diagnostics(findings, snapshot, export_dir=export_dir)
        print(f"diagnostics exported: {out}", file=sys.stderr)
        print(f"ANOMALY ENGINE: {breaches} breach(es), {warnings} warning(s) — evidence in {out}")
        for x in sorted(findings, key=lambda z: (z["severity"] != "breach", z["check"], z["surface"])):
            print(f"  [{x['severity'].upper()}] {x['check']}/{x['surface']}: {x['message']}")
        sys.exit(2)

    print("OK: no anomalies across freshness/density/pg_stat_activity/bloat/cohort", file=sys.stderr)
    sys.exit(0)


def main():
    ap = argparse.ArgumentParser(description="SycodeTrading real-time pipeline anomaly detection engine (t_fa200457)")
    ap.add_argument("--self-test", action="store_true",
                    help="run evaluation self-test on synthetic data (no DB, no export)")
    ap.add_argument("--export-dir", default=None,
                    help="override diagnostics export root (tests use a tmp dir)")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())
    run(export_dir=args.export_dir)


if __name__ == "__main__":
    main()
