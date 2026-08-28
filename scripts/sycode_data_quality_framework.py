#!/usr/bin/env python3
# invoker: hermes cron job or manual execution
#
# sycode_data_quality_framework.py
#
# Continuous Data Integrity Monitor & Diagnostic Evidence Collector
#
# Inspects:
#   1. Orphan rows in signal_pnl_points and trade_close_events.
#   2. Queue backlogs and starved pipelines (journey_finalizer, clean_outcome_binary_24h, event_dead_letter).
#   3. Clean journey cohort accrual metrics towards n >= 100 and n >= 300.
#
# On breach, exports comprehensive diagnostics to /tmp/data-integrity-diagnostics/
# as structured JSON/CSV evidence to accelerate human developer resolution.
#
# Safety boundaries: Paper-Mode Only, Zero Secrets, Read-Only database audits.

import argparse
import csv
import io
import json
import os
from hermes_cli import kanban_db as kb
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

# Set True when the current run detected an A3 credential/credit gate breach,
# so main() can suppress automated self-healing retries (review §5.2).
a3_gate_hit_this_run = False

# Database config
DB_CONTAINER = "sycodetrading-supabase-db"
EPOCH_START = "2026-07-05 22:41:00+00"  # f553af43c redeploy cutoff
EVIDENCE_DIR = Path("/tmp/data-integrity-diagnostics")

# Alarm thresholds
THRESHOLD_ORPHANS = 0                    # Any orphan is a breach
THRESHOLD_FINALIZER_BACKLOG = 50         # Max pending finalized outcomes allowed
# t_d9c7537b tuning: the binary_24h labeler is a periodic batch job. Its backlog
# sits in a STABLE steady-state (~700 immature-but-in-window journeys, measured
# 2026-07-11) that does NOT grow — it is not a true backlog. The prior threshold
# of 500 sat BELOW that steady-state, so it perpetually breached and re-alerted
# (the QUEUE_BACKLOG flood). Raised to 1500: still sensitive to a real surge
# (1h+ intake spike, ~10x the steady-state) while suppressing the benign window.
THRESHOLD_BINARY_BACKLOG = 1500          # Max pending binary 24h labels allowed (t_d9c7537b)
THRESHOLD_DLQ_COUNT = 0                  # Any dead-letter queue item is a breach
THRESHOLD_STARVATION_LAG_HOURS = 24.0    # Starvation threshold for active pipelines

# Cohort Targets
TARGET_REBASELINE = 100
TARGET_RESEARCH = 300

# ----------------------------------------------------------------------------
# SMART ALERTING & DISCORD COOLDOWN CONFIGURATION (review t_ba1827ad §2.2/§2.3/§4)
# ----------------------------------------------------------------------------

# On-disk alert state (review §4.1.4): last-sent Discord epoch keyed by metric.
# Read-write-safe (atomic temp+os.replace); never deletes other keys.
ALERT_STATE_PATH = Path(os.path.expanduser("~/.hermes/var/dq/alert_state.json"))

# Minimum cooldown between Discord alerts for the SAME persistent metric.
# Protects #critical-alerts / #fleet-reports from 5-min-cron spam (review §2.2).
DISCORD_COOLDOWN_SECONDS = 3600  # 1 hour

# Three-tier escalation timing (review §2.3):
#   Tier 1: new card  -> priority 0
#   Tier 2: breach persists > 24h -> priority 1 + reassign to trading-devops
#   Tier 3: A3 gate breach (credential/credit exhaustion) -> block + route to Frank
DISCORD_TARGET = "discord:#critical-alerts"
TIER2_ESCALATION_SECONDS = 86400  # 24h
TIER2_ASSIGNEE = "trading-devops"
TIER3_ASSIGNEE = "frank"

# A3-gate signatures (review §5.2): any diagnostic whose error text matches one
# of these is treated as a hard credential/credit exhaustion gate and routed to
# Frank with the card blocked (no automated retries to avoid credit waste).
A3_GATE_SIGNATURES = (
    "api key",
    "api_key",
    "invalid api key",
    "unauthorized",
    "401",
    "403",
    "forbidden",
    "rate limit",
    "rate-limit",
    "quota",
    "quota exceeded",
    "credits",
    "credit balance",
    "insufficient",
    "exhausted",
    "expired",
    "token expired",
    "credential",
    "auth failed",
    "authentication failed",
    "permission denied",
)

# ----------------------------------------------------------------------------
# BOARD RESOLUTION (systemic fix t_4f419b25)
#
# The diagnostic-card generator creates cards on a board slug (via
# `hermes kanban create --board <slug>`), but the dedup-query helpers
# previously resolved the kanban DB from the *ambient* HERMES_KANBAN_DB env
# var, which can diverge from the creation board (the on-disk `kanban/current`
# pin is `sycode-trading` while cards are created on `jarvis-os`). When they
# diverged, dedup lookups missed the existing card and DIAGNOSTIC cards
# multiplied across boards (the recurring "QUEUE_BACKLOG" flood). ALL board-DB
# lookups must resolve from the SAME board slug the card was created with. The
# idempotency key encodes it: diag:<board>:<type>:<metric>.
# ----------------------------------------------------------------------------

KANBAN_BOARDS_DIR = Path(os.path.expanduser("~/.hermes/kanban/boards"))


def resolve_board_db(board_slug):
    """Single source of truth for the kanban.db path of `board_slug`.

    Matches the kernel's board layout (`kanban/boards/<slug>/kanban.db`),
    falling back to the legacy flat `~/.hermes/kanban.db`. Using this
    everywhere guarantees card creation and dedup queries can never disagree
    on which DB to touch (systemic fix t_4f419b25)."""
    if board_slug:
        board_db = KANBAN_BOARDS_DIR / board_slug / "kanban.db"
        if board_db.exists():
            return str(board_db)
    legacy = os.path.expanduser("~/.hermes/kanban.db")
    return str(legacy)


def board_env(board_name):
    """Build a subprocess env pinned to `board_name` for `hermes kanban` calls.

    The CLI resolves the target DB from HERMES_KANBAN_DB (explicit path,
    highest priority) BEFORE HERMES_KANBAN_BOARD and the `--board` flag. If a
    parent process leaks HERMES_KANBAN_DB pointing at another board (e.g. an
    ambient sycode-trading pin), every `hermes kanban <subcommand>` call would
    silently write to the WRONG board, and the framework's key-driven dedup
    (which resolves the DB from the idempotency key's board slug) would no
    longer match — creating duplicate diag cards (t_4f419b25 root cause).

    So both vars are pinned: HERMES_KANBAN_DB to the resolved board DB path
    (via :func:`resolve_board_db`, the same source used by dedup queries) and
    HERMES_KANBAN_BOARD to the slug. Returns a fresh dict; the caller's env is
    never mutated.
    """
    env = os.environ.copy()
    env["HERMES_KANBAN_DB"] = resolve_board_db(board_name)
    env["HERMES_KANBAN_BOARD"] = board_name
    return env

# Auto-close: after this many consecutive healthy CDPDS/audit profiles the
# active diagnostic card is completed to clear the board (review §2.2/§4.1.5).
AUTO_CLOSE_CONSECUTIVE_HEALTHY = 2

HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")


def run_sql(sql):
    """Executes SQL against the Supabase database in read-only mode."""
    cmd = [
        "docker", "exec",
        "-e", "PGOPTIONS=-c default_transaction_read_only=on",
        DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "postgres",
        "-X", "-q", "-v", "ON_ERROR_STOP=1", "--csv", "-c", sql,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed rc={proc.returncode}: {proc.stderr.strip()[:500]}")
    return list(csv.DictReader(io.StringIO(proc.stdout)))


# ----------------------------------------------------------------------------
# ACTIVE INTEGRITY CHECKS
# ----------------------------------------------------------------------------

def inspect_orphans():
    """Traces orphan rows across signal_pnl_points and trade_close_events.

    Returns:
        dict: Orphan counts and sample rows. Each numeric metric is
        accompanied by a `status` key ("OK" or "UNKNOWN"): if the underlying
        SQL query times out or errors (e.g. the DB is congested by a long-held
        lock — these LEFT JOINs over ~2.4M signal_journeys are the most exposed
        queries, measured 55s+ standalone on 2026-08-11), the metric is
        reported as "UNKNOWN" instead of crashing the whole monitor. This is
        the same hardening applied to inspect_queues (t_d9c7537b): a failed
        measurement must not abort the audit before kanban routing happens —
        it is surfaced as MONITOR_DEGRADED so the incident stays visible.
    """
    UNKNOWN = "UNKNOWN"

    def safe_orphan(query, label):
        try:
            rows = run_sql(query)
            return len(rows), rows, "OK"
        except Exception as e:
            print(f"[WARN] inspect_orphans.{label} query failed ({type(e).__name__}): {e}",
                  file=sys.stderr)
            return UNKNOWN, [], UNKNOWN

    # 1. signal_pnl_points orphans (no signal_journeys match)
    pnl_count, pnl_orphans, pnl_status = safe_orphan(pnl_orphans_query(), "pnl_points")

    # 2. trade_close_events orphans (journey_id set but no signal_journeys match)
    close_j_count, close_journey_orphans, close_j_status = safe_orphan(
        close_journey_orphans_query(), "trade_close_journeys")

    # 3. trade_close_events orphans with invalid position_id (no managed_positions match)
    close_p_count, close_position_orphans, close_p_status = safe_orphan(
        close_position_orphans_query(), "trade_close_positions")

    return {
        "pnl_points": {
            "count": pnl_count,  # Note: Count reflects the sample or total if below limit
            "samples": pnl_orphans,
            "status": pnl_status,
        },
        "trade_close_journeys": {
            "count": close_j_count,
            "samples": close_journey_orphans,
            "status": close_j_status,
        },
        "trade_close_positions": {
            "count": close_p_count,
            "samples": close_position_orphans,
            "status": close_p_status,
        }
    }


def pnl_orphans_query():
    """SQL for signal_pnl_points orphans. Hoisted so the safe_orphan helper and
    tests can reference it without duplicating the literal."""
    return """
        SELECT p.id::text, p.ts::text, p.journey_id::text, p.symbol, p.pnl_percent::text
        FROM public.signal_pnl_points p
        LEFT JOIN public.signal_journeys j ON p.journey_id = j.id
        WHERE j.id IS NULL
        LIMIT 100;
    """


def close_journey_orphans_query():
    """SQL for trade_close_events orphans (journey_id set but no signal_journeys match)."""
    return """
        SELECT t.id::text, t.created_at::text, t.journey_id::text, t.symbol, t.pnl_percent::text
        FROM public.trade_close_events t
        LEFT JOIN public.signal_journeys j ON t.journey_id = j.id
        WHERE t.journey_id IS NOT NULL AND j.id IS NULL
        LIMIT 100;
    """


def close_position_orphans_query():
    """SQL for trade_close_events orphans with invalid position_id (no managed_positions match)."""
    return """
        SELECT t.id::text, t.created_at::text, t.position_id::text, t.symbol, t.pnl_percent::text
        FROM public.trade_close_events t
        LEFT JOIN public.managed_positions p ON t.position_id = p.id
        WHERE p.id IS NULL
        LIMIT 100;
    """


def _status_for_value(value):
    """Classify a metric status string for monitoring/alert routing."""
    if value in ("UNKNOWN", "TIMEOUT", "ERROR"):
        return "UNKNOWN"
    return "OK"


def inspect_queues():
    """Checks queue backlogs and measures pipeline starvation lags.

    Returns:
        dict: Queue backlog counts and starvation ages. Each numeric metric is
        accompanied by a `status` key ("OK" or "UNKNOWN"): if the underlying
        SQL query times out or errors (e.g. the DB is congested by a long-held
        lock), the metric is reported as "UNKNOWN" instead of crashing the whole
        monitor. This prevents a single congested query from masking the health
        of every other pipeline and stops the monitor from dying with a
        non-zero exit (t_d9c7537b hardening — see 2026-07-11 systemic RCA).

        NOTE on the finalizer_backlog query: a previous version referenced
        signal_journeys.clean_outcome, which does not exist on this schema and
        silently yielded 0 backlog (masking real starvation). t_d9c7537b fix:
        it previously LEFT JOINed ONLY the 'journey_finalizer' lane and counted
        any journey lacking THAT lane's row, but journeys finalized via other
        lanes (e.g. 'trade_close') already have a decision_outcomes row and are
        NOT pending finalizer work. We now exclude journeys that already have a
        decision_outcomes row from ANY lane — only genuinely-unfinalized
        journeys count as backlog. That LEFT JOIN over 2.4M signal_journeys x
        large decision_outcomes is the query most exposed to DB lock contention.
    """
    # Sentinel for "could not measure" — distinct from a real 0 backlog.
    UNKNOWN = "UNKNOWN"

    def safe_count(query, label):
        try:
            res = run_sql(query)
            return int(res[0]["backlog_count"]) if res else 0, "OK"
        except Exception as e:
            print(f"[WARN] inspect_queues.{label} query failed ({type(e).__name__}): {e}",
                  file=sys.stderr)
            return UNKNOWN, "UNKNOWN"

    def safe_lag(query, label):
        try:
            res = run_sql(query)
            return float(res[0]["lag"]) if res else -1.0, "OK"
        except Exception as e:
            print(f"[WARN] inspect_queues.{label} query failed ({type(e).__name__}): {e}",
                  file=sys.stderr)
            return UNKNOWN, "UNKNOWN"

    # 1. Finalizer Queue Backlog (active journeys whose finalizer-produced
    #    outcome has NOT yet landed). See long comment above for the t_d9c7537b fix.
    finalizer_backlog_query = """
        SELECT count(*)::integer AS backlog_count
        FROM public.signal_journeys sj
        LEFT JOIN public.decision_outcomes any_dec
               ON sj.correlation_id = any_dec.correlation_id
        WHERE sj.is_active = false
          AND sj.exit_type IS NOT NULL
          AND sj.exit_type != 'reconciliation'
          AND sj.realized_exit_price IS NOT NULL
          AND sj.realized_pnl_percent IS NOT NULL
          AND sj.bars_held >= 1
          AND any_dec.id IS NULL;
    """
    finalizer_backlog, fb_status = safe_count(finalizer_backlog_query, "finalizer_backlog")

    # 2. clean_outcome_binary_24h Backlog (Inactive since Epoch Start, mature (older than 24h), clean label is NULL)
    binary_backlog_query = f"""
        SELECT count(*)::integer AS backlog_count
        FROM public.signal_journeys
        WHERE created_at > '{EPOCH_START}'
          AND created_at <= now() - interval '24 hours'
          AND is_active = false
          AND clean_outcome_binary_24h IS NULL;
    """
    binary_backlog, bb_status = safe_count(binary_backlog_query, "binary_backlog")

    # 3. Time Since Last Update (Lag)
    finalizer_lag, fl_status = safe_lag("""
        SELECT COALESCE(EXTRACT(EPOCH FROM (now() - max(created_at)))/3600.0, -1.0) AS lag
        FROM public.decision_outcomes WHERE label_source = 'journey_finalizer';
    """, "finalizer_lag")
    closer_lag, cl_status = safe_lag("""
        SELECT COALESCE(EXTRACT(EPOCH FROM (now() - max(created_at)))/3600.0, -1.0) AS lag
        FROM public.decision_outcomes WHERE label_source = 'trade_close';
    """, "closer_lag")
    binary_lag, bl_status = safe_lag("""
        SELECT COALESCE(EXTRACT(EPOCH FROM (now() - max(updated_at)))/3600.0, -1.0) AS lag
        FROM public.signal_journeys WHERE clean_outcome_binary_24h IS NOT NULL;
    """, "binary_lag")

    return {
        "finalizer_backlog": finalizer_backlog,
        "binary_backlog": binary_backlog,
        "finalizer_lag_hours": finalizer_lag,
        "closer_lag_hours": closer_lag,
        "binary_lag_hours": binary_lag,
        "status": {
            "finalizer_backlog": _status_for_value(fb_status),
            "binary_backlog": _status_for_value(bb_status),
            "finalizer_lag_hours": _status_for_value(fl_status),
            "closer_lag_hours": _status_for_value(cl_status),
            "binary_lag_hours": _status_for_value(bl_status),
        }
    }


def inspect_dlq():
    """Inspects failed event deliveries in the event_dead_letter queue.

    Returns:
        dict: Count and details of DLQ events.
    """
    dlq_query = """
        SELECT id::text, event_type, failed_at::text, substring(error_message from 1 for 200) AS error
        FROM public.event_dead_letter
        WHERE resolved_at IS NULL
        ORDER BY failed_at DESC
        LIMIT 100;
    """
    dlq_events = run_sql(dlq_query)
    return {
        "count": len(dlq_events),
        "events": dlq_events
    }


def inspect_cohort_accrual():
    """Measures progress of unique clean journey samples since epoch start.

    Returns:
        dict: Accrual statistics and ETA projections.
    """
    # Total signals since epoch
    signals_query = f"""
        SELECT count(*)::integer AS count
        FROM public.signal_journeys
        WHERE created_at > '{EPOCH_START}';
    """
    signals_res = run_sql(signals_query)
    signals = int(signals_res[0]["count"]) if signals_res else 0

    # Clean finalized outcomes since epoch
    outcomes_query = f"""
        SELECT count(*)::integer AS count
        FROM public.decision_outcomes
        WHERE created_at > '{EPOCH_START}'
          AND is_final = true
          AND COALESCE(contaminated, false) = false;
    """
    outcomes_res = run_sql(outcomes_query)
    outcomes = int(outcomes_res[0]["count"]) if outcomes_res else 0

    # Clean realized closes since epoch
    closes_query = f"""
        SELECT count(*)::integer AS count
        FROM public.trade_close_events
        WHERE created_at > '{EPOCH_START}'
          AND COALESCE(contaminated, false) = false;
    """
    closes_res = run_sql(closes_query)
    closes = int(closes_res[0]["count"]) if closes_res else 0

    # Calculate signal accrual rate (daily)
    hours_query = f"""
        SELECT EXTRACT(EPOCH FROM (now() - '{EPOCH_START}'::timestamptz))/3600.0 AS hours;
    """
    hours_res = run_sql(hours_query)
    hours = float(hours_res[0]["hours"]) if hours_res else 1.0
    rate_day = (signals / hours * 24.0) if hours > 0 else 0.0

    return {
        "signals": signals,
        "outcomes": outcomes,
        "closes": closes,
        "hours_since_epoch": hours,
        "signals_per_day": rate_day,
        "progress_rebaseline_pct": min(100.0, (outcomes / TARGET_REBASELINE) * 100.0) if TARGET_REBASELINE else 0.0,
        "progress_research_pct": min(100.0, (outcomes / TARGET_RESEARCH) * 100.0) if TARGET_RESEARCH else 0.0
    }


# ----------------------------------------------------------------------------
# DIAGNOSTIC EVIDENCE EXPORTS
# ----------------------------------------------------------------------------

def collect_and_save_evidence(results, breaches):
    """Exports structured JSON/CSV logs of breaches to /tmp/data-integrity-diagnostics/"""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    # 1. Save main diagnostics report (JSON)
    report_path = EVIDENCE_DIR / f"diagnostics-report-{timestamp}.json"
    report_data = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat() + "Z",
        "epoch_start": EPOCH_START,
        "breaches_detected": breaches,
        "results": results
    }
    with open(report_path, "w") as f:
        json.dump(report_data, f, indent=2)

    exported_files = [str(report_path)]

    # 2. Save sample orphan signal PnL points (CSV)
    # Count may be "UNKNOWN" (query timeout) — never compare a str to 0.
    if isinstance(results["orphans"]["pnl_points"]["count"], int) and results["orphans"]["pnl_points"]["count"] > 0:
        pnl_path = EVIDENCE_DIR / f"orphan-signal-pnl-points-{timestamp}.csv"
        samples = results["orphans"]["pnl_points"]["samples"]
        if samples:
            with open(pnl_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=samples[0].keys())
                writer.writeheader()
                writer.writerows(samples)
            exported_files.append(str(pnl_path))

    # 3. Save sample orphan trade close events (CSV)
    j_count = results["orphans"]["trade_close_journeys"]["count"]
    p_count = results["orphans"]["trade_close_positions"]["count"]
    if (isinstance(j_count, int) and j_count > 0) or (isinstance(p_count, int) and p_count > 0):
        close_path = EVIDENCE_DIR / f"orphan-trade-close-events-{timestamp}.csv"
        samples = results["orphans"]["trade_close_journeys"]["samples"] + results["orphans"]["trade_close_positions"]["samples"]
        if samples:
            with open(close_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=samples[0].keys())
                writer.writeheader()
                writer.writerows(samples)
            exported_files.append(str(close_path))

    # 4. Save DLQ events (CSV)
    if results["dlq"]["count"] > 0:
        dlq_path = EVIDENCE_DIR / f"dlq-events-{timestamp}.csv"
        samples = results["dlq"]["events"]
        if samples:
            with open(dlq_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=samples[0].keys())
                writer.writeheader()
                writer.writerows(samples)
            exported_files.append(str(dlq_path))

    return exported_files


# ----------------------------------------------------------------------------
# CORE LOGIC & ALERTS
# ----------------------------------------------------------------------------

def evaluate_data_integrity():
    """Runs inspections and evaluates thresholds."""
    orphans = inspect_orphans()
    queues = inspect_queues()
    dlq = inspect_dlq()
    cohort = inspect_cohort_accrual()

    results = {
        "orphans": orphans,
        "queues": queues,
        "dlq": dlq,
        "cohort": cohort
    }

    breaches = []

    # Evaluate Orphan Rows
    # Orphan counts may be "UNKNOWN" (string sentinel) when the underlying
    # LEFT JOIN query timed out under DB lock contention. A non-measured
    # orphan count is NOT evidence of orphans, so skip the numeric comparison
    # rather than crashing on str > int (t_d9c7537b hardening, extended to
    # orphans on 2026-08-11 after a live statement timeout aborted the audit).
    if isinstance(orphans["pnl_points"]["count"], (int, float)) and orphans["pnl_points"]["count"] > THRESHOLD_ORPHANS:
        breaches.append({
            "type": "ORPHAN_ROWS",
            "metric": "signal_pnl_points_orphans",
            "value": orphans["pnl_points"]["count"],
            "threshold": THRESHOLD_ORPHANS,
            "message": f"Found {orphans['pnl_points']['count']} orphan rows in signal_pnl_points (unlinked to signal_journeys)."
        })
    if isinstance(orphans["trade_close_journeys"]["count"], (int, float)) and orphans["trade_close_journeys"]["count"] > THRESHOLD_ORPHANS:
        breaches.append({
            "type": "ORPHAN_ROWS",
            "metric": "trade_close_journeys_orphans",
            "value": orphans["trade_close_journeys"]["count"],
            "threshold": THRESHOLD_ORPHANS,
            "message": f"Found {orphans['trade_close_journeys']['count']} orphan rows in trade_close_events (unlinked to signal_journeys)."
        })
    if isinstance(orphans["trade_close_positions"]["count"], (int, float)) and orphans["trade_close_positions"]["count"] > THRESHOLD_ORPHANS:
        breaches.append({
            "type": "ORPHAN_ROWS",
            "metric": "trade_close_positions_orphans",
            "value": orphans["trade_close_positions"]["count"],
            "threshold": THRESHOLD_ORPHANS,
            "message": f"Found {orphans['trade_close_positions']['count']} orphan rows in trade_close_events (unlinked to managed_positions)."
        })

    # Evaluate Queue Backlogs
    # Guard against UNKNOWN (string sentinel from a failed/timeout query): a
    # non-measured metric is NOT a real backlog, so skip the numeric comparison
    # rather than crashing on str > int (t_d9c7537b hardening).
    if isinstance(queues["finalizer_backlog"], (int, float)) and queues["finalizer_backlog"] > THRESHOLD_FINALIZER_BACKLOG:
        breaches.append({
            "type": "QUEUE_BACKLOG",
            "metric": "finalizer_backlog",
            "value": queues["finalizer_backlog"],
            "threshold": THRESHOLD_FINALIZER_BACKLOG,
            "message": f"Journey finalizer queue backlog is too high: {queues['finalizer_backlog']} pending journeys (threshold: {THRESHOLD_FINALIZER_BACKLOG})."
        })
    if isinstance(queues["binary_backlog"], (int, float)) and queues["binary_backlog"] > THRESHOLD_BINARY_BACKLOG:
        breaches.append({
            "type": "QUEUE_BACKLOG",
            "metric": "binary_backlog",
            "value": queues["binary_backlog"],
            "threshold": THRESHOLD_BINARY_BACKLOG,
            "message": f"clean_outcome_binary_24h label backlog is too high: {queues['binary_backlog']} pending journeys (threshold: {THRESHOLD_BINARY_BACKLOG})."
        })

    # Evaluate Starved Pipelines (Lags)
    # A pipeline is only starved if it has pending backlog items that are not being processed.
    # If either lag or backlog is UNKNOWN (query failed), skip the starvation
    # evaluation for that pipeline — a failed measurement is NOT evidence of
    # starvation, and comparing UNKNOWN to a numeric threshold would raise a
    # spurious breach (t_d9c7537b hardening).
    qstatus = queues.get("status", {})
    if (qstatus.get("finalizer_lag_hours") == "OK" and qstatus.get("finalizer_backlog") == "OK"
            and queues["finalizer_lag_hours"] > THRESHOLD_STARVATION_LAG_HOURS
            and queues["finalizer_backlog"] > 0):
        breaches.append({
            "type": "PIPELINE_STARVATION",
            "metric": "finalizer_lag_hours",
            "value": queues["finalizer_lag_hours"],
            "threshold": THRESHOLD_STARVATION_LAG_HOURS,
            "message": f"Journey finalizer pipeline is starved: last write was {queues['finalizer_lag_hours']:.1f} hours ago (threshold: {THRESHOLD_STARVATION_LAG_HOURS}h)."
        })
    if (qstatus.get("binary_lag_hours") == "OK" and qstatus.get("binary_backlog") == "OK"
            and queues["binary_lag_hours"] > THRESHOLD_STARVATION_LAG_HOURS
            and queues["binary_backlog"] > 0):
        breaches.append({
            "type": "PIPELINE_STARVATION",
            "metric": "binary_lag_hours",
            "value": queues["binary_lag_hours"],
            "threshold": THRESHOLD_STARVATION_LAG_HOURS,
            "message": f"clean_outcome_binary_24h labeler pipeline is starved: last write was {queues['binary_lag_hours']:.1f} hours ago (threshold: {THRESHOLD_STARVATION_LAG_HOURS}h)."
        })

    # Monitor degradation: if any queue metric could not be measured (DB
    # congestion / lock contention / timeout), raise a single MONITOR_DEGRADED
    # breach so the incident is VISIBLE instead of being masked by a crash or
    # by the framework emitting a false "all healthy" exit 0 (t_d9c7537b).
    # Orphan-status UNKNOWNs (statement timeout on the heavy LEFT JOINs) are
    # folded into the same breach (extended 2026-08-11).
    unknown_metrics = [m for m, s in qstatus.items() if s == "UNKNOWN"]
    orphan_status = {
        "pnl_points": orphans["pnl_points"].get("status", "OK"),
        "trade_close_journeys": orphans["trade_close_journeys"].get("status", "OK"),
        "trade_close_positions": orphans["trade_close_positions"].get("status", "OK"),
    }
    for m, s in orphan_status.items():
        if s == "UNKNOWN":
            unknown_metrics.append(f"orphans.{m}")
    if unknown_metrics:
        breaches.append({
            "type": "MONITOR_DEGRADED",
            "metric": "queue_monitor_availability",
            "value": ",".join(unknown_metrics),
            "threshold": "all-OK",
            "message": (
                f"Data-quality monitor could not measure {len(unknown_metrics)} pipeline "
                f"metric(s) due to DB query failure (likely lock contention / congestion): "
                f"{', '.join(unknown_metrics)}. Investigate long-held locks on the trading DB "
                f"(e.g. pg_stat_activity wait_event='Lock'); do NOT treat as healthy."
            )
        })

    # Evaluate Dead-Letter Queue
    if dlq["count"] > THRESHOLD_DLQ_COUNT:
        breaches.append({
            "type": "DLQ_ERRORS",
            "metric": "dead_letter_queue_count",
            "value": dlq["count"],
            "threshold": THRESHOLD_DLQ_COUNT,
            "message": f"Found {dlq['count']} failed outcome events in event_dead_letter queue (failed finalization/labeling)."
        })

    return results, breaches


# ----------------------------------------------------------------------------
# KANBAN AUTO-ROUTING & DEDUPLICATION
# ----------------------------------------------------------------------------

def build_diag_idempotency_key(board_slug, error_type, metric_name):
    """Build the canonical deduplication key shared by the kanban kernel and this framework.

    Shape (per review t_ba1827ad §2.1 / §4.1):
        diag:<board>:<error_type>:<metric_name>
    e.g. diag:sycode-trading:PIPELINE_STARVATION:finalizer_lag_hours

    This MUST match the key passed to `hermes kanban create --idempotency-key`,
    otherwise the kernel-native dedup (return existing id instead of creating a
    duplicate) will not fire and cards will flood the board again.
    """
    return f"diag:{board_slug}:{error_type}:{metric_name}"


def query_last_task(idempotency_key):
    """Queries the active board database to find the most recent active/non-archived task for this idempotency key.

    Args:
        idempotency_key: canonical key from :func:`build_diag_idempotency_key`.

    Returns:
        tuple: (id, created_at, status) or None

    Board resolution: the board slug is decoded from the idempotency key
    (diag:<board>:<type>:<metric>) and used to resolve the SAME kanban.db the
    card was created in. This prevents the dedup lookup from hitting a
    different board than the one the card lives on (systemic fix t_4f419b25).
    """
    parts = idempotency_key.split(":")
    board_slug = parts[1] if len(parts) >= 2 else None
    db_path = resolve_board_db(board_slug)

    if not os.path.exists(db_path):
        return None
        
    try:
        conn = kb.connect(db_path=Path(db_path))
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, created_at, status FROM tasks
            WHERE idempotency_key = ? AND status != 'archived'
            ORDER BY created_at DESC LIMIT 1;
            """,
            (idempotency_key,)
        )
        row = cursor.fetchone()
        conn.close()
        return row
    except Exception as e:
        print(f"Warning: Failed to query kanban db ({db_path}) for deduplication: {e}", file=sys.stderr)
        return None


def bump_priority(task_id, board_name, priority):
    """Directly set a task's priority in the board DB (Tier-2 escalation).

    The kanban CLI `edit` verb only backfills DONE tasks (requires --result)
    and there is no CLI verb to mutate priority on a running task, so the
    framework performs the single-column UPDATE itself through the same
    board-resolved connection used for dedup lookups (systemic fix
    t_4f419b25: the DB is resolved from the board slug encoded in the
    canonical idempotency key, never ambient env).

    Args:
        task_id: existing active card id to bump.
        board_name: kanban board slug owning the card.
        priority: new priority int (e.g. 1 = Tier-2 escalation lane).

    Returns:
        bool: True on success.
    """
    db_path = resolve_board_db(board_name)
    if not os.path.exists(db_path):
        print(f"Warning: kanban db ({db_path}) missing; priority bump skipped", file=sys.stderr)
        return False
    try:
        conn = kb.connect(db_path=Path(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tasks SET priority = ? WHERE id = ?",
            (priority, task_id),
        )
        conn.commit()
        conn.close()
        print(f"[TIER2] Bumped card {task_id} priority to {priority} on {board_name}.")
        return True
    except Exception as e:
        print(f"[TIER2] WARNING: priority bump failed for {task_id}: {e}", file=sys.stderr)
        return False


def comment_diagnostic_update(task_id, b, board_name, delta_note=None, queue_backlog=None,
                               status="PERSISTENT_BREACH"):
    """Append a lightweight `[Diagnostic Update]` comment to an existing card instead of
    re-creating duplicate body content (review t_ba1827ad §2.2).

    Called only when the kernel-native idempotency check returned the existing
    task id (i.e. this breach was already on the board). This keeps the board
    free of duplicate diagnostic dumps while still recording the latest metric
    value and any change since the prior report.

    Args:
        task_id: existing active card id to update.
        b: breach dict (metric/type/value/threshold/message).
        board_name: kanban board slug to scope the comment.
        delta_note: optional human-readable change-vs-prior value.
        queue_backlog: optional pipeline backlog gauge to include in the update.
        status: status label (PERSISTENT_BREACH / NORMALIZED / etc).
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"[Diagnostic Update - {ts}]",
        f"- metric: {b.get('metric', '?')}",
        f"- error_type: {b.get('type', '?')}",
        f"- current_value: {b.get('value', '?')} (threshold: {b.get('threshold', '?')})",
    ]
    if delta_note:
        lines.append(f"- delta: {delta_note}")
    if queue_backlog is not None:
        lines.append(f"- queue_backlog: {queue_backlog}")
    lines.append(f"- status: {status}")
    body = "\n".join(lines)

    env = board_env(board_name)
    cmd = [
        "hermes", "kanban", "comment", task_id, body,
        "--author", "sycode-data-quality-framework",
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, env=env)
        print(f"Appended diagnostic update to existing card {task_id} for [{b.get('metric')}].")
    except subprocess.CalledProcessError as e:
        print(f"Error: Failed to comment on existing card {task_id}: {e.stderr}", file=sys.stderr)
    except Exception as e:
        print(f"Error: Unexpected failure while commenting on card {task_id}: {e}", file=sys.stderr)


def load_alert_state():
    """Read `~/.hermes/var/dq/alert_state.json` (review §4.1.4).

    Returns a dict keyed by metric -> {"last_sent_epoch": int}. Missing file or
    corrupt JSON yields a fresh empty dict; we never raise here because a bad
    state file must not break the diagnostic audit.
    """
    if not ALERT_STATE_PATH.exists():
        return {}
    try:
        with open(ALERT_STATE_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[ALERT] WARNING: failed to read {ALERT_STATE_PATH}, starting fresh: {e}", file=sys.stderr)
        return {}


def save_alert_state(state):
    """Atomically persist alert state (review §4.1.4).

    Read-write-safe: writes to a sibling temp file, then os.replace() into place
    so a crash mid-write can never leave a truncated/corrupt file. Only touches
    the `last_sent_epoch` per-metric keys; never deletes other keys (callers
    pass the existing dict through, mutated in place).
    """
    ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(ALERT_STATE_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, ALERT_STATE_PATH)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        print(f"[ALERT] WARNING: failed to persist {ALERT_STATE_PATH}: {e}", file=sys.stderr)


def send_discord_alert_cooled(metric, message, state, now=None):
    """Send a Discord alert for `metric`, enforcing a >=1h per-metric cooldown.

    Args:
        metric: metric name used as the cooldown key.
        message: alert text.
        state: mutable alert_state dict (persisted by caller via save_alert_state).
        now: optional epoch override (used by tests to simulate time).

    Returns:
        bool: True if a Discord alert was actually sent, False if throttled.
    """
    now = now if now is not None else time.time()
    last = state.get(metric, {}).get("last_sent_epoch", 0)
    if last and (now - last) < DISCORD_COOLDOWN_SECONDS:
        remaining = int(DISCORD_COOLDOWN_SECONDS - (now - last))
        print(f"[ALERT] Discord cooldown active for [{metric}]: {remaining}s remaining. Skipping send.")
        return False

    # Fire the alert via the Jarvis Discord integration (same pattern as dq_sentinel_healer).
    # The actual network send is isolated in `_dispatch_discord_alert` so the
    # `--test` suite can mock it (hermetic: no live Discord messages — Defect B).
    _dispatch_discord_alert(metric, message)

    state.setdefault(metric, {})["last_sent_epoch"] = now
    return True


def _dispatch_discord_alert(metric, message):
    """Send a Discord message to DISCORD_TARGET via the Jarvis profile.

    Isolated here so unit tests can patch this single symbol instead of the
    real `hermes send` subprocess (review t_97299572 Defect B: the test suite
    must never post live messages to #critical-alerts).
    """
    print(f"[ALERT] Sending Discord alert for [{metric}] -> {DISCORD_TARGET}")
    env = os.environ.copy()
    env["HERMES_HOME"] = "/home/frank/.hermes/profiles/jarvis"
    env["HERMES_PROFILE"] = "jarvis"
    try:
        res = subprocess.run(
            [HERMES_BIN, "send", "--to", DISCORD_TARGET, "--quiet", message],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if res.returncode != 0:
            print(f"[ALERT] WARNING: Discord delivery failed: {res.stderr.strip() or res.stdout.strip()}", file=sys.stderr)
    except Exception as e:
        print(f"[ALERT] WARNING: Discord delivery error: {e}", file=sys.stderr)


def is_a3_gate_breach(message):
    """Detect an A3 hard gate (credential/credit exhaustion) in an error string.

    A True result means the diagnostic must block + route to Frank and must NOT
    be retried automatically (review §5.2).
    """
    if not message:
        return False
    low = message.lower()
    return any(sig in low for sig in A3_GATE_SIGNATURES)


def escalate_a3(task_id, metric, message, board_name=None):
    """Tier 3: A3 gate hit -> block card (needs_input) + reassign to Frank (review §2.3/§5.2)."""
    global a3_gate_hit_this_run
    a3_gate_hit_this_run = True
    board_name = board_name or os.environ.get("HERMES_KANBAN_BOARD") or "sycode-trading"
    print(f"[TIER3] A3 gate breach for [{metric}] -> blocking + routing to Frank: {message[:160]}")
    env = board_env(board_name)
    try:
        subprocess.run(
            ["hermes", "kanban", "block", task_id,
             "--kind", "needs_input",
             f"A3 gate breach (credential/credit exhaustion) for metric {metric}: {message[:200]}. "
             f"Escalated to Frank; automated retries suppressed."],
            capture_output=True, text=True, check=True, env=env,
        )
    except Exception as e:
        print(f"[TIER3] WARNING: failed to block {task_id}: {e}", file=sys.stderr)
    try:
        subprocess.run(
            ["hermes", "kanban", "reassign", "--reclaim", task_id, TIER3_ASSIGNEE],
            capture_output=True, text=True, check=True, env=env,
        )
    except Exception as e:
        print(f"[TIER3] WARNING: failed to reassign {task_id} to Frank: {e}", file=sys.stderr)


def auto_close_diagnostic(task_id, metric, board_name):
    """Two consecutive healthy profiles -> kanban_complete the active card (review §2.2/§4.1.5)."""
    board_name = board_name or os.environ.get("HERMES_KANBAN_BOARD") or "sycode-trading"
    print(f"[AUTOCLOSE] Metric [{metric}] normalized; completing active diagnostic card {task_id} on {board_name}.")
    env = board_env(board_name)
    try:
        subprocess.run(
            ["hermes", "kanban", "complete", task_id,
             "--summary", f"Auto-closed by data-quality framework: metric {metric} returned under threshold for "
                          f"{AUTO_CLOSE_CONSECUTIVE_HEALTHY} consecutive healthy profiles."],
            capture_output=True, text=True, check=True, env=env,
        )
        return True
    except Exception as e:
        print(f"[AUTOCLOSE] WARNING: failed to complete {task_id}: {e}", file=sys.stderr)
        return False


def route_breaches_to_kanban(breaches, exported_files, state=None):
    """Auto-routes diagnostic cards with smart alerting (review t_ba1827ad §2.2/§2.3/§4).

    Behaviour:
      * Tier 1 (new card): priority 0, assign per ROUTING_MAP.
      * Existing active card (idempotency hit): append a lightweight
        `[Diagnostic Update]` comment instead of spawning a duplicate.
      * Discord alerts are throttled to >=1h per metric via alert_state.json.
      * Tier 2: breach persists >24h -> priority 1 + reassign trading-devops.
      * Tier 3: A3 gate (credential/credit) -> block + route to Frank, suppress retries.
      * Auto-close: returns a (metric -> consecutive_healthy) tracker so main()
        can complete the card after two consecutive healthy profiles.

    Args:
        breaches: list of breach dicts.
        exported_files: evidence paths for fresh-card bodies.
        state: mutable alert_state dict (loaded by caller, persisted after).
    """
    if state is None:
        state = load_alert_state()

    print(f"\n--- KANBAN AUTO-ROUTING (smart alerting) ---")

    # Assignee routing map (Tier 1 default lane per review §2.3/§4)
    ROUTING_MAP = {
        "ORPHAN_ROWS": "trading-data-oracle",
        "DLQ_ERRORS": "trading-data-oracle",
        "QUEUE_BACKLOG": "trading-devops",
        "PIPELINE_STARVATION": "trading-devops",
        # Monitor degradation (DB congestion / lock contention / query timeout):
        # the metric could not be measured at all. This needs a human to resolve
        # the underlying DB contention — route as needs_input, not an auto-closed
        # routine. t_d9c7537b hardening.
        "MONITOR_DEGRADED": "trading-devops",
    }

    routed_count = 0
    deduped_count = 0
    # Tracks per-metric consecutive-healthy profile count for auto-close (main() drives completion).
    healthy_tracker = {}

    board_name = os.environ.get("HERMES_KANBAN_BOARD") or "sycode-trading"

    for b in breaches:
        metric = b["metric"]
        b_type = b["type"]
        assignee = ROUTING_MAP.get(b_type, "trading-devops")

        # Reset healthy counter for any metric that is currently breaching.
        healthy_tracker[metric] = 0

        # A3 hard gate: credential/credit exhaustion -> Tier 3 immediately.
        if is_a3_gate_breach(b.get("message", "")):
            # Try to find an existing card to block; otherwise create one then block.
            idempotency_key = build_diag_idempotency_key(board_name, b_type, metric)
            last_task = query_last_task(idempotency_key)
            if last_task:
                task_id = last_task[0]
            else:
                task_id = _create_diag_card(b, assignee, idempotency_key, board_name, exported_files)
                if task_id:
                    routed_count += 1
            if task_id:
                escalate_a3(task_id, metric, b.get("message", ""), board_name=board_name)
                deduped_count += 1
            # A3 breaches never trigger Discord spam loops; still record cooldown timestamp.
            state.setdefault(metric, {})["last_sent_epoch"] = time.time()
            continue

        # Canonical idempotency key — MUST match the kernel's native dedup key so
        # re-triggering the same breach returns the existing card id (review §2.1).
        idempotency_key = build_diag_idempotency_key(board_name, b_type, metric)

        last_task = query_last_task(idempotency_key)
        if last_task:
            existing_id, created_at, status = last_task
            age_seconds = time.time() - created_at

            # Tier 2 escalation: sustained breach >24h -> priority 1 + reassign.
            if age_seconds > TIER2_ESCALATION_SECONDS and status not in ("blocked",):
                try:
                    bump_priority(existing_id, board_name, 1)
                    env2 = board_env(board_name)
                    subprocess.run(
                        ["hermes", "kanban", "reassign", "--reclaim", existing_id, TIER2_ASSIGNEE],
                        capture_output=True, text=True, check=True, env=env2,
                    )
                    print(f"[TIER2] Breach for [{metric}] persisted >24h ({age_seconds/3600:.1f}h); bumped to priority 1 + reassigned {TIER2_ASSIGNEE}.")
                except Exception as e:
                    print(f"[TIER2] WARNING: escalation failed for {existing_id}: {e}", file=sys.stderr)

            # Existing-card path: comment update, NOT a new card.
            queue_backlog = _extract_backlog(b_type, metric)
            print(f"Deduplicated: Card {existing_id} for [{metric}] active ({age_seconds/3600.0:.1f}h). Appending update.")
            comment_diagnostic_update(existing_id, b, board_name, queue_backlog=queue_backlog)
            deduped_count += 1

            # Throttled Discord alert (>=1h per metric) on persistent anomalies.
            send_discord_alert_cooled(
                metric,
                f"[DATA-QUALITY] PERSISTENT BREACH [{b_type}] {metric} = {b['value']} (threshold {b['threshold']})",
                state,
            )
            continue

        # Tier 1: fresh breach -> create card at priority 0.
        task_id = _create_diag_card(b, assignee, idempotency_key, board_name, exported_files, priority=0)
        if task_id:
            routed_count += 1
            # Monitor degradation requires a human to resolve underlying DB
            # lock contention; block the card as needs_input rather than leaving
            # it as a routine actionable (t_d9c7537b hardening).
            if b_type == "MONITOR_DEGRADED":
                try:
                    env3 = board_env(board_name)
                    subprocess.run(
                        ["hermes", "kanban", "block", task_id,
                         "--kind", "needs_input",
                         f"MONITOR_DEGRADED: {b['message']}"],
                        capture_output=True, text=True, check=True, env=env3,
                    )
                    print(f"[MONITOR_DEGRADED] Card {task_id} blocked as needs_input (DB contention).")
                except Exception as e:
                    print(f"[MONITOR_DEGRADED] WARNING: block failed for {task_id}: {e}", file=sys.stderr)
            # First alert on a new breach is allowed (no prior cooldown entry).
            send_discord_alert_cooled(
                metric,
                f"[DATA-QUALITY] NEW BREACH [{b_type}] {metric} = {b['value']} (threshold {b['threshold']})",
                state,
            )

    return routed_count, deduped_count, healthy_tracker


def _extract_backlog(b_type, metric):
    """Best-effort pipeline backlog gauge for the `[Diagnostic Update]` comment."""
    # The framework's live queue snapshot isn't threaded through route_breaches,
    # so we return a coarse value only when the breach type is queue-related.
    if b_type in ("QUEUE_BACKLOG", "PIPELINE_STARVATION") and "backlog" in metric:
        # metric names: finalizer_backlog / binary_backlog
        return metric
    return None


def _create_diag_card(b, assignee, idempotency_key, board_name, exported_files, priority=0):
    """Create a fresh diagnostic kanban card (kernel dedups on idempotency_key)."""
    metric = b["metric"]
    b_type = b["type"]
    title = f"DIAGNOSTIC: [{b_type}] {metric} threshold breach"
    evidence_list = "\n".join([f"- `{path}`" for path in exported_files])
    body = (
        f"### Data Integrity Breach Detected\n\n"
        f"**Metric:** `{metric}`\n"
        f"**Failure Type:** `{b_type}`\n"
        f"**Observed Value:** `{b['value']}` (Threshold: `{b['threshold']}`)\n\n"
        f"#### Details\n"
        f"{b['message']}\n\n"
        f"#### Diagnostic Evidence Files\n"
        f"{evidence_list}\n\n"
        f"**Action Required:** Inspect the evidence payloads in `/tmp/data-integrity-diagnostics/` "
        f"and resolve the underlying database or pipeline ingestion issue immediately.\n\n"
        f"*Automated diagnostic card generated by SycodeTrading Data Quality Framework.*"
    )
    env = board_env(board_name)
    cmd = [
        "hermes", "kanban", "create",
        title,
        "--assignee", assignee,
        "--idempotency-key", idempotency_key,
        "--priority", str(priority),
        "--body", body,
        "--json"
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True, env=env)
        task_data = json.loads(proc.stdout)
        task_id = task_data.get("id", "unknown")
        print(f"Auto-Routed card {task_id} assigned to {assignee} (priority {priority}) for [{metric}].")
        return task_id
    except subprocess.CalledProcessError as e:
        print(f"Error: Failed to run 'hermes kanban create' for metric {metric}: {e.stderr}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Error: Unexpected failure while creating card for metric {metric}: {e}", file=sys.stderr)
        return None



def enumerate_diag_board_dbs():
    """Yield (board_slug, db_path) pairs for every kanban board DB that may
    hold diag cards.

    Resolution is fully key-driven: diag cards are created per-board with the
    canonical idempotency key ``diag:<board>:<type>:<metric>``, so the board
    belongs to the KEY, not to ambient ``HERMES_KANBAN_DB`` /
    ``HERMES_KANBAN_BOARD`` env (systemic fix t_4f419b25 / R2 key-driven dedup).

    We scan deterministically: every per-board kanban.db under
    ``~/.hermes/kanban/boards/<slug>/kanban.db`` (plus the legacy flat
    ``~/.hermes/kanban.db``) and ask each for rows whose
    ``idempotency_key LIKE 'diag:%'``. The board slug is decoded from the key
    at query time (parts[1]), so callers never depend on an ambient
    board pin whose value may drift from the board the card was created on.

    Never reads HERMES_KANBAN_DB / HERMES_KANBAN_BOARD for resolution.
    """
    candidates = []
    # Per-board DBs (deterministic board dirs).
    if KANBAN_BOARDS_DIR.exists():
        for board_dir in sorted(KANBAN_BOARDS_DIR.iterdir()):
            if board_dir.is_dir():
                db = board_dir / "kanban.db"
                if db.exists():
                    candidates.append((board_dir.name, str(db)))
    # Legacy flat DB (single copy, no per-board subdir).
    legacy = Path(os.path.expanduser("~/.hermes/kanban.db"))
    if legacy.exists():
        candidates.append(("sycode-trading", str(legacy)))
    return candidates


def query_active_diag_cards(priority=None, board_slugs=None):
    """Return all active (non-archived) diagnostic cards keyed by idempotency_key.

    Used by auto-close: when no breaches are detected we look up the previously
    active diag cards (optionally filtered to a priority lane) to complete them
    after two consecutive healthy profiles (review §2.2/§4.1.5).

    Board resolution: cards are created per-board with the canonical key
    ``diag:<board>:<type>:<metric>``, so the board is decoded from the KEY,
    never from ambient ``HERMES_KANBAN_BOARD`` / ``HERMES_KANBAN_DB`` env. Each
    returned card carries a ``board`` field sourced from its key
    (``parts[1]``), so ``process_auto_close`` can route the completion to the
    correct board DB without any env dependency (systemic fix t_4f419b25 / R2).

    Args:
        priority: optional int lane filter applied per-DB.
        board_slugs: optional iterable restricting which board dirs to scan.
            When None (default) all boards under KANBAN_BOARDS_DIR are scanned.
            Ambient env is never consulted for resolution.
    """
    results = []
    for board_slug, db_path in enumerate_diag_board_dbs():
        if board_slugs is not None and board_slug not in board_slugs:
            continue
        if not os.path.exists(db_path):
            continue
        try:
            conn = kb.connect(db_path=Path(db_path))
            cursor = conn.cursor()
            if priority is None:
                cursor.execute(
                    "SELECT id, idempotency_key, priority FROM tasks "
                    "WHERE idempotency_key LIKE 'diag:%' "
                    "AND status != 'archived' AND status != 'done'"
                )
            else:
                cursor.execute(
                    "SELECT id, idempotency_key, priority FROM tasks "
                    "WHERE idempotency_key LIKE 'diag:%' "
                    "AND status != 'archived' AND status != 'done' "
                    "AND priority = ?",
                    (priority,),
                )
            rows = cursor.fetchall()
            conn.close()
            for r in rows:
                key = r[1]
                parts = key.split(":")
                # diag:<board>:<type>:<metric> — board is parts[1]; if the key
                # is malformed we fall back to the DB's own board dir name.
                key_board = parts[1] if len(parts) >= 4 else board_slug
                results.append({
                    "id": r[0],
                    "idempotency_key": key,
                    "priority": r[2],
                    "board": key_board,
                })
        except Exception as e:
            print(f"Warning: failed to query active diag cards on {board_slug} ({db_path}): {e}", file=sys.stderr)
    return results


def process_auto_close(metric_ids, state, now=None):
    """Complete active diag cards whose metric has been healthy for >=N consecutive profiles.

    Args:
        metric_ids: dict metric_name -> consecutive_healthy count (0 means
            breaching this profile).
        state: shared alert_state dict (loaded/persisted by caller). The
            consecutive-healthy counter lives under the dedicated
            ``state["__healthy__"]`` sub-dict so it NEVER collides with the
            per-metric Discord cooldown entries ``state[metric] = {"last_sent_epoch": int}``
            (review t_97299572 Defect A1: previously the same dict was used for
            both, so ``state.get(metric, 0)`` returned a dict and ``dict + 1``
            raised TypeError, silently killing auto-close in production).
        now: optional epoch (unused for logic, reserved for test symmetry).

    Returns:
        list of task ids completed this call.
    """
    now = now if now is not None else time.time()
    completed = []
    active = query_active_diag_cards()
    # Map idempotency_key -> card for lookup.
    active_by_key = {c["idempotency_key"]: c for c in active}

    # Dedicated healthy counter sub-dict; never touches cooldown entries.
    healthy = state.setdefault("__healthy__", {})

    for metric, consecutive in metric_ids.items():
        key = None
        # Match any active diag card whose key ends with this metric.
        for akey, card in active_by_key.items():
            if akey.endswith(f":{metric}"):
                key = akey
                break
        if key is None:
            # No active card for this metric; ensure counter reset so a future
            # breach starts the two-healthy countdown fresh.
            healthy[metric] = 0
            continue

        prev = healthy.get(metric, 0)
        new_count = prev + (consecutive if consecutive > 0 else 0)
        # If a breach is present this profile, consecutive == 0 -> reset counter.
        if consecutive == 0:
            new_count = 0
        healthy[metric] = new_count

        if new_count >= AUTO_CLOSE_CONSECUTIVE_HEALTHY:
            key_parts = key.split(":")
            board_slug = key_parts[1] if len(key_parts) >= 2 else None
            if auto_close_diagnostic(active_by_key[key]["id"], metric, board_slug):
                completed.append(active_by_key[key]["id"])
                healthy[metric] = 0
    return completed


# ----------------------------------------------------------------------------
# UNIT TESTS (WITH MOCK DB RESPONSES)
# ----------------------------------------------------------------------------

class TestDataQualityFramework(unittest.TestCase):

    @patch("__main__.run_sql")
    def test_inspect_orphans_no_orphans(self, mock_run_sql):
        mock_run_sql.return_value = []  # Empty responses -> no orphans
        results = inspect_orphans()
        self.assertEqual(results["pnl_points"]["count"], 0)
        self.assertEqual(results["trade_close_journeys"]["count"], 0)
        self.assertEqual(results["trade_close_positions"]["count"], 0)

    @patch("__main__.run_sql")
    def test_inspect_orphans_with_orphans(self, mock_run_sql):
        # Setup mock returns for the 3 queries in inspect_orphans
        mock_run_sql.side_effect = [
            [{"id": "uuid1", "ts": "2026-07-06 12:00:00", "journey_id": "j1", "symbol": "BTC", "pnl_percent": "0.1"}],
            [{"id": "uuid2", "created_at": "2026-07-06 12:00:00", "journey_id": "j2", "symbol": "ETH", "pnl_percent": "-0.2"}],
            []
        ]
        results = inspect_orphans()
        self.assertEqual(results["pnl_points"]["count"], 1)
        self.assertEqual(results["trade_close_journeys"]["count"], 1)
        self.assertEqual(results["trade_close_positions"]["count"], 0)
        self.assertEqual(results["pnl_points"]["samples"][0]["symbol"], "BTC")

    @patch("__main__.run_sql")
    def test_inspect_orphans_db_timeout_marked_unknown(self, mock_run_sql):
        """An orphan-query timeout must be reported as UNKNOWN, not crash the
        monitor (t_d9c7537b hardening extended to orphans 2026-08-11: a live
        statement timeout on the heavy LEFT JOIN previously aborted the whole
        audit with FATAL before kanban routing could happen)."""
        def side_effect(query):
            if "signal_pnl_points" in query:
                raise TimeoutError("psql timed out after 120 seconds")
            return []
        mock_run_sql.side_effect = side_effect
        results = inspect_orphans()
        self.assertEqual(results["pnl_points"]["count"], "UNKNOWN")
        self.assertEqual(results["pnl_points"]["status"], "UNKNOWN")
        # Unaffected orphan metrics still measured normally.
        self.assertEqual(results["trade_close_journeys"]["count"], 0)
        self.assertEqual(results["trade_close_journeys"]["status"], "OK")
        self.assertEqual(results["trade_close_positions"]["count"], 0)

    @patch("__main__.run_sql")
    def test_inspect_queues_healthy(self, mock_run_sql):
        mock_run_sql.side_effect = [
            [{"backlog_count": "5"}],   # Finalizer backlog
            [{"backlog_count": "12"}],  # Binary backlog
            [{"lag": "1.5"}],           # Finalizer lag
            [{"lag": "2.1"}],           # Closer lag
            [{"lag": "3.0"}]            # Binary lag
        ]
        results = inspect_queues()
        self.assertEqual(results["finalizer_backlog"], 5)
        self.assertEqual(results["binary_backlog"], 12)
        self.assertEqual(results["finalizer_lag_hours"], 1.5)
        self.assertEqual(results["binary_lag_hours"], 3.0)

    @patch("__main__.run_sql")
    def test_inspect_queues_starved_and_backlogged(self, mock_run_sql):
        mock_run_sql.side_effect = [
            [{"backlog_count": "120"}],  # Finalizer backlog (breach!)
            [{"backlog_count": "650"}],  # Binary backlog (breach!)
            [{"lag": "36.5"}],           # Finalizer lag (breach!)
            [{"lag": "42.0"}],           # Closer lag
            [{"lag": "48.0"}]            # Binary lag (breach!)
        ]
        results = inspect_queues()
        self.assertEqual(results["finalizer_backlog"], 120)
        self.assertEqual(results["binary_backlog"], 650)
        self.assertEqual(results["finalizer_lag_hours"], 36.5)
        self.assertEqual(results["binary_lag_hours"], 48.0)
        # Each metric must carry a status key (all OK when queries succeed).
        self.assertEqual(results["status"]["finalizer_backlog"], "OK")
        self.assertEqual(results["status"]["binary_lag_hours"], "OK")

    @patch("__main__.run_sql")
    def test_inspect_queues_db_timeout_marked_unknown(self, mock_run_sql):
        """A query timeout/error must be reported as UNKNOWN, not crash the monitor."""
        # First query (finalizer_backlog) raises -> UNKNOWN; rest succeed.
        def side_effect(query):
            if "LEFT JOIN public.decision_outcomes" in query:
                raise TimeoutError("psql timed out after 120 seconds")
            return [{"backlog_count": "5"}]
        mock_run_sql.side_effect = side_effect
        results = inspect_queues()
        self.assertEqual(results["finalizer_backlog"], "UNKNOWN")
        self.assertEqual(results["status"]["finalizer_backlog"], "UNKNOWN")
        # Unaffected metrics still measured normally.
        self.assertEqual(results["binary_backlog"], 5)
        self.assertEqual(results["status"]["binary_backlog"], "OK")

    @patch("__main__.inspect_orphans")
    @patch("__main__.inspect_queues")
    @patch("__main__.inspect_dlq")
    @patch("__main__.inspect_cohort_accrual")
    @patch("__main__.collect_and_save_evidence")
    def test_evaluate_monitor_degraded_on_unknown(self, mock_save, mock_cohort, mock_dlq, mock_queues, mock_orphans):
        """When queue metrics are UNKNOWN (DB down), a MONITOR_DEGRADED breach surfaces."""
        mock_orphans.return_value = {
            "pnl_points": {"count": 0, "samples": []},
            "trade_close_journeys": {"count": 0, "samples": []},
            "trade_close_positions": {"count": 0, "samples": []},
        }
        mock_queues.return_value = {
            "finalizer_backlog": "UNKNOWN",
            "binary_backlog": "UNKNOWN",
            "finalizer_lag_hours": "UNKNOWN",
            "closer_lag_hours": "UNKNOWN",
            "binary_lag_hours": "UNKNOWN",
            "status": {
                "finalizer_backlog": "UNKNOWN", "binary_backlog": "UNKNOWN",
                "finalizer_lag_hours": "UNKNOWN", "closer_lag_hours": "UNKNOWN",
                "binary_lag_hours": "UNKNOWN",
            },
        }
        mock_dlq.return_value = {"count": 0, "events": []}
        mock_cohort.return_value = {
            "signals": 1, "outcomes": 1, "closes": 1, "hours_since_epoch": 1, "signals_per_day": 24,
            "progress_rebaseline_pct": 1.0, "progress_research_pct": 0.3,
        }
        mock_save.return_value = []
        results, breaches = evaluate_data_integrity()
        breach_types = [b["type"] for b in breaches]
        self.assertIn("MONITOR_DEGRADED", breach_types)
        degraded = [b for b in breaches if b["type"] == "MONITOR_DEGRADED"][0]
        self.assertIn("finalizer_backlog", degraded["value"])
        # Starvation must NOT be falsely raised from UNKNOWN metrics.
        self.assertNotIn("PIPELINE_STARVATION", breach_types)

    @patch("__main__.inspect_orphans")
    @patch("__main__.inspect_queues")
    @patch("__main__.inspect_dlq")
    @patch("__main__.inspect_cohort_accrual")
    @patch("__main__.collect_and_save_evidence")
    def test_evaluate_monitor_degraded_on_orphan_unknown(self, mock_save, mock_cohort, mock_dlq, mock_queues, mock_orphans):
        """Orphan UNKNOWN (LEFT JOIN timeout) folds into MONITOR_DEGRADED and
        never raises a false ORPHAN_ROWS breach or a crash (extended 2026-08-11)."""
        mock_orphans.return_value = {
            "pnl_points": {"count": "UNKNOWN", "samples": [], "status": "UNKNOWN"},
            "trade_close_journeys": {"count": 0, "samples": [], "status": "OK"},
            "trade_close_positions": {"count": 0, "samples": [], "status": "OK"},
        }
        mock_queues.return_value = {
            "finalizer_backlog": 0, "binary_backlog": 0,
            "finalizer_lag_hours": 0.5, "closer_lag_hours": 0.1, "binary_lag_hours": 0.2,
            "status": {m: "OK" for m in ("finalizer_backlog", "binary_backlog",
                                         "finalizer_lag_hours", "closer_lag_hours", "binary_lag_hours")},
        }
        mock_dlq.return_value = {"count": 0, "events": []}
        mock_cohort.return_value = {
            "signals": 1, "outcomes": 1, "closes": 1, "hours_since_epoch": 1, "signals_per_day": 24,
            "progress_rebaseline_pct": 1.0, "progress_research_pct": 0.3,
        }
        mock_save.return_value = []
        results, breaches = evaluate_data_integrity()
        breach_types = [b["type"] for b in breaches]
        self.assertIn("MONITOR_DEGRADED", breach_types)
        degraded = [b for b in breaches if b["type"] == "MONITOR_DEGRADED"][0]
        self.assertIn("orphans.pnl_points", degraded["value"])
        # UNKNOWN orphan count must NOT be compared numerically (no ORPHAN_ROWS).
        self.assertNotIn("ORPHAN_ROWS", breach_types)

    @patch("__main__.EVIDENCE_DIR")
    def test_collect_evidence_unknown_orphans_no_crash(self, mock_evidence_dir):
        """Evidence export must not crash when orphan counts are UNKNOWN (strings)."""
        import tempfile
        from pathlib import Path as _P
        tmp = _P(tempfile.mkdtemp(prefix="dq-ev-unknown-test-"))
        mock_evidence_dir.__truediv__ = lambda self, name: tmp / name
        results = {
            "orphans": {
                "pnl_points": {"count": "UNKNOWN", "samples": []},
                "trade_close_journeys": {"count": "UNKNOWN", "samples": []},
                "trade_close_positions": {"count": 0, "samples": []},
            },
            "queues": {}, "dlq": {"count": 0, "events": []}, "cohort": {},
        }
        breaches = [{"type": "MONITOR_DEGRADED", "metric": "queue_monitor_availability",
                     "value": "orphans.pnl_points", "threshold": "all-OK", "message": "x"}]
        exported = collect_and_save_evidence(results, breaches)
        self.assertTrue(any("diagnostics-report" in p for p in exported))
        # No orphan CSV exported for UNKNOWN counts.
        self.assertFalse(any("orphan-signal" in p or "orphan-trade" in p for p in exported))
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    @patch("__main__.run_sql")
    def test_inspect_dlq(self, mock_run_sql):
        mock_run_sql.return_value = [
            {"id": "e1", "event_type": "DECISION_OUTCOME_FINALIZED", "failed_at": "2026-07-06 10:00:00", "error": "Invalid payload"}
        ]
        results = inspect_dlq()
        self.assertEqual(results["count"], 1)
        self.assertEqual(results["events"][0]["event_type"], "DECISION_OUTCOME_FINALIZED")

    @patch("__main__.run_sql")
    def test_inspect_cohort_accrual(self, mock_run_sql):
        mock_run_sql.side_effect = [
            [{"count": "1500"}],   # Signals
            [{"count": "45"}],     # Outcomes (progress 45 / 100)
            [{"count": "38"}],     # Closes
            [{"hours": "48.0"}]    # Hours since epoch (rate = 1500/48 * 24 = 750/day)
        ]
        results = inspect_cohort_accrual()
        self.assertEqual(results["signals"], 1500)
        self.assertEqual(results["outcomes"], 45)
        self.assertEqual(results["closes"], 38)
        self.assertEqual(results["signals_per_day"], 750.0)
        self.assertEqual(results["progress_rebaseline_pct"], 45.0)

    @patch("__main__.inspect_orphans")
    @patch("__main__.inspect_queues")
    @patch("__main__.inspect_dlq")
    @patch("__main__.inspect_cohort_accrual")
    @patch("__main__.collect_and_save_evidence")
    def test_evaluate_data_integrity_breaches(self, mock_save, mock_cohort, mock_dlq, mock_queues, mock_orphans):
        # Configure mocks to return a starving, backlogged, orphaned state
        mock_orphans.return_value = {
            "pnl_points": {"count": 10, "samples": [{"id": "uuid"}]},
            "trade_close_journeys": {"count": 0, "samples": []},
            "trade_close_positions": {"count": 5, "samples": [{"id": "uuid"}]}
        }
        mock_queues.return_value = {
            "finalizer_backlog": 10,
            "binary_backlog": 10,
            "finalizer_lag_hours": 31.5,   # Starvation breach!
            "closer_lag_hours": 12.0,
            "binary_lag_hours": 42.0,       # Starvation breach!
            "status": {                      # inspect_queues() now returns per-metric status
                "finalizer_backlog": "OK",
                "binary_backlog": "OK",
                "finalizer_lag_hours": "OK",
                "closer_lag_hours": "OK",
                "binary_lag_hours": "OK",
            }
        }
        mock_dlq.return_value = {
            "count": 193,                  # DLQ breach!
            "events": [{"id": "e1"}]
        }
        mock_cohort.return_value = {
            "signals": 14000, "outcomes": 10, "closes": 10, "hours_since_epoch": 23.0, "signals_per_day": 14000/23*24,
            "progress_rebaseline_pct": 10.0, "progress_research_pct": 3.3
        }
        mock_save.return_value = ["/tmp/report.json", "/tmp/dlq.csv"]

        results, breaches = evaluate_data_integrity()

        # Check breach detections
        breach_types = [b["type"] for b in breaches]
        self.assertIn("ORPHAN_ROWS", breach_types)
        self.assertIn("PIPELINE_STARVATION", breach_types)
        self.assertIn("DLQ_ERRORS", breach_types)
        self.assertEqual(len(breaches), 5)  # 2 orphans, 2 starvations, 1 DLQ

    @patch("os.path.exists")
    @patch("__main__.kb.connect")
    def test_query_last_task_exists(self, mock_connect, mock_exists):
        mock_exists.return_value = True
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = ("t_test_123", 1234567890, "todo")

        key = build_diag_idempotency_key("sycode-trading", "PIPELINE_STARVATION", "finalizer_lag_hours")
        self.assertEqual(key, "diag:sycode-trading:PIPELINE_STARVATION:finalizer_lag_hours")

        row = query_last_task(key)
        self.assertEqual(row, ("t_test_123", 1234567890, "todo"))
        mock_cursor.execute.assert_called_once()
        sql_arg = mock_cursor.execute.call_args[0][0]
        params_arg = mock_cursor.execute.call_args[0][1]
        self.assertIn("idempotency_key = ?", sql_arg)
        self.assertEqual(params_arg, ("diag:sycode-trading:PIPELINE_STARVATION:finalizer_lag_hours",))

    @patch("__main__.send_discord_alert_cooled", return_value=True)
    @patch("__main__.comment_diagnostic_update")
    @patch("__main__.query_last_task")
    @patch("subprocess.run")
    def test_route_breaches_update_dont_duplicate(self, mock_run, mock_query, mock_comment, mock_discord):
        """Canonical-key dedup: active card hit -> comment instead of create (no duplicate)."""
        mock_query.return_value = ("t_existing_1", time.time() - 600, "running")

        breaches = [{
            "type": "PIPELINE_STARVATION",
            "metric": "finalizer_lag_hours",
            "value": 35.5,
            "threshold": 24.0,
            "message": "Starved."
        }]
        # Force board slug used by the function
        old_board = os.environ.get("HERMES_KANBAN_BOARD")
        os.environ["HERMES_KANBAN_BOARD"] = "sycode-trading"
        try:
            routed, deduped, _h = route_breaches_to_kanban(breaches, ["/tmp/report.json"])
        finally:
            if old_board is None:
                os.environ.pop("HERMES_KANBAN_BOARD", None)
            else:
                os.environ["HERMES_KANBAN_BOARD"] = old_board

        self.assertEqual(routed, 0)
        self.assertEqual(deduped, 1)
        mock_run.assert_not_called()                      # no new card
        mock_comment.assert_called_once()                 # update appended instead
        # Comment target must be the existing card id
        self.assertEqual(mock_comment.call_args[0][0], "t_existing_1")

    @patch("__main__.send_discord_alert_cooled", return_value=True)
    @patch("__main__.query_last_task")
    @patch("subprocess.run")
    def test_route_breaches_creates_canonical_key(self, mock_run, mock_query, mock_discord):
        """Fresh breach -> kanban create invoked with canonical diag:<board>:<type>:<metric> key."""
        mock_query.return_value = None
        mock_proc = MagicMock()
        mock_proc.stdout = '{"id": "t_created_999"}'
        mock_run.return_value = mock_proc

        breaches = [{
            "type": "DLQ_ERRORS",
            "metric": "dead_letter_queue_count",
            "value": 193,
            "threshold": 0,
            "message": "DLQ breach."
        }]
        old_board = os.environ.get("HERMES_KANBAN_BOARD")
        os.environ["HERMES_KANBAN_BOARD"] = "sycode-trading"
        try:
            routed, deduped, _h = route_breaches_to_kanban(breaches, ["/tmp/report.json"])
        finally:
            if old_board is None:
                os.environ.pop("HERMES_KANBAN_BOARD", None)
            else:
                os.environ["HERMES_KANBAN_BOARD"] = old_board

        self.assertEqual(routed, 1)
        self.assertEqual(deduped, 0)
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertEqual(args[0:3], ["hermes", "kanban", "create"])
        self.assertIn("--idempotency-key", args)
        key_arg = args[args.index("--idempotency-key") + 1]
        self.assertEqual(key_arg, "diag:sycode-trading:DLQ_ERRORS:dead_letter_queue_count")

    @patch("__main__.send_discord_alert_cooled", return_value=True)
    @patch("__main__.comment_diagnostic_update")
    @patch("__main__.query_last_task")
    @patch("subprocess.run")
    def test_route_breaches_to_kanban_deduplicated(self, mock_run, mock_query, mock_comment, mock_discord):
        # Last task created 10 minutes ago
        mock_query.return_value = ("t_existing_1", time.time() - 600, "running")
        
        breaches = [{
            "type": "ORPHAN_ROWS",
            "metric": "signal_pnl_points_orphans",
            "value": 10,
            "threshold": 0,
            "message": "Found 10 orphan rows."
        }]
        
        routed, deduped, _h = route_breaches_to_kanban(breaches, ["/tmp/report.json"])
        self.assertEqual(routed, 0)
        self.assertEqual(deduped, 1)
        # No NEW card was created for the existing breach...
        mock_run.assert_not_called()
        # ...instead a lightweight metric update was appended to the existing card.
        mock_comment.assert_called_once()
        self.assertEqual(mock_comment.call_args[0][0], "t_existing_1")

    @patch("__main__.send_discord_alert_cooled", return_value=True)
    @patch("__main__.query_last_task")
    @patch("subprocess.run")
    def test_route_breaches_to_kanban_creates_task(self, mock_run, mock_query, mock_discord):
        # No last task
        mock_query.return_value = None
        
        mock_proc = MagicMock()
        mock_proc.stdout = '{"id": "t_created_999"}'
        mock_run.return_value = mock_proc
        
        breaches = [{
            "type": "ORPHAN_ROWS",
            "metric": "signal_pnl_points_orphans",
            "value": 15,
            "threshold": 0,
            "message": "Found 15 orphan rows."
        }]
        
        routed, deduped, _h = route_breaches_to_kanban(breaches, ["/tmp/report.json"])
        self.assertEqual(routed, 1)
        self.assertEqual(deduped, 0)
        
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertEqual(args[0], "hermes")
        self.assertEqual(args[1], "kanban")
        self.assertEqual(args[2], "create")
        self.assertIn("--assignee", args)
        assignee_idx = args.index("--assignee")
        self.assertEqual(args[assignee_idx + 1], "trading-data-oracle")  # Routed to oracle

    @patch("subprocess.run")
    def test_self_heal_success(self, mock_run):
        # Configure subprocess mocks
        mock_run.return_value = MagicMock(stdout="Success", returncode=0)
        
        breaches = [
            {"type": "ORPHAN_ROWS", "metric": "signal_pnl_points_orphans"},
            {"type": "DLQ_ERRORS", "metric": "dead_letter_queue_count"},
            {"type": "QUEUE_BACKLOG", "metric": "binary_backlog"}
        ]
        
        healed = self_heal(breaches)
        self.assertIn("ORPHAN_ROWS", healed)
        self.assertIn("DLQ_ERRORS", healed)
        self.assertIn("QUEUE_BACKLOG", healed)
        self.assertEqual(mock_run.call_count, 3)

    # ------------------------------------------------------------------
    # SMART ALERTING & DISCORD COOLDOWN TESTS (review t_ba1827ad §2.2/§2.3/§4)
    # ------------------------------------------------------------------

    @patch("__main__._dispatch_discord_alert")
    def test_discord_cooldown_throttles_rapid_triggers(self, mock_dispatch):
        """>=1h per-metric cooldown: only the first of rapid triggers actually sends.

        Hermetic (review t_97299572 Defect B): `_dispatch_discord_alert` — the
        real `hermes send` subprocess call — is mocked, so NO live Discord
        message is ever posted to #critical-alerts during the test run.
        """
        state = {}
        now = 1_000_000.0
        # First send is allowed (no prior entry).
        sent1 = send_discord_alert_cooled("finalizer_backlog", "breach", state, now=now)
        self.assertTrue(sent1)
        # Immediate second trigger is throttled (< 1h).
        sent2 = send_discord_alert_cooled("finalizer_backlog", "breach", state, now=now + 10)
        self.assertFalse(sent2)
        # 59m later still throttled.
        sent3 = send_discord_alert_cooled("finalizer_backlog", "breach", state, now=now + 3540)
        self.assertFalse(sent3)
        # 1h+ later allowed again.
        sent4 = send_discord_alert_cooled("finalizer_backlog", "breach", state, now=now + 3601)
        self.assertTrue(sent4)
        # Different metric is independently allowed.
        sent5 = send_discord_alert_cooled("binary_backlog", "breach", state, now=now + 3602)
        self.assertTrue(sent5)
        # Exactly three dispatches occurred (the three allowed sends: first,
        # after-1h, and the independent second metric); the two throttled
        # triggers must NOT have performed a network send.
        self.assertEqual(mock_dispatch.call_count, 3)

    def test_alert_state_atomic_persist_preserves_other_keys(self):
        """save_alert_state must never delete other keys (read-write-safe)."""
        ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ALERT_STATE_PATH.write_text(json.dumps({"other_metric": {"last_sent_epoch": 5}}))
        try:
            state = load_alert_state()
            self.assertIn("other_metric", state)
            state["finalizer_backlog"] = {"last_sent_epoch": 999}
            save_alert_state(state)
            reloaded = load_alert_state()
            self.assertIn("other_metric", reloaded)        # untouched
            self.assertEqual(reloaded["other_metric"]["last_sent_epoch"], 5)
            self.assertEqual(reloaded["finalizer_backlog"]["last_sent_epoch"], 999)
            # Atomic: the live file (not a .tmp) holds the data.
            self.assertFalse(list(ALERT_STATE_PATH.parent.glob("*.tmp")))
        finally:
            if ALERT_STATE_PATH.exists():
                ALERT_STATE_PATH.unlink()

    def test_a3_gate_detection(self):
        """Credential/credit exhaustion strings are flagged as A3 hard gates."""
        self.assertTrue(is_a3_gate_breach("HTTP 401 Unauthorized: invalid api key"))
        self.assertTrue(is_a3_gate_breach("Rate limit exceeded (quota exhausted)"))
        self.assertTrue(is_a3_gate_breach("credential auth failed"))
        self.assertFalse(is_a3_gate_breach("finalizer backlog is too high"))
        self.assertFalse(is_a3_gate_breach(""))

    @patch("__main__.escalate_a3")
    @patch("__main__.query_last_task")
    def test_a3_route_blocks_and_routes_to_frank(self, mock_query, mock_escalate):
        """Tier 3: A3 breach -> block + route to Frank, no new card if active exists."""
        mock_query.return_value = ("t_a3_1", time.time() - 100, "running")
        breaches = [{
            "type": "DLQ_ERRORS", "metric": "dead_letter_queue_count",
            "value": 1, "threshold": 0,
            "message": "Failed: HTTP 401 Unauthorized (expired token)",
        }]
        old_board = os.environ.get("HERMES_KANBAN_BOARD")
        os.environ["HERMES_KANBAN_BOARD"] = "sycode-trading"
        try:
            routed, deduped, _h = route_breaches_to_kanban(breaches, ["/tmp/report.json"])
        finally:
            if old_board is None:
                os.environ.pop("HERMES_KANBAN_BOARD", None)
            else:
                os.environ["HERMES_KANBAN_BOARD"] = old_board
        self.assertEqual(routed, 0)
        self.assertEqual(deduped, 1)
        mock_escalate.assert_called_once()
        # escalate_a3 was invoked with the existing card id + metric.
        self.assertEqual(mock_escalate.call_args[0][0], "t_a3_1")
        self.assertEqual(mock_escalate.call_args[0][1], "dead_letter_queue_count")

    @patch("__main__._dispatch_discord_alert")
    @patch("__main__.comment_diagnostic_update")
    @patch("__main__.query_last_task")
    @patch("__main__.bump_priority")
    def test_tier2_escalation_after_24h(self, mock_bump, mock_query, mock_comment, mock_dispatch):
        """Tier 2: breach persists >24h -> priority 1 + reassign trading-devops.

        `_dispatch_discord_alert` is mocked (Defect B): the existing-card path
        still fires a throttled Discord alert, and the test must not emit the
        real `[ALERT] Sending Discord` line. `bump_priority` is mocked so the
        test stays hermetic (no UPDATE against the real board DB).
        """
        # Created 25h ago -> age > 86400s.
        mock_query.return_value = ("t_old_1", time.time() - (25 * 3600), "running")
        breaches = [{
            "type": "PIPELINE_STARVATION", "metric": "finalizer_lag_hours",
            "value": 35.5, "threshold": 24.0, "message": "Starved.",
        }]
        old_board = os.environ.get("HERMES_KANBAN_BOARD")
        os.environ["HERMES_KANBAN_BOARD"] = "sycode-trading"
        try:
            with patch("subprocess.run") as mock_run:
                routed, deduped, _h = route_breaches_to_kanban(breaches, ["/tmp/report.json"])
                # Capture the reassign kanban invocation.
                kanban_calls = [c for c in mock_run.call_args_list
                                if c.args and list(c.args[0])[:2] == ["hermes", "kanban"]]
                reassign_seen = any(list(c.args[0])[2] == "reassign" for c in kanban_calls)
        finally:
            if old_board is None:
                os.environ.pop("HERMES_KANBAN_BOARD", None)
            else:
                os.environ["HERMES_KANBAN_BOARD"] = old_board
        self.assertEqual(routed, 0)
        self.assertEqual(deduped, 1)
        mock_comment.assert_called_once()
        mock_dispatch.assert_called()  # throttled first-alert request still attempted
        # Tier 2 priority bump must have fired against the board DB.
        mock_bump.assert_called_once()
        self.assertEqual(mock_bump.call_args[0][0], "t_old_1")
        self.assertEqual(mock_bump.call_args[0][2], 1)
        # Tier 2 reassign to trading-devops must have fired.
        self.assertTrue(reassign_seen, "expected kanban reassign to trading-devops on >24h breach")

    @patch("__main__.auto_close_diagnostic")
    @patch("__main__.query_active_diag_cards")
    def test_auto_close_after_two_healthy(self, mock_query_cards, mock_complete):
        """Auto-close fires only after two consecutive healthy profiles (uses __healthy__ counter)."""
        mock_complete.return_value = True
        # Simulate an active card for finalizer_lag_hours.
        mock_query_cards.return_value = [
            {"id": "t_active_1", "idempotency_key": "diag:sycode-trading:PIPELINE_STARVATION:finalizer_lag_hours", "priority": 0}
        ]
        state = {}  # no prior healthy count
        old_board = os.environ.get("HERMES_KANBAN_BOARD")
        os.environ["HERMES_KANBAN_BOARD"] = "sycode-trading"
        try:
            # Profile 1 healthy: counter -> 1, no completion.
            done1 = process_auto_close({"finalizer_lag_hours": 1}, state)
            self.assertEqual(done1, [])
            self.assertEqual(state["__healthy__"]["finalizer_lag_hours"], 1)
            # Profile 2 healthy: counter -> 2, completion fires.
            done2 = process_auto_close({"finalizer_lag_hours": 1}, state)
            self.assertEqual(done2, ["t_active_1"])
            mock_complete.assert_called_once_with("t_active_1", "finalizer_lag_hours", "sycode-trading")
        finally:
            if old_board is None:
                os.environ.pop("HERMES_KANBAN_BOARD", None)
            else:
                os.environ["HERMES_KANBAN_BOARD"] = old_board
        # Counter reset after completion.
        self.assertEqual(state["__healthy__"]["finalizer_lag_hours"], 0)

    @patch("__main__.auto_close_diagnostic")
    @patch("__main__.query_active_diag_cards")
    def test_auto_close_reset_on_breach(self, mock_query_cards, mock_complete):
        """A breach in a profile resets the healthy counter (no premature close)."""
        mock_query_cards.return_value = [
            {"id": "t_active_1", "idempotency_key": "diag:sycode-trading:PIPELINE_STARVATION:finalizer_lag_hours", "priority": 0}
        ]
        state = {"__healthy__": {"finalizer_lag_hours": 1}}  # one healthy already recorded
        old_board = os.environ.get("HERMES_KANBAN_BOARD")
        os.environ["HERMES_KANBAN_BOARD"] = "sycode-trading"
        try:
            # This profile breaches (consecutive=0) -> reset, no completion.
            done = process_auto_close({"finalizer_lag_hours": 0}, state)
            self.assertEqual(done, [])
            self.assertEqual(state["__healthy__"]["finalizer_lag_hours"], 0)
        finally:
            if old_board is None:
                os.environ.pop("HERMES_KANBAN_BOARD", None)
            else:
                os.environ["HERMES_KANBAN_BOARD"] = old_board
        mock_complete.assert_not_called()

    @patch("__main__.auto_close_diagnostic")
    @patch("__main__.query_active_diag_cards")
    def test_auto_close_integration_two_persisted_runs(self, mock_query_cards, mock_complete):
        """INTEGRATION (Defect A3): real healthy-path wiring closes the card after two
        persisted runs, without touching the live alert_state.json.

        Drives the SAME code path main() uses on the healthy branch:
            query_active_diag_cards()  -> metric set  -> process_auto_close()
            -> auto_close_diagnostic() -> kanban complete
        but with the on-disk state backed by a temp file so the production
        alert_state.json is never read or mutated. Proves the __healthy__ counter
        survives across two persisted runs (the Defect A failure mode was that the
        counter collided with the cooldown dict and raised TypeError in prod).
        """
        mock_complete.return_value = True
        active_card = {
            "id": "t_live_42",
            "idempotency_key": "diag:sycode-trading:PIPELINE_STARVATION:finalizer_lag_hours",
            "priority": 0,
        }
        mock_query_cards.return_value = [active_card]

        import tempfile
        tmp_state = Path(tempfile.mkdtemp(prefix="dq-ac-test-")) / "alert_state.json"
        old_board = os.environ.get("HERMES_KANBAN_BOARD")
        os.environ["HERMES_KANBAN_BOARD"] = "sycode-trading"
        try:
            with patch("__main__.ALERT_STATE_PATH", tmp_state):
                # --- Run 1 (healthy): counter -> 1, no completion ---
                state1 = load_alert_state()        # fresh empty dict
                cards = query_active_diag_cards()
                metric_ids = {c["idempotency_key"].split(":")[3]: 1 for c in cards
                              if c["idempotency_key"].startswith("diag:")}
                done1 = process_auto_close(metric_ids, state1)
                save_alert_state(state1)
                self.assertEqual(done1, [])
                self.assertEqual(state1["__healthy__"]["finalizer_lag_hours"], 1)
                mock_complete.assert_not_called()

                # --- Run 2 (healthy): counter -> 2, completion fires ---
                # Simulate a NEW process load: read the persisted state file back.
                state2 = load_alert_state()
                cards = query_active_diag_cards()
                metric_ids = {c["idempotency_key"].split(":")[3]: 1 for c in cards
                              if c["idempotency_key"].startswith("diag:")}
                done2 = process_auto_close(metric_ids, state2)
                save_alert_state(state2)
                self.assertEqual(done2, ["t_live_42"])
                mock_complete.assert_called_once_with(
                    "t_live_42", "finalizer_lag_hours", "sycode-trading")
                # Counter reset after completion (persisted).
                self.assertEqual(state2["__healthy__"]["finalizer_lag_hours"], 0)

                # The cooldown store must remain untouched by the healthy counter.
                self.assertNotIn("finalizer_lag_hours", state2)
        finally:
            if old_board is None:
                os.environ.pop("HERMES_KANBAN_BOARD", None)
            else:
                os.environ["HERMES_KANBAN_BOARD"] = old_board
            if tmp_state.exists():
                tmp_state.unlink()

    # ------------------------------------------------------------------
    # REGRESSION: board-resolution fix (task t_4f419b25)
    # ------------------------------------------------------------------

    @patch("__main__.KANBAN_BOARDS_DIR")
    def test_resolve_board_db_uses_key_board_slug(self, mock_boards_dir):
        """resolve_board_db must return the board-specific kanban.db, not the
        ambient HERMES_KANBAN_DB / on-disk current pin.

        This is the systemic fix for the QUEUE_BACKLOG flood: previously
        query_last_task() read HERMES_KANBAN_DB, which could resolve to a
        DIFFERENT board than the one the card was created on (current pin =
        sycode-trading, cards created on jarvis-os). Dedup then missed and
        DIAGNOSTIC cards multiplied."""
        import tempfile
        from pathlib import Path as _P
        base = _P(tempfile.mkdtemp(prefix="dq-board-test-"))
        target = base / "jarvis-os" / "kanban.db"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
        mock_boards_dir.__truediv__ = lambda self, name: base / name

        # Ambient env points at a DIFFERENT board (the bug condition) — must be ignored.
        old_db = os.environ.get("HERMES_KANBAN_DB")
        old_board = os.environ.get("HERMES_KANBAN_BOARD")
        os.environ["HERMES_KANBAN_DB"] = "/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db"
        os.environ["HERMES_KANBAN_BOARD"] = "sycode-trading"
        try:
            self.assertEqual(resolve_board_db("jarvis-os"), str(target))
        finally:
            for k, v in (("HERMES_KANBAN_DB", old_db), ("HERMES_KANBAN_BOARD", old_board)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            import shutil
            shutil.rmtree(base, ignore_errors=True)

    @patch("__main__.KANBAN_BOARDS_DIR")
    def test_resolve_board_db_legacy_fallback(self, mock_boards_dir):
        """When the board DB does not exist under boards/, fall back to the flat legacy kanban.db."""
        import tempfile
        from pathlib import Path as _P
        base = _P(tempfile.mkdtemp(prefix="dq-board-test-"))
        legacy = _P("/home/frank/.hermes/kanban.db")
        mock_boards_dir.__truediv__ = lambda self, name: base / name
        # No per-board DB created -> legacy path returned.
        self.assertEqual(resolve_board_db("jarvis-os"), str(legacy))
        import shutil
        shutil.rmtree(base, ignore_errors=True)

    @patch("__main__.KANBAN_BOARDS_DIR")
    @patch("__main__.kb.connect")
    @patch("os.path.exists")
    def test_query_active_diag_cards_key_driven_board(self, mock_exists, mock_connect, mock_boards_dir):
        """R2: query_active_diag_cards must decode the board from the diag
        idempotency key, NOT from ambient HERMES_KANBAN_BOARD env.

        Regression for t_6cf8bfd4: the old implementation read
        HERMES_KANBAN_BOARD to pick a single DB; when that env pointed at the
        wrong board the auto-close lookup missed the real card (or hit the
        wrong DB). The new implementation scans every board DB and records
        the board decoded from each key (parts[1]).

        Ambient env is deliberately poisoned to a DIFFERENT board to prove it
        is ignored.
        """
        import tempfile
        from pathlib import Path as _P
        base = _P(tempfile.mkdtemp(prefix="dq-diag-test-"))

        def board_div(name):
            return base / name

        mock_boards_dir.__truediv__ = lambda self, name: base / name
        mock_boards_dir.iterdir = lambda: [base / "jarvis-os", base / "sycode-trading"]
        for d in ("jarvis-os", "sycode-trading"):
            (base / d / "kanban.db").parent.mkdir(parents=True, exist_ok=True)
            (base / d / "kanban.db").touch()

        def exists_side_effect(path):
            return str(path) in (str(base / "jarvis-os" / "kanban.db"), str(base / "sycode-trading" / "kanban.db"))

        mock_exists.side_effect = exists_side_effect

        def connect_side_effect(db_path=None, board=None):
            conn = MagicMock()
            cur = MagicMock()
            conn.cursor.return_value = cur
            # Each board returns diag cards that encode a board in the key.
            if str(db_path) == str(base / "jarvis-os" / "kanban.db"):
                cur.fetchall.return_value = [
                    ("t_j1", "diag:jarvis-os:QUEUE_BACKLOG:finalizer_backlog", 0),
                ]
            elif str(db_path) == str(base / "sycode-trading" / "kanban.db"):
                cur.fetchall.return_value = [
                    ("t_s1", "diag:sycode-trading:PIPELINE_STARVATION:finalizer_lag_hours", 1),
                ]
            else:
                cur.fetchall.return_value = []
            return conn

        mock_connect.side_effect = connect_side_effect

        # Poison ambient env to a board that has NO diag cards here — must be ignored.
        old_db = os.environ.get("HERMES_KANBAN_DB")
        old_board = os.environ.get("HERMES_KANBAN_BOARD")
        os.environ["HERMES_KANBAN_DB"] = "/nonexistent/board/kanban.db"
        os.environ["HERMES_KANBAN_BOARD"] = "yorkstone-supplies"
        try:
            cards = query_active_diag_cards()
            # Both boards scanned; board decoded from key, not env.
            by_id = {c["id"]: c for c in cards}
            self.assertIn("t_j1", by_id)
            self.assertIn("t_s1", by_id)
            self.assertEqual(by_id["t_j1"]["board"], "jarvis-os")
            self.assertEqual(by_id["t_s1"]["board"], "sycode-trading")
            # The poisoned ambient board must NOT appear in results.
            self.assertTrue(all(c["board"] != "yorkstone-supplies" for c in cards))
            # Each key is the canonical diag key.
            self.assertEqual(by_id["t_j1"]["idempotency_key"], "diag:jarvis-os:QUEUE_BACKLOG:finalizer_backlog")
        finally:
            for k, v in (("HERMES_KANBAN_DB", old_db), ("HERMES_KANBAN_BOARD", old_board)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            import shutil
            shutil.rmtree(base, ignore_errors=True)


# ----------------------------------------------------------------------------
# AUTOMATED SELF-HEALING PROCEDURES
# ----------------------------------------------------------------------------

def self_heal(breaches):
    """Executes automated self-healing actions on detected breaches."""
    print(f"\n--- STARTING AUTOMATED SELF-HEALING ---")
    healed_types = set()
    
    # 1. Orphan rows self-healing via DataRetentionService
    orphan_breaches = [b for b in breaches if b["type"] == "ORPHAN_ROWS"]
    if orphan_breaches:
        print(f"[SELF-HEAL] Found {len(orphan_breaches)} orphan row breaches. Triggering run-retention.ts...")
        cmd = [
            "docker", "exec", "sycodetrading-server",
            "bun", "run", "/app/scripts/run-retention.ts",
            "--orphansOnly", "--retentionDays=0"
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            print(f"[SELF-HEAL] run-retention.ts executed successfully:\n{res.stdout[-600:]}")
            healed_types.add("ORPHAN_ROWS")
        except subprocess.CalledProcessError as e:
            print(f"[SELF-HEAL] ERROR: run-retention.ts failed (rc={e.returncode}): {e.stderr}", file=sys.stderr)

    # 2. Dead-letter queue errors self-healing via replay-dlq.ts
    dlq_breaches = [b for b in breaches if b["type"] == "DLQ_ERRORS"]
    if dlq_breaches:
        print(f"[SELF-HEAL] Found dead-letter queue errors. Triggering replay-dlq.ts...")
        cmd = [
            "docker", "exec", "sycodetrading-server",
            "bun", "run", "/app/scripts/replay-dlq.ts"
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            print(f"[SELF-HEAL] replay-dlq.ts executed successfully:\n{res.stdout[-600:]}")
            healed_types.add("DLQ_ERRORS")
        except subprocess.CalledProcessError as e:
            print(f"[SELF-HEAL] ERROR: replay-dlq.ts failed (rc={e.returncode}): {e.stderr}", file=sys.stderr)

    # 3. Label queue backlog self-healing via label-clean-outcomes-24h.ts
    backlog_breaches = [b for b in breaches if b["type"] == "QUEUE_BACKLOG" and b["metric"] == "binary_backlog"]
    if backlog_breaches:
        print(f"[SELF-HEAL] Found clean_outcome_binary_24h label backlog. Triggering label-clean-outcomes-24h.ts...")
        cmd = [
            "docker", "exec", "-e", "CLEAN_OUTCOME_LABELER_EXECUTE=true", "sycodetrading-server",
            "bun", "run", "/app/scripts/label-clean-outcomes-24h.ts", "--execute", "--max-rows", "500"
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            print(f"[SELF-HEAL] label-clean-outcomes-24h.ts executed successfully:\n{res.stdout[-600:]}")
            healed_types.add("QUEUE_BACKLOG")
        except subprocess.CalledProcessError as e:
            print(f"[SELF-HEAL] ERROR: label-clean-outcomes-24h.ts failed (rc={e.returncode}): {e.stderr}", file=sys.stderr)

    print(f"--- SELF-HEALING ATTEMPTS COMPLETE ---\n")
    return healed_types


# ----------------------------------------------------------------------------
# MAIN EXECUTION ENTRY POINT
# ----------------------------------------------------------------------------

def _fmt_lag_hours(value):
    """Format a pipeline-lag hours value for the scorecard.

    inspect_queues.safe_lag returns the string "UNKNOWN" when the backing query
    times out (statement_timeout / DB lock contention — see t_d9c7537b), so a
    bare {value:.1f} raises "Unknown format code 'f' for object of type 'str'"
    and crashes the whole audit BEFORE kanban routing happens. Format numerics
    and pass through non-numeric sentinels verbatim (same hardening the breach
    evaluation already applies at lines 583-617).
    """
    if isinstance(value, (int, float)):
        return f"{float(value):.1f}h"
    return f"{value}h"


def main():
    parser = argparse.ArgumentParser(description="Continuous Data Integrity Monitor & Evidence Collector")
    parser.add_argument("--test", action="store_true", help="run comprehensive unit tests with mock DB")
    parser.add_argument("--heal", action="store_true", help="execute automated self-healing actions on breaches")
    args = parser.parse_args()

    if args.test:
        print("Running unit tests...")
        sys.argv = [sys.argv[0]]  # Clear argv for unittest
        unittest.main()
        sys.exit(0)

    now_utc = datetime.now(timezone.utc)
    print(f"[{now_utc:%Y-%m-%d %H:%M:%S}Z] Starting SycodeTrading Data Integrity Audit...")

    try:
        results, breaches = evaluate_data_integrity()
    except Exception as e:
        print(f"FATAL: Operational error during audit: {e}", file=sys.stderr)
        sys.exit(1)

    # Print summary metrics to console
    print(f"\n--- INTEGRITY SCORECARD ---")
    print(f"Orphan signal_pnl_points:      {results['orphans']['pnl_points']['count']}")
    print(f"Orphan trade_close journeys:   {results['orphans']['trade_close_journeys']['count']}")
    print(f"Orphan trade_close positions:  {results['orphans']['trade_close_positions']['count']}")
    print(f"Finalizer Queue Backlog:       {results['queues']['finalizer_backlog']}")
    print(f"Binary Label Backlog:          {results['queues']['binary_backlog']}")
    print(f"Finalizer pipeline lag:        {_fmt_lag_hours(results['queues']['finalizer_lag_hours'])}")
    print(f"Trade Close pipeline lag:      {_fmt_lag_hours(results['queues']['closer_lag_hours'])}")
    print(f"Binary Labeler pipeline lag:   {_fmt_lag_hours(results['queues']['binary_lag_hours'])}")
    print(f"Failed DLQ Outcome events:     {results['dlq']['count']}")
    print(f"\n--- COHORT ACCRUAL PROGRESS ---")
    print(f"Signals since Epoch:           {results['cohort']['signals']}")
    print(f"Realized Closes since Epoch:   {results['cohort']['closes']}")
    print(f"Clean Outcomes since Epoch:    {results['cohort']['outcomes']} (Progress: {results['cohort']['progress_rebaseline_pct']:.1f}% to n>=100 rebaseline, {results['cohort']['progress_research_pct']:.1f}% to n>=300)")

    if breaches:
        print(f"\n⚠️  ALERTS TRIGGERED: {len(breaches)} integrity threshold breaches detected!")
        for b in breaches:
            print(f"  - [{b['type']}] {b['message']}")

        if args.heal and not a3_gate_hit_this_run:
            # Run self-healing and re-evaluate (suppressed on A3 gate to avoid
            # burning credentials/credits retrying an exhausted upstream — review §5.2)
            healed_types = self_heal(breaches)
            if healed_types:
                print("Self-healing actions executed. Waiting 3 seconds for database/pipeline updates before re-evaluation...")
                time.sleep(3)
                try:
                    results, breaches = evaluate_data_integrity()
                    print("\n--- POST-HEAL INTEGRITY SCORECARD ---")
                    print(f"Orphan signal_pnl_points:      {results['orphans']['pnl_points']['count']}")
                    print(f"Orphan trade_close journeys:   {results['orphans']['trade_close_journeys']['count']}")
                    print(f"Orphan trade_close positions:  {results['orphans']['trade_close_positions']['count']}")
                    print(f"Finalizer Queue Backlog:       {results['queues']['finalizer_backlog']}")
                    print(f"Binary Label Backlog:          {results['queues']['binary_backlog']}")
                    print(f"Failed DLQ Outcome events:     {results['dlq']['count']}")
                    if not breaches:
                        print(f"\n✅ All breaches successfully self-healed!")
                        sys.exit(0)
                    else:
                        print(f"\n⚠️  {len(breaches)} breaches remain after self-healing:")
                        for b in breaches:
                            print(f"  - [{b['type']}] {b['message']}")
                except Exception as e:
                    print(f"ERROR: Re-evaluation failed: {e}", file=sys.stderr)

        # Export evidence automatically on breach
        print(f"\nExporting diagnostic evidence to accelerate developer resolution...")
        exported = collect_and_save_evidence(results, breaches)
        for path in exported:
            print(f"  -> Exported: {path}")

        # Route breaches to Kanban boards (smart alerting: Tier1/2/3 + Discord cooldown)
        alert_state = load_alert_state()
        try:
            route_breaches_to_kanban(breaches, exported, state=alert_state)
        except Exception as ke:
            print(f"Error auto-routing breaches to Kanban: {ke}", file=sys.stderr)
        finally:
            save_alert_state(alert_state)

        sys.exit(2)

    # Healthy path: attempt auto-close of any previously active diagnostic cards
    # after two consecutive healthy CDPDS/audit profiles (review §2.2/§4.1.5).
    print(f"\n✅ All alertable pipelines are healthy, zero orphans detected.")
    alert_state = load_alert_state()
    try:
        # Derive the metric set from the REAL active diagnostic cards on the
        # board (review t_97299572 Defect A2), NOT from alert_state.keys()
        # (which are per-metric Discord cooldown entries, not trackable metrics).
        # No breaches this profile -> every active card's metric counts as one
        # healthy profile. Extract the trailing metric token from each card's
        # canonical idempotency key: diag:<board>:<type>:<metric>.
        active_cards = query_active_diag_cards()
        metric_ids = {}
        for card in active_cards:
            key = card.get("idempotency_key") or ""
            if key.startswith("diag:"):
                parts = key.split(":")
                if len(parts) == 4:
                    metric_ids[parts[3]] = 1
        completed = process_auto_close(metric_ids, alert_state)
        if completed:
            print(f"[AUTOCLOSE] Completed {len(completed)} normalized diagnostic card(s): {completed}")
    except Exception as ae:
        print(f"Error during auto-close processing: {ae}", file=sys.stderr)
    finally:
        save_alert_state(alert_state)
    sys.exit(0)


if __name__ == "__main__":
    main()
