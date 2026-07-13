#!/usr/bin/env python3
# invoker: hermes cron job or manual execution
#
# data_profiler_sentinel.py
#
# Continuous Data Profiler & Drift Sentinel.
# Analyzes the sycode-trading database to measure:
#   1. Completeness: Null counts and percentages for key columns.
#   2. Freshness: Elapsed hours since the last write to crucial tables.
#   3. Validity: Domain constraint and range violations across signal_journeys.
#   4. Consistency: Referential integrity checks (orphan counts).
#   5. Accuracy: Predictive accuracy/calibration of direction_quality_prob vs clean labels.
#
# Detects DirectionQuality feedback collapses (variance/all-null predictions).
# Automatically updates scorecard.json hourly.
#
# Safety boundaries: Paper-Mode Only, Zero Secrets, Read-Only database audits.

import argparse
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from second_brain_writer import write_markdown_atomic

# Configurations
DB_CONTAINER = "sycodetrading-supabase-db"
SCORECARD_PATH = Path("/home/frank/.hermes/var/dq/scorecard.json")
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
OBSIDIAN_DASHBOARD_PATH = Path("/home/frank/obsidian-fleet-vault/analytics/data-quality-dashboard.md")

# Default thresholds
THRESHOLD_FRESHNESS_STALE_HOURS = 24.0
THRESHOLD_COLLAPSE_STDDEV = 0.005
THRESHOLD_COLLAPSE_MIN_COUNT = 10


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


def send_discord_alert(message, target):
    """Sends a Discord alert via the Jarvis profile integration."""
    print(f"[SENTINEL] Discord Alert Target {target}: {message}")
    env = os.environ.copy()
    env["HERMES_HOME"] = "/home/frank/.hermes/profiles/jarvis"
    env["HERMES_PROFILE"] = "jarvis"
    result = subprocess.run(
        [HERMES_BIN, "send", "--to", target, "--quiet", message],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if result.returncode != 0:
        print(f"[SENTINEL] WARNING: Discord alert delivery failed: {result.stderr.strip() or result.stdout.strip()}", file=sys.stderr)


# ----------------------------------------------------------------------------
# CORE PROFILING LOGIC
# ----------------------------------------------------------------------------

def compute_completeness():
    """Computes completeness statistics and score."""
    # 1. Total row and null checks on all signal_journeys
    count_query = """
        SELECT
          COUNT(*)::integer AS total_rows,
          COUNT(id)::integer AS id_non_null,
          COUNT(correlation_id)::integer AS correlation_id_non_null,
          COUNT(symbol)::integer AS symbol_non_null,
          COUNT(direction)::integer AS direction_non_null,
          COUNT(entry_price)::integer AS entry_price_non_null,
          COUNT(triggered_at)::integer AS triggered_at_non_null,
          COUNT(direction_quality_prob)::integer AS dqp_non_null
        FROM public.signal_journeys;
    """
    counts = run_sql(count_query)[0]
    total_rows = int(counts["total_rows"])

    # 2. Null checks on inactive signal_journeys
    inactive_query = """
        SELECT
          COUNT(*)::integer AS total_inactive,
          COUNT(exit_price)::integer AS exit_price_non_null,
          COUNT(realized_pnl_percent)::integer AS realized_pnl_non_null,
          COUNT(exit_type)::integer AS exit_type_non_null
        FROM public.signal_journeys
        WHERE is_active = false;
    """
    inactive_counts = run_sql(inactive_query)[0]
    total_inactive = int(inactive_counts["total_inactive"])

    # 3. Null checks on mature inactive signal_journeys (older than 24h)
    mature_query = """
        SELECT
          COUNT(*)::integer AS total_mature_inactive,
          COUNT(clean_outcome_binary_24h)::integer AS clean_outcome_non_null
        FROM public.signal_journeys
        WHERE is_active = false AND created_at <= now() - interval '24 hours';
    """
    mature_counts = run_sql(mature_query)[0]
    total_mature_inactive = int(mature_counts["total_mature_inactive"])

    # Calculate null counts
    null_counts = {
        "id": total_rows - int(counts["id_non_null"]),
        "correlation_id": total_rows - int(counts["correlation_id_non_null"]),
        "symbol": total_rows - int(counts["symbol_non_null"]),
        "direction": total_rows - int(counts["direction_non_null"]),
        "entry_price": total_rows - int(counts["entry_price_non_null"]),
        "triggered_at": total_rows - int(counts["triggered_at_non_null"]),
        "direction_quality_prob": total_rows - int(counts["dqp_non_null"]),
        "exit_price": total_inactive - int(inactive_counts["exit_price_non_null"]),
        "realized_pnl_percent": total_inactive - int(inactive_counts["realized_pnl_non_null"]),
        "exit_type": total_inactive - int(inactive_counts["exit_type_non_null"]),
        "clean_outcome_binary_24h": total_mature_inactive - int(mature_counts["clean_outcome_non_null"])
    }

    # Calculate null percentages
    null_percentages = {}
    for col, null_val in null_counts.items():
        if col in ["exit_price", "realized_pnl_percent", "exit_type"]:
            denom = total_inactive
        elif col == "clean_outcome_binary_24h":
            denom = total_mature_inactive
        else:
            denom = total_rows
        null_percentages[col] = (null_val / denom * 100.0) if denom > 0 else 0.0

    # Completeness score is 100 - average of all null percentages
    avg_null_pct = sum(null_percentages.values()) / len(null_percentages) if null_percentages else 0.0
    score = max(0.0, min(100.0, 100.0 - avg_null_pct))

    return {
        "score": round(score, 2),
        "total_rows": total_rows,
        "total_inactive": total_inactive,
        "total_mature_inactive": total_mature_inactive,
        "null_counts": null_counts,
        "null_percentages": {k: round(v, 2) for k, v in null_percentages.items()},
        "details": "Calculated completeness across key columns in public.signal_journeys table."
    }


def compute_freshness():
    """Computes freshness statistics and score."""
    # Last signal lag
    signal_query = """
        SELECT
          COALESCE(MAX(created_at), '1970-01-01'::timestamp with time zone)::text AS last_signal,
          COALESCE(EXTRACT(EPOCH FROM (now() - MAX(created_at)))/3600.0, -1.0)::double precision AS signal_lag_hours
        FROM public.signal_journeys;
    """
    signal_res = run_sql(signal_query)[0]
    signal_lag = float(signal_res["signal_lag_hours"])

    # Last outcome lag
    outcome_query = """
        SELECT
          COALESCE(MAX(created_at), '1970-01-01'::timestamp with time zone)::text AS last_outcome,
          COALESCE(EXTRACT(EPOCH FROM (now() - MAX(created_at)))/3600.0, -1.0)::double precision AS outcome_lag_hours
        FROM public.decision_outcomes;
    """
    outcome_res = run_sql(outcome_query)[0]
    outcome_lag = float(outcome_res["outcome_lag_hours"])

    # Last trade close lag
    close_query = """
        SELECT
          COALESCE(MAX(created_at), '1970-01-01'::timestamp with time zone)::text AS last_close,
          COALESCE(EXTRACT(EPOCH FROM (now() - MAX(created_at)))/3600.0, -1.0)::double precision AS close_lag_hours
        FROM public.trade_close_events;
    """
    close_res = run_sql(close_query)[0]
    close_lag = float(close_res["close_lag_hours"])

    # Freshness score scales down based on signal lag (0 to 24 hours)
    # Target signal freshness: within 2 hours is perfect 100, decaying to 0 at 24 hours.
    if signal_lag < 0 or signal_lag > THRESHOLD_FRESHNESS_STALE_HOURS:
        score = 0.0
    elif signal_lag <= 2.0:
        score = 100.0
    else:
        score = 100.0 - (signal_lag - 2.0) * (100.0 / (THRESHOLD_FRESHNESS_STALE_HOURS - 2.0))
        score = max(0.0, min(100.0, score))

    return {
        "score": round(score, 2),
        "last_signal_created_at": signal_res["last_signal"],
        "last_outcome_created_at": outcome_res["last_outcome"],
        "last_trade_close_created_at": close_res["last_close"],
        "signal_lag_hours": round(signal_lag, 2) if signal_lag >= 0 else None,
        "outcome_lag_hours": round(outcome_lag, 2) if outcome_lag >= 0 else None,
        "trade_close_lag_hours": round(close_lag, 2) if close_lag >= 0 else None,
        "details": f"Enforces surface and queue freshness. Score degrades to 0 if signal delay exceeds {THRESHOLD_FRESHNESS_STALE_HOURS}h."
    }


def compute_validity():
    """Computes validity statistics and score."""
    # Check domain constraint and range violations across signal_journeys columns
    total_query = "SELECT COUNT(*)::integer AS total_rows FROM public.signal_journeys;"
    total_rows = int(run_sql(total_query)[0]["total_rows"])

    validity_query = """
        SELECT
          SUM(CASE WHEN direction NOT IN ('LONG', 'SHORT') THEN 1 ELSE 0 END)::integer AS invalid_direction,
          SUM(CASE WHEN entry_price <= 0 THEN 1 ELSE 0 END)::integer AS invalid_entry_price,
          SUM(CASE WHEN direction_quality_prob IS NOT NULL AND (direction_quality_prob < 0.0 OR direction_quality_prob > 1.0) THEN 1 ELSE 0 END)::integer AS invalid_dqp,
          SUM(CASE WHEN progress_percent IS NOT NULL AND progress_percent < 0.0 THEN 1 ELSE 0 END)::integer AS invalid_progress
        FROM public.signal_journeys;
    """
    res = run_sql(validity_query)[0]
    
    invalid_counts = {
        "invalid_direction": int(res["invalid_direction"] or 0),
        "invalid_entry_price": int(res["invalid_entry_price"] or 0),
        "invalid_direction_quality_probability": int(res["invalid_dqp"] or 0),
        "invalid_progress_percent": int(res["invalid_progress"] or 0)
    }

    total_invalid = sum(invalid_counts.values())
    validity_pct = (1.0 - (total_invalid / total_rows)) * 100.0 if total_rows > 0 else 100.0
    score = max(0.0, min(100.0, validity_pct))

    return {
        "score": round(score, 2),
        "invalid_rows_count": invalid_counts,
        "details": "Asserts range validity of prices, percentages, and probabilities."
    }


def compute_consistency():
    """Computes consistency statistics and score."""
    # Referential integrity / orphan checks
    consistency_query = """
        SELECT
          (SELECT count(*)::integer FROM public.signal_pnl_points p LEFT JOIN public.signal_journeys j ON p.journey_id = j.id WHERE j.id IS NULL) as pnl_orphans,
          (SELECT count(*)::integer FROM public.trade_close_events t LEFT JOIN public.signal_journeys j ON t.journey_id = j.id WHERE t.journey_id IS NOT NULL AND j.id IS NULL) as close_journey_orphans,
          (SELECT count(*)::integer FROM public.trade_close_events t LEFT JOIN public.managed_positions p ON t.position_id = p.id WHERE p.id IS NULL) as close_position_orphans;
    """
    res = run_sql(consistency_query)[0]
    
    orphan_counts = {
        "pnl_points_orphans": int(res["pnl_orphans"] or 0),
        "trade_close_journeys_orphans": int(res["close_journey_orphans"] or 0),
        "trade_close_positions_orphans": int(res["close_position_orphans"] or 0)
    }

    total_orphans = sum(orphan_counts.values())
    # Deduct 5 points per orphan row detected (up to 100 points maximum deduction)
    score = max(0.0, 100.0 - (total_orphans * 5.0))

    return {
        "score": round(score, 2),
        "orphan_counts": orphan_counts,
        "details": "Monitors referential integrity and logical state coherence across PnL points and close events."
    }


def compute_accuracy():
    """Computes predictive accuracy statistics and score."""
    # Check overall calibration of direction_quality_prob vs clean_outcome_binary_24h
    stats_query = """
        SELECT
          COUNT(*)::integer AS labeled_count,
          AVG( (direction_quality_prob - (CASE WHEN clean_outcome_binary_24h = true THEN 1.0 ELSE 0.0 END))^2 )::double precision AS brier_score,
          AVG( ABS(direction_quality_prob - (CASE WHEN clean_outcome_binary_24h = true THEN 1.0 ELSE 0.0 END)) )::double precision AS mae,
          AVG(CASE WHEN (direction_quality_prob >= 0.5) = clean_outcome_binary_24h THEN 1.0 ELSE 0.0 END)::double precision AS binary_accuracy
        FROM public.signal_journeys
        WHERE direction_quality_prob IS NOT NULL AND clean_outcome_binary_24h IS NOT NULL;
    """
    stats_res = run_sql(stats_query)[0]
    total_labeled = int(stats_res["labeled_count"] or 0)

    # Check recent calibration over the last 1000 labeled predictions
    recent_query = """
        SELECT
          COUNT(*)::integer AS labeled_count,
          AVG( (direction_quality_prob - (CASE WHEN clean_outcome_binary_24h = true THEN 1.0 ELSE 0.0 END))^2 )::double precision AS brier_score,
          AVG( ABS(direction_quality_prob - (CASE WHEN clean_outcome_binary_24h = true THEN 1.0 ELSE 0.0 END)) )::double precision AS mae,
          AVG(CASE WHEN (direction_quality_prob >= 0.5) = clean_outcome_binary_24h THEN 1.0 ELSE 0.0 END)::double precision AS binary_accuracy
        FROM (
          SELECT direction_quality_prob, clean_outcome_binary_24h
          FROM public.signal_journeys
          WHERE direction_quality_prob IS NOT NULL AND clean_outcome_binary_24h IS NOT NULL
          ORDER BY created_at DESC
          LIMIT 1000
        ) t;
    """
    recent_res = run_sql(recent_query)[0]
    recent_labeled = int(recent_res["labeled_count"] or 0)

    # Use recent binary accuracy as the basis for accuracy score
    binary_accuracy = float(recent_res["binary_accuracy"] or 0.50) if recent_labeled > 0 else float(stats_res["binary_accuracy"] or 0.50)
    score = binary_accuracy * 100.0

    return {
        "score": round(score, 2),
        "total_labeled_predictions": total_labeled,
        "recent_labeled_predictions": recent_labeled,
        "overall": {
            "brier_score": round(float(stats_res["brier_score"] or 0.0), 4),
            "mean_absolute_error": round(float(stats_res["mae"] or 0.0), 4),
            "binary_accuracy_pct": round(float(stats_res["binary_accuracy"] or 0.0) * 100.0, 2)
        },
        "recent_1000": {
            "brier_score": round(float(recent_res["brier_score"] or 0.0), 4),
            "mean_absolute_error": round(float(recent_res["mae"] or 0.0), 4),
            "binary_accuracy_pct": round(binary_accuracy * 100.0, 2)
        },
        "details": "Evaluates prediction accuracy (Brier score, MAE, Binary Accuracy) of DirectionQuality probability models vs mature outcome labels."
    }


def compute_direction_quality_feedback():
    """Profiles the distribution of DirectionQuality predictions and checks for feedback loop collapse."""
    stats_query = """
        SELECT
          count(*)::integer as count,
          COALESCE(min(direction_quality_prob), -1.0)::double precision as min_val,
          COALESCE(max(direction_quality_prob), -1.0)::double precision as max_val,
          COALESCE(avg(direction_quality_prob), -1.0)::double precision as avg_val,
          COALESCE(stddev(direction_quality_prob), -1.0)::double precision as std_val,
          count(distinct direction_quality_prob)::integer as unique_val_cnt
        FROM (
          SELECT direction_quality_prob
          FROM public.signal_journeys
          WHERE direction_quality_prob IS NOT NULL
          ORDER BY created_at DESC
          LIMIT 1000
        ) t;
    """
    res = run_sql(stats_query)[0]
    count = int(res["count"] or 0)
    stddev = float(res["std_val"] or -1.0)
    unique_vals = int(res["unique_val_cnt"] or 0)

    # Check for overall model active state
    active_check_query = """
        SELECT count(*)::integer as total_recent_journeys
        FROM public.signal_journeys
        WHERE created_at > now() - interval '24 hours';
    """
    recent_journeys = int(run_sql(active_check_query)[0]["total_recent_journeys"] or 0)

    collapsed = False
    reason = "healthy"

    if recent_journeys > 0 and count == 0:
        collapsed = True
        reason = "all_values_null_pipeline_collapse"
    elif count >= THRESHOLD_COLLAPSE_MIN_COUNT:
        if stddev == 0.0 or unique_vals <= 1:
            collapsed = True
            reason = "constant_prediction_collapse"
        elif stddev < THRESHOLD_COLLAPSE_STDDEV:
            collapsed = True
            reason = "standard_deviation_too_low"

    stats = {
        "count": count,
        "mean": round(float(res["avg_val"]), 4) if count > 0 else None,
        "median": round(float(res["avg_val"]), 4) if count > 0 else None, # approximate with avg for simplicity
        "min": round(float(res["min_val"]), 4) if count > 0 else None,
        "max": round(float(res["max_val"]), 4) if count > 0 else None,
        "stddev": round(stddev, 4) if count > 0 else None,
        "unique_values_count": unique_vals,
        "recent_24h_journeys": recent_journeys
    }

    return {
        "collapsed": collapsed,
        "reason": reason,
        "stats": stats
    }


# ----------------------------------------------------------------------------
# EXPORTS & SYSTEM RUN
# ----------------------------------------------------------------------------

def generate_obsidian_dashboard(scorecard):
    """Renders the data quality state to the Obsidian dashboard."""
    metrics = scorecard["metrics"]
    comp = metrics["completeness"]
    fresh = metrics["freshness"]
    val = metrics["validity"]
    cons = metrics["consistency"]
    acc = metrics["accuracy"]
    feedback = metrics["direction_quality_feedback"]
    
    overall_score = scorecard["overall_score"]
    
    # Status calculation helpers
    def get_status(score, is_feedback_collapsed=False):
        if is_feedback_collapsed:
            return "❌ COLLAPSED"
        if score >= 95.0:
            return "🟢 PERFECT"
        elif score >= 85.0:
            return "🟢 HEALTHY"
        elif score >= 70.0:
            return "🟡 WARNING"
        else:
            return "🔴 CRITICAL"

    comp_status = get_status(comp["score"])
    fresh_status = get_status(fresh["score"])
    val_status = get_status(val["score"])
    cons_status = get_status(cons["score"])
    acc_status = get_status(acc["score"])
    dq_status = "❌ COLLAPSED" if feedback["collapsed"] else "🟢 HEALTHY"

    # Form details text
    comp_details = f"Total rows: {comp['total_rows']}. Null count in direction_quality_prob: {comp['null_counts']['direction_quality_prob']} ({comp['null_percentages']['direction_quality_prob']}%)."
    fresh_details = f"Lag hours: signal={fresh.get('signal_lag_hours', 'N/A')}h, outcome={fresh.get('outcome_lag_hours', 'N/A')}h, close={fresh.get('trade_close_lag_hours', 'N/A')}h."
    val_details = f"Invalid counts: direction={val['invalid_rows_count']['invalid_direction']}, entry_price={val['invalid_rows_count']['invalid_entry_price']}, prob={val['invalid_rows_count']['invalid_direction_quality_probability']}."
    cons_details = f"Orphan counts: PnL={cons['orphan_counts']['pnl_points_orphans']}, close_journeys={cons['orphan_counts']['trade_close_journeys_orphans']}, close_positions={cons['orphan_counts']['trade_close_positions_orphans']}."
    
    recent_1000 = acc.get("recent_1000") or {}
    acc_details = f"Recent 1000 labeled: Accuracy={recent_1000.get('binary_accuracy_pct', 'N/A')}%, Brier={recent_1000.get('brier_score', 0.0):.4f}, MAE={recent_1000.get('mean_absolute_error', 0.0):.4f}."

    dq_mean = feedback['stats'].get('mean')
    dq_stddev = feedback['stats'].get('stddev')
    dq_mean_str = f"{dq_mean:.4f}" if dq_mean is not None else "N/A"
    dq_stddev_str = f"{dq_stddev:.4f}" if dq_stddev is not None else "N/A"

    content = f"""# Continuous Data Quality & Real-Time Drift Sentinel Dashboard

**Last updated (UTC):** `{scorecard['timestamp_utc']}`

## 1. Overall System Score

The overall system data quality score is **{overall_score:.2f}%**.

| Dimension | Score | Status | Key Metrics / Details |
|---|---|---|---|
| **Completeness** | {comp['score']:.2f}% | {comp_status} | {comp_details} |
| **Freshness** | {fresh['score']:.2f}% | {fresh_status} | {fresh_details} |
| **Validity** | {val['score']:.2f}% | {val_status} | {val_details} |
| **Consistency** | {cons['score']:.2f}% | {cons_status} | {cons_details} |
| **Accuracy** | {acc['score']:.2f}% | {acc_status} | {acc_details} |

## 2. DirectionQuality Feedback Sentinel

**Feedback State:** {dq_status}
**Reason/Category:** `{feedback['reason']}`

### Recent Statistical Distribution (Last 1,000 predictions)
- **Predictions Count:** {feedback['stats']['count']}
- **Mean Probability:** {dq_mean_str}
- **Std Deviation:** {dq_stddev_str}
- **Unique Values:** {feedback['stats']['unique_values_count']}
- **Recent 24h Ingested Journeys:** {feedback['stats']['recent_24h_journeys']}

## 3. Alerts & Remediation Thresholds

| Dimension | Warning Threshold | Critical Threshold | Current Score |
|---|---|---|---|
| **Overall Score** | < 85.00% | < 70.00% | {overall_score:.2f}% |
| **Completeness** | < 65.00% | < 50.00% | {comp['score']:.2f}% |
| **Freshness** | < 95.00% | < 80.00% | {fresh['score']:.2f}% |
| **Validity** | < 99.00% | < 95.00% | {val['score']:.2f}% |
| **Consistency** | < 99.00% | < 95.00% | {cons['score']:.2f}% |
| **Accuracy** | < 55.00% | < 50.00% | {acc['score']:.2f}% |
| **DQ Feedback** | N/A | Collapsed | {'COLLAPSED' if feedback['collapsed'] else 'HEALTHY'} |

---
*Log maintained asynchronously by the Continuous Data Profiler & Drift Sentinel.*
"""
    updated = scorecard["timestamp_utc"][:10]
    operational_status = (
        "critical" if overall_score < 70.0 else "warning" if overall_score < 85.0 else "healthy"
    )
    write_markdown_atomic(
        OBSIDIAN_DASHBOARD_PATH,
        content,
        title="Data Quality & Drift Sentinel Dashboard",
        type="moc",
        status="active",
        created="2026-07-07",
        updated=updated,
        confidence="high",
        tags=["data-quality", "drift-sentinel", "dashboard", "analytics"],
        sources=[
            "sycodetrading-supabase-db:signal_journeys",
            "sycodetrading-supabase-db:decision_outcomes",
            "sycodetrading-supabase-db:trade_close_events",
        ],
        project="sycode-trading",
        owners=["trading-devops"],
        knowledge_tier="compiled",
        generated=True,
        generator="data_profiler_sentinel.py",
        operational_status=operational_status,
        generated_at=scorecard["timestamp_utc"],
        overall_score=overall_score,
    )
    print(f"Obsidian dashboard written to {OBSIDIAN_DASHBOARD_PATH}")


def check_alert_thresholds(scorecard):
    """Analyzes the scorecard against thresholds and triggers Discord alerts."""
    metrics = scorecard["metrics"]
    comp = metrics["completeness"]
    fresh = metrics["freshness"]
    val = metrics["validity"]
    cons = metrics["consistency"]
    acc = metrics["accuracy"]
    feedback = metrics["direction_quality_feedback"]
    overall_score = scorecard["overall_score"]

    critical_alerts = []
    warning_alerts = []

    # 1. Feedback collapse is a critical alert
    if feedback["collapsed"]:
        dq_mean = feedback["stats"].get("mean")
        dq_stddev = feedback["stats"].get("stddev")
        dq_mean_str = f"{dq_mean:.4f}" if dq_mean is not None else "N/A"
        dq_stddev_str = f"{dq_stddev:.4f}" if dq_stddev is not None else "N/A"
        critical_alerts.append(
            f"❌ **DirectionQuality Feedback Loop Collapse Detected!**\n"
            f"Reason: `{feedback['reason']}`\n"
            f"Stats (last 1000): mean={dq_mean_str}, stddev={dq_stddev_str}, unique={feedback['stats']['unique_values_count']}"
        )

    # Helper function to check thresholds
    def evaluate(name, score, warning_thresh, critical_thresh):
        if score < critical_thresh:
            critical_alerts.append(f"🚨 **Critical Breach: {name}** is **{score:.2f}%** (threshold: < {critical_thresh:.2f}%)")
        elif score < warning_thresh:
            warning_alerts.append(f"⚠️ **Warning Breach: {name}** is **{score:.2f}%** (threshold: < {warning_thresh:.2f}%)")

    evaluate("Overall Score", overall_score, 85.0, 70.0)
    evaluate("Completeness", comp["score"], 65.0, 50.0)
    evaluate("Freshness", fresh["score"], 95.0, 80.0)
    evaluate("Validity", val["score"], 99.0, 95.0)
    evaluate("Consistency", cons["score"], 99.0, 95.0)
    evaluate("Accuracy", acc["score"], 55.0, 50.0)

    # Route notifications
    if critical_alerts:
        msg_body = "\n".join(critical_alerts)
        full_critical_msg = (
            f"🟥 **[CRITICAL ALERT] Sycode Trading Data Quality Framework Breach**\n"
            f"Timestamp: `{scorecard['timestamp_utc']}`\n"
            f"Overall Score: **{overall_score:.2f}%**\n\n"
            f"{msg_body}"
        )
        send_discord_alert(full_critical_msg, "discord:#critical-alerts")
        # Send a summary to fleet-reports too
        send_discord_alert(f"🟥 **[CRITICAL BREACH]** Overall Score: **{overall_score:.2f}%** - Check `#critical-alerts` for details.", "discord:#fleet-reports")

    if warning_alerts and not critical_alerts:
        msg_body = "\n".join(warning_alerts)
        full_warning_msg = (
            f"🟨 **[WARNING ALERT] Sycode Trading Data Quality Framework Notice**\n"
            f"Timestamp: `{scorecard['timestamp_utc']}`\n"
            f"Overall Score: **{overall_score:.2f}%**\n\n"
            f"{msg_body}"
        )
        send_discord_alert(full_warning_msg, "discord:#fleet-reports")

    return critical_alerts, warning_alerts


def run_profiler():
    """Gathers all metrics, computes scores, and saves to scorecard.json."""
    completeness = compute_completeness()
    freshness = compute_freshness()
    validity = compute_validity()
    consistency = compute_consistency()
    accuracy = compute_accuracy()
    dq_feedback = compute_direction_quality_feedback()

    # Calculate overall score as simple average of the 5 criteria
    overall_score = (
        completeness["score"] +
        freshness["score"] +
        validity["score"] +
        consistency["score"] +
        accuracy["score"]
    ) / 5.0

    scorecard_data = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat() + "Z",
        "overall_score": round(overall_score, 2),
        "metrics": {
            "completeness": completeness,
            "freshness": freshness,
            "validity": validity,
            "consistency": consistency,
            "accuracy": accuracy,
            "direction_quality_feedback": dq_feedback
        }
    }

    # Save to scorecard.json
    SCORECARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=SCORECARD_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(scorecard_data, f, indent=2)
        os.replace(temp_path, SCORECARD_PATH)
    except Exception as e:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise e

    # Render current data quality state into Obsidian dashboard note
    try:
        generate_obsidian_dashboard(scorecard_data)
    except Exception as e:
        print(f"ERROR: Failed to generate Obsidian dashboard: {e}", file=sys.stderr)

    # Check warning/critical thresholds and send Discord alerts
    try:
        check_alert_thresholds(scorecard_data)
    except Exception as e:
        print(f"ERROR: Failed to run alert checks: {e}", file=sys.stderr)

    return scorecard_data


# ----------------------------------------------------------------------------
# UNIT TESTS (MOCKED FOR OFFLINE / PIPELINE STAGE RUNS)
# ----------------------------------------------------------------------------

class TestDataProfilerSentinel(unittest.TestCase):

    @patch("__main__.run_sql")
    def test_compute_completeness_perfect(self, mock_run_sql):
        mock_run_sql.side_effect = [
            # 1. counts
            [{"total_rows": "1000", "id_non_null": "1000", "correlation_id_non_null": "1000", 
              "symbol_non_null": "1000", "direction_non_null": "1000", "entry_price_non_null": "1000", 
              "triggered_at_non_null": "1000", "dqp_non_null": "1000"}],
            # 2. inactive
            [{"total_inactive": "400", "exit_price_non_null": "400", "realized_pnl_non_null": "400", "exit_type_non_null": "400"}],
            # 3. mature inactive
            [{"total_mature_inactive": "300", "clean_outcome_non_null": "300"}]
        ]
        res = compute_completeness()
        self.assertEqual(res["score"], 100.0)
        self.assertEqual(res["total_rows"], 1000)
        self.assertEqual(res["null_counts"]["direction_quality_prob"], 0)

    @patch("__main__.run_sql")
    def test_compute_completeness_with_nulls(self, mock_run_sql):
        mock_run_sql.side_effect = [
            # 1. counts (100 nulls in dqp out of 1000 total)
            [{"total_rows": "1000", "id_non_null": "1000", "correlation_id_non_null": "1000", 
              "symbol_non_null": "1000", "direction_non_null": "1000", "entry_price_non_null": "1000", 
              "triggered_at_non_null": "1000", "dqp_non_null": "900"}],
            # 2. inactive (all non-null out of 400 total)
            [{"total_inactive": "400", "exit_price_non_null": "400", "realized_pnl_non_null": "400", "exit_type_non_null": "400"}],
            # 3. mature inactive (all non-null out of 300 total)
            [{"total_mature_inactive": "300", "clean_outcome_non_null": "300"}]
        ]
        res = compute_completeness()
        # 1 column out of 11 has 10% nulls -> average null pct = (10.0 / 11) = 0.909%
        # Score = 100.0 - 0.909 = 99.09%
        self.assertEqual(res["score"], 99.09)
        self.assertEqual(res["null_counts"]["direction_quality_prob"], 100)
        self.assertEqual(res["null_percentages"]["direction_quality_prob"], 10.0)

    @patch("__main__.run_sql")
    def test_compute_freshness_healthy(self, mock_run_sql):
        mock_run_sql.side_effect = [
            [{"last_signal": "2026-07-07 10:00:00+00", "signal_lag_hours": "1.5"}],
            [{"last_outcome": "2026-07-07 09:30:00+00", "outcome_lag_hours": "2.0"}],
            [{"last_close": "2026-07-07 09:45:00+00", "close_lag_hours": "1.75"}]
        ]
        res = compute_freshness()
        self.assertEqual(res["score"], 100.0)
        self.assertEqual(res["signal_lag_hours"], 1.5)

    @patch("__main__.run_sql")
    def test_compute_freshness_stale(self, mock_run_sql):
        mock_run_sql.side_effect = [
            [{"last_signal": "2026-07-06 10:00:00+00", "signal_lag_hours": "25.5"}],
            [{"last_outcome": "2026-07-06 09:30:00+00", "outcome_lag_hours": "26.0"}],
            [{"last_close": "2026-07-06 09:45:00+00", "close_lag_hours": "25.75"}]
        ]
        res = compute_freshness()
        self.assertEqual(res["score"], 0.0)
        self.assertEqual(res["signal_lag_hours"], 25.5)

    @patch("__main__.run_sql")
    def test_compute_validity(self, mock_run_sql):
        mock_run_sql.side_effect = [
            [{"total_rows": "1000"}],
            [{"invalid_direction": "5", "invalid_entry_price": "2", "invalid_dqp": "0", "invalid_progress": "3"}]
        ]
        res = compute_validity()
        # 10 invalid entries out of 1000 rows -> 1.0% invalid -> validity pct = 99.0%
        self.assertEqual(res["score"], 99.0)
        self.assertEqual(res["invalid_rows_count"]["invalid_direction"], 5)

    @patch("__main__.run_sql")
    def test_compute_consistency_clean(self, mock_run_sql):
        mock_run_sql.return_value = [
            {"pnl_orphans": "0", "close_journey_orphans": "0", "close_position_orphans": "0"}
        ]
        res = compute_consistency()
        self.assertEqual(res["score"], 100.0)

    @patch("__main__.run_sql")
    def test_compute_consistency_with_orphans(self, mock_run_sql):
        mock_run_sql.return_value = [
            {"pnl_orphans": "2", "close_journey_orphans": "1", "close_position_orphans": "0"}
        ]
        res = compute_consistency()
        # 3 orphans total * 5 deduction = 15 point penalty -> score = 85.0
        self.assertEqual(res["score"], 85.0)

    @patch("__main__.run_sql")
    def test_compute_direction_quality_feedback_healthy(self, mock_run_sql):
        mock_run_sql.side_effect = [
            [{"count": "1000", "min_val": "0.1", "max_val": "0.9", "avg_val": "0.52", "std_val": "0.15", "unique_val_cnt": "950"}],
            [{"total_recent_journeys": "50"}]
        ]
        res = compute_direction_quality_feedback()
        self.assertFalse(res["collapsed"])
        self.assertEqual(res["reason"], "healthy")
        self.assertEqual(res["stats"]["count"], 1000)

    @patch("__main__.run_sql")
    def test_compute_direction_quality_feedback_collapsed_stddev(self, mock_run_sql):
        mock_run_sql.side_effect = [
            [{"count": "1000", "min_val": "0.499", "max_val": "0.501", "avg_val": "0.5", "std_val": "0.0002", "unique_val_cnt": "12"}],
            [{"total_recent_journeys": "50"}]
        ]
        res = compute_direction_quality_feedback()
        self.assertTrue(res["collapsed"])
        self.assertEqual(res["reason"], "standard_deviation_too_low")

    @patch("__main__.send_discord_alert")
    def test_check_alert_thresholds_healthy(self, mock_send_alert):
        scorecard = {
            "timestamp_utc": "2026-07-07T12:00:00Z",
            "overall_score": 95.0,
            "metrics": {
                "completeness": {"score": 95.0, "total_rows": 100, "null_counts": {"direction_quality_prob": 0}, "null_percentages": {"direction_quality_prob": 0.0}},
                "freshness": {"score": 100.0, "signal_lag_hours": 1.0, "outcome_lag_hours": 1.0, "trade_close_lag_hours": 1.0},
                "validity": {"score": 100.0, "invalid_rows_count": {"invalid_direction": 0, "invalid_entry_price": 0, "invalid_direction_quality_probability": 0, "invalid_progress_percent": 0}},
                "consistency": {"score": 100.0, "orphan_counts": {"pnl_points_orphans": 0, "trade_close_journeys_orphans": 0, "trade_close_positions_orphans": 0}},
                "accuracy": {"score": 60.0, "recent_1000": {"binary_accuracy_pct": 60.0, "brier_score": 0.2, "mean_absolute_error": 0.4}},
                "direction_quality_feedback": {"collapsed": False, "reason": "healthy", "stats": {"count": 1000, "mean": 0.5, "stddev": 0.1, "unique_values_count": 500, "recent_24h_journeys": 100}}
            }
        }
        critical, warnings = check_alert_thresholds(scorecard)
        self.assertEqual(len(critical), 0)
        self.assertEqual(len(warnings), 0)
        mock_send_alert.assert_not_called()

    @patch("__main__.send_discord_alert")
    def test_check_alert_thresholds_breaches(self, mock_send_alert):
        scorecard = {
            "timestamp_utc": "2026-07-07T12:00:00Z",
            "overall_score": 65.0,
            "metrics": {
                "completeness": {"score": 45.0, "total_rows": 100, "null_counts": {"direction_quality_prob": 55}, "null_percentages": {"direction_quality_prob": 55.0}},
                "freshness": {"score": 75.0, "signal_lag_hours": 25.0, "outcome_lag_hours": 1.0, "trade_close_lag_hours": 1.0},
                "validity": {"score": 100.0, "invalid_rows_count": {"invalid_direction": 0, "invalid_entry_price": 0, "invalid_direction_quality_probability": 0, "invalid_progress_percent": 0}},
                "consistency": {"score": 100.0, "orphan_counts": {"pnl_points_orphans": 0, "trade_close_journeys_orphans": 0, "trade_close_positions_orphans": 0}},
                "accuracy": {"score": 48.0, "recent_1000": {"binary_accuracy_pct": 48.0, "brier_score": 0.3, "mean_absolute_error": 0.5}},
                "direction_quality_feedback": {"collapsed": True, "reason": "standard_deviation_too_low", "stats": {"count": 1000, "mean": 0.5, "stddev": 0.0001, "unique_values_count": 5, "recent_24h_journeys": 100}}
            }
        }
        critical, warnings = check_alert_thresholds(scorecard)
        self.assertGreater(len(critical), 0)
        self.assertGreater(mock_send_alert.call_count, 0)


# ----------------------------------------------------------------------------
# MAIN EXECUTION ENTRY POINT
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Continuous Data Profiler & Drift Sentinel")
    parser.add_argument("--test", action="store_true", help="run unit tests with mocked DB")
    args = parser.parse_args()

    if args.test:
        print("Running unit tests...")
        sys.argv = [sys.argv[0]]  # Clear argv for unittest
        unittest.main()
        sys.exit(0)

    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] Starting SycodeTrading Data Profiling Sentinel...")

    try:
        scorecard = run_profiler()
    except Exception as e:
        print(f"FATAL: Operational error during data profiling: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\n--- DATA QUALITY SCORECARD ---")
    print(f"Overall Score:  {scorecard['overall_score']:.2f}%")
    print(f"  - Completeness: {scorecard['metrics']['completeness']['score']:.2f}%")
    print(f"  - Freshness:    {scorecard['metrics']['freshness']['score']:.2f}%")
    print(f"  - Validity:     {scorecard['metrics']['validity']['score']:.2f}%")
    print(f"  - Consistency:  {scorecard['metrics']['consistency']['score']:.2f}%")
    print(f"  - Accuracy:     {scorecard['metrics']['accuracy']['score']:.2f}%")

    feedback = scorecard['metrics']['direction_quality_feedback']
    print(f"\n--- DIRECTION QUALITY SENTINEL ---")
    print(f"Feedback Status: {'⚠️  COLLAPSED!' if feedback['collapsed'] else '✅ HEALTHY'}")
    print(f"Reason:          {feedback['reason']}")
    if feedback['stats']['count'] > 0:
        stats = feedback['stats']
        print(f"Distribution Statistics (last 1000):")
        print(f"  - Predictions Count: {stats['count']}")
        print(f"  - Mean Probability:  {stats['mean']:.4f}")
        print(f"  - Std Deviation:     {stats['stddev']:.4f}")
        print(f"  - Unique Values:     {stats['unique_values_count']}")
    else:
        print("  - No predictions written yet.")

    print(f"\nScorecard successfully written to {SCORECARD_PATH}")
    sys.exit(0)


if __name__ == "__main__":
    main()
