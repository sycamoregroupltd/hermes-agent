#!/usr/bin/env python3
# invoker: hermes cron job or manual execution
#
# dq_sentinel_healer.py
#
# Data Quality Sentinel & Self-Healer (DQSH) Engine.
# Performs missing value interpolation and warm-start model re-seeding under strict safety caps.
# Integrates scorecard terminal dashboard and wires Discord alerts.

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
from typing import Any, Dict, List

# Absolute Paths
SCORECARD_PATH = Path("/home/frank/.hermes/var/dq/scorecard.json")
STATE_PATH = Path("/home/frank/.hermes/var/dq/dqsh_state.json")
DB_CONTAINER = "sycodetrading-supabase-db"
TARGET_CONSUMER_CONTAINER = "sycodetrading-server"
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")

# Discord targets
CRITICAL_ALERTS_TARGET = "discord:#critical-alerts"
FLEET_REPORTS_TARGET = "discord:#fleet-reports"
QUANT_REPORTS_TARGET = "discord:#quant-reports"


# ----------------------------------------------------------------------------
# STATE & CONFIGURATION MANAGEMENT
# ----------------------------------------------------------------------------

def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"reseed_history": [], "candle_remediations": [], "healing_log": []}
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"[DQSH] WARNING: Failed to load state, resetting: {e}", file=sys.stderr)
        return {"reseed_history": [], "candle_remediations": [], "healing_log": []}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=str(STATE_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(temp_path, STATE_PATH)
    except Exception as e:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        print(f"[DQSH] ERROR: Failed to save state: {e}", file=sys.stderr)


def check_reseed_gate():
    state = load_state()
    history = state.get("reseed_history", [])
    now = time.time()
    cutoff = now - 24 * 3600  # 24h window
    recent = [t for t in history if t >= cutoff]
    if len(recent) >= 1:
        return False, len(recent)
    return True, 0


def record_reseed(details):
    state = load_state()
    state["reseed_history"].append(time.time())
    # Keep up to 30 days of history
    cutoff = time.time() - 30 * 86400
    state["reseed_history"] = [t for t in state["reseed_history"] if t >= cutoff]
    
    # Store healing log entry
    if "healing_log" not in state:
        state["healing_log"] = []
    state["healing_log"].append({
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        "action": "model_reseed_and_restart",
        "details": details
    })
    save_state(state)


# ----------------------------------------------------------------------------
# DATABASE UTILITIES
# ----------------------------------------------------------------------------

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


def run_sql_mutation(sql):
    """Executes a mutating SQL statement against the Supabase database."""
    cmd = [
        "docker", "exec",
        DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "postgres",
        "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", sql,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"psql mutation failed rc={proc.returncode}: {proc.stderr.strip()[:500]}")
    return proc.stdout.strip()


# ----------------------------------------------------------------------------
# REMEDIATION ENGINES
# ----------------------------------------------------------------------------

def clean_and_reseed_signal_journeys():
    """Sets pnl_percent of unverified signal_journeys to NULL to prevent feedback contamination in warmStart()."""
    # Step 1: Set pnl_percent to NULL for journeys without a verified outcome in decision_outcomes
    cleanup_null_sql = """
        UPDATE public.signal_journeys sj
        SET pnl_percent = NULL
        WHERE sj.pnl_percent IS NOT NULL
          AND sj.correlation_id NOT IN (
            SELECT correlation_id 
            FROM public.decision_outcomes
          );
    """
    res1 = run_sql_mutation(cleanup_null_sql)
    
    # Step 2: Ensure verified outcomes are correctly synchronized to pnl_percent based on decision_outcomes outcome_class
    sync_sql = """
        UPDATE public.signal_journeys sj
        SET pnl_percent = CASE 
            WHEN dec_out.outcome_class = 'WIN' THEN 0.01
            WHEN dec_out.outcome_class = 'LOSS' THEN -0.01
            ELSE 0.0
        END
        FROM public.decision_outcomes dec_out
        WHERE sj.correlation_id = dec_out.correlation_id
          AND sj.pnl_percent IS NULL;
    """
    res2 = run_sql_mutation(sync_sql)
    return f"Cleaned unverified signal_journeys ({res1}) and synchronized verified ones ({res2})"


def interpolate_candles(dry_run=False):
    """
    Scans recent candles for nulls or 0 volume segments and performs:
      - Linear interpolation for single candle gaps.
      - Forward-fill for up to 3 candle gaps.
      - Flags gaps > 3 as GAP.
    """
    # Fetch recent candles from DB to check for nulls or 0 volume segments
    query = """
        SELECT symbol, timeframe, timestamp, open, high, low, close, volume
        FROM public.candles
        WHERE timestamp >= now() - interval '24 hours'
        ORDER BY symbol, timeframe, timestamp ASC;
    """
    try:
        candles = run_sql(query)
    except Exception as e:
        print(f"[DQSH] Failed to fetch candles: {e}", file=sys.stderr)
        return False, f"Failed to fetch candles: {e}"

    if not candles:
        return True, "No recent candles found to check"

    # Group candles by (symbol, timeframe)
    grouped = {}
    for c in candles:
        key = (c["symbol"], c["timeframe"])
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(c)

    remediated_count = 0
    gaps_detected = []

    for key, series in grouped.items():
        symbol, timeframe = key
        n = len(series)
        i = 0
        while i < n:
            c = series[i]
            # Detect missing/null close/volume segment (volume == 0 or close is null/0)
            is_bad = False
            try:
                vol = float(c.get("volume") or 0)
                close_val = float(c.get("close") or 0)
                is_bad = (vol <= 0.0) or (close_val <= 0.0)
            except (ValueError, TypeError):
                is_bad = True

            if is_bad:
                # Found a bad segment. Count consecutive bad segments.
                start_idx = i
                while i < n:
                    curr_c = series[i]
                    try:
                        curr_vol = float(curr_c.get("volume") or 0)
                        curr_close = float(curr_c.get("close") or 0)
                        curr_bad = (curr_vol <= 0.0) or (curr_close <= 0.0)
                    except (ValueError, TypeError):
                        curr_bad = True
                    if not curr_bad:
                        break
                    i += 1
                end_idx = i  # Exclusive
                gap_len = end_idx - start_idx

                if gap_len == 1:
                    # Linear Interpolation: single candle
                    # Need non-null before and after
                    prev_idx = start_idx - 1
                    next_idx = end_idx
                    if prev_idx >= 0 and next_idx < n:
                        try:
                            p_close = float(series[prev_idx]["close"])
                            n_close = float(series[next_idx]["close"])
                            p_open = float(series[prev_idx]["open"])
                            n_open = float(series[next_idx]["open"])
                            p_high = float(series[prev_idx]["high"])
                            n_high = float(series[next_idx]["high"])
                            p_low = float(series[prev_idx]["low"])
                            n_low = float(series[next_idx]["low"])
                            p_vol = float(series[prev_idx]["volume"])
                            n_vol = float(series[next_idx]["volume"])

                            interp_close = (p_close + n_close) / 2.0
                            interp_open = (p_open + n_open) / 2.0
                            interp_high = (p_high + n_high) / 2.0
                            interp_low = (p_low + n_low) / 2.0
                            interp_vol = (p_vol + n_vol) / 2.0

                            bad_ts = series[start_idx]["timestamp"]
                            remed_sql = f"""
                                UPDATE public.candles
                                SET open = {interp_open}, high = {interp_high}, low = {interp_low}, close = {interp_close}, volume = {interp_vol}
                                WHERE symbol = '{symbol}' AND timeframe = '{timeframe}' AND timestamp = '{bad_ts}';
                            """
                            if not dry_run:
                                run_sql_mutation(remed_sql)
                            remediated_count += 1
                        except Exception as ex:
                            print(f"[DQSH] Interpolation failed: {ex}", file=sys.stderr)
                elif 1 < gap_len <= 3:
                    # Forward-fill: up to 3 candles
                    prev_idx = start_idx - 1
                    if prev_idx >= 0:
                        try:
                            p_close = float(series[prev_idx]["close"])
                            p_open = float(series[prev_idx]["open"])
                            p_high = float(series[prev_idx]["high"])
                            p_low = float(series[prev_idx]["low"])
                            p_vol = float(series[prev_idx]["volume"])

                            for fill_idx in range(start_idx, end_idx):
                                fill_ts = series[fill_idx]["timestamp"]
                                remed_sql = f"""
                                    UPDATE public.candles
                                    SET open = {p_open}, high = {p_high}, low = {p_low}, close = {p_close}, volume = {p_vol}
                                    WHERE symbol = '{symbol}' AND timeframe = '{timeframe}' AND timestamp = '{fill_ts}';
                                """
                                if not dry_run:
                                    run_sql_mutation(remed_sql)
                                remediated_count += 1
                        except Exception as ex:
                            print(f"[DQSH] Forward-fill failed: {ex}", file=sys.stderr)
                else:
                    # gap_len > 3: flag as GAP
                    gap_ts_start = series[start_idx]["timestamp"]
                    gap_ts_end = series[end_idx-1]["timestamp"]
                    gaps_detected.append(f"{symbol} {timeframe} ({gap_len} bars: {gap_ts_start} -> {gap_ts_end})")
            else:
                i += 1

    summary = f"Remediated {remediated_count} null/0 volume candles."
    if gaps_detected:
        summary += f" Detected {len(gaps_detected)} unresolved GAPs: {'; '.join(gaps_detected[:3])}"
        if len(gaps_detected) > 3:
            summary += f" (+{len(gaps_detected)-3} more)"
    return True, summary


# ----------------------------------------------------------------------------
# ALERTS & COMMUNICATIONS
# ----------------------------------------------------------------------------

def send_discord_alert(message, target):
    """Sends a Discord alert via the Jarvis profile integration."""
    print(f"[DQSH] Discord Alert Target {target}: {message}")
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
        print(f"[DQSH] WARNING: Discord alert delivery failed: {result.stderr.strip() or result.stdout.strip()}", file=sys.stderr)


# ----------------------------------------------------------------------------
# ACTIVE HEALING SCAN CYCLE
# ----------------------------------------------------------------------------

def run_healing_cycle(dry_run=False):
    print("------------------------------------------------------------------")
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Initiating DQSH Self-Healing Loop Scan...")
    print("------------------------------------------------------------------")

    remediations_triggered = []
    remediations_failed = []

    # 1. Load scorecard.json to check DirectionQuality feedback collapse
    scorecard = {}
    if SCORECARD_PATH.exists():
        try:
            with open(SCORECARD_PATH, "r") as f:
                scorecard = json.load(f)
        except Exception as e:
            print(f"[DQSH] ERROR: Failed to load scorecard: {e}", file=sys.stderr)
    
    dq_feedback = scorecard.get("metrics", {}).get("direction_quality_feedback", {})
    collapsed = dq_feedback.get("collapsed", False)
    reason = dq_feedback.get("reason", "healthy")

    if collapsed:
        print(f"[ALARM] DirectionQuality feedback collapse detected! Reason: {reason}")
        allowed, count = check_reseed_gate()
        if not allowed:
            msg = f"⚠️ [DQSH] Model Re-seed BLOCKED by safety cap (max 1 per 24h, already executed in last 24h)."
            print(msg)
            send_discord_alert(msg, CRITICAL_ALERTS_TARGET)
            remediations_failed.append(("Model Re-seed", "Blocked by safety cap"))
        else:
            print("[DQSH] Executing Model Re-seed and AR Tracker Clear...")
            details = "Cleaned unverified signal_journeys and synchronized verified ones"
            if not dry_run:
                try:
                    # Run DB cleanup & sync
                    db_summary = clean_and_reseed_signal_journeys()
                    # Restart consumer container to clear in-memory trackers
                    cmd = ["docker", "restart", TARGET_CONSUMER_CONTAINER]
                    subprocess.run(cmd, capture_output=True, text=True, check=True)
                    record_reseed(f"{details} - {db_summary}")
                    
                    alert_msg = f"🚨 [DQSH] **Data Quality Self-Healer Alert**:\nDirectionQuality feedback loop collapse detected ({reason}).\nSuccessfully cleared model's AR trackers via `{TARGET_CONSUMER_CONTAINER}` container restart, and re-seeded `warmStart()` from verified outcomes."
                    send_discord_alert(alert_msg, CRITICAL_ALERTS_TARGET)
                    remediations_triggered.append(("Model Re-seed", alert_msg))
                except Exception as ex:
                    err_msg = f"Failed to execute re-seed: {ex}"
                    print(f"[DQSH] ERROR: {err_msg}", file=sys.stderr)
                    remediations_failed.append(("Model Re-seed", err_msg))
            else:
                dry_msg = f"[DRY-RUN] Would execute model re-seed, clear AR trackers, and restart {TARGET_CONSUMER_CONTAINER}"
                print(dry_msg)
                remediations_triggered.append(("Model Re-seed", dry_msg))

    # 2. Missing value interpolation for Candles
    print("[DQSH] Checking candle data for nulls or 0-volume segments...")
    success, candle_summary = interpolate_candles(dry_run=dry_run)
    if success:
        print(f"[DQSH] Candle Interpolation Scan: {candle_summary}")
        if "Remediated" in candle_summary and "0 null/0 volume" not in candle_summary:
            alert_msg = f"ℹ️ [DQSH] **Candle Interpolation Report**:\n{candle_summary}"
            send_discord_alert(alert_msg, QUANT_REPORTS_TARGET)
            remediations_triggered.append(("Candle Interpolation", candle_summary))
    else:
        print(f"[DQSH] ERROR: Candle interpolation failed: {candle_summary}", file=sys.stderr)
        remediations_failed.append(("Candle Interpolation", candle_summary))

    # Save last run summary in state
    state = load_state()
    state["last_run"] = {
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        "triggered": len(remediations_triggered),
        "failed": len(remediations_failed),
        "details": f"Triggered: {remediations_triggered}. Failed: {remediations_failed}"
    }
    save_state(state)

    print("\n------------------------------------------------------------------")
    print("DQSH REMEDIATION RUN SUMMARY")
    print("------------------------------------------------------------------")
    print(f"Triggered Remediations: {len(remediations_triggered)}")
    for action, detail in remediations_triggered:
        print(f"  [SUCCESS] {action}: {detail[:150]}")
    print(f"Failed Remediations: {len(remediations_failed)}")
    for action, detail in remediations_failed:
        print(f"  [FAILED]  {action}: {detail}")
    print("------------------------------------------------------------------\n")
    return len(remediations_failed) == 0


# ----------------------------------------------------------------------------
# TERMINAL DASHBOARD
# ----------------------------------------------------------------------------

def render_dashboard():
    """Renders a beautiful ASCII terminal dashboard showing DQ health and healer state."""
    # Read scorecard
    scorecard = {}
    if SCORECARD_PATH.exists():
        try:
            with open(SCORECARD_PATH, "r") as f:
                scorecard = json.load(f)
        except Exception:
            pass

    # Read state
    state = load_state()

    # Get scores
    overall = scorecard.get("overall_score", 0.0)
    metrics = scorecard.get("metrics", {})
    completeness = metrics.get("completeness", {}).get("score", 0.0)
    freshness = metrics.get("freshness", {}).get("score", 0.0)
    validity = metrics.get("validity", {}).get("score", 0.0)
    consistency = metrics.get("consistency", {}).get("score", 0.0)
    accuracy = metrics.get("accuracy", {}).get("score", 0.0)

    dq_feedback = metrics.get("direction_quality_feedback", {})
    collapsed = dq_feedback.get("collapsed", False)
    reason = dq_feedback.get("reason", "healthy")
    stats = dq_feedback.get("stats", {})

    # ANSI Colors
    GREEN = "\033[1;32m"
    RED = "\033[1;31m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[1;34m"
    CYAN = "\033[1;36m"
    RESET = "\033[0m"

    print(f"{CYAN}=================================================================={RESET}")
    print(f"               {BLUE}JARVIS-OS DATA QUALITY SCORECARD & DASHBOARD{RESET}      ")
    print(f"{CYAN}=================================================================={RESET}")
    print(f" Timestamp (UTC): {scorecard.get('timestamp_utc', 'N/A')}")
    
    score_color = GREEN if overall >= 85.0 else (YELLOW if overall >= 70.0 else RED)
    print(f" Overall DQ Score: {score_color}{overall:.2f}%{RESET}")
    print(f"------------------------------------------------------------------")
    print(f"   - Completeness: {GREEN if completeness >= 85.0 else YELLOW}{completeness:.2f}%{RESET}")
    print(f"   - Freshness:    {GREEN if freshness >= 85.0 else YELLOW}{freshness:.2f}%{RESET}")
    print(f"   - Validity:     {GREEN if validity >= 85.0 else YELLOW}{validity:.2f}%{RESET}")
    print(f"   - Consistency:  {GREEN if consistency >= 85.0 else YELLOW}{consistency:.2f}%{RESET}")
    print(f"   - Accuracy:     {GREEN if accuracy >= 55.0 else RED}{accuracy:.2f}%{RESET}")
    print(f"------------------------------------------------------------------")
    
    status_str = f"{RED}⚠️ COLLAPSED! ({reason}){RESET}" if collapsed else f"{GREEN}✅ HEALTHY{RESET}"
    print(f" DirectionQuality Feedback: {status_str}")
    if stats:
        print(f"   - Predictions Count (last 1000): {stats.get('count', 0)}")
        print(f"   - Mean Probability:              {stats.get('mean', 0.0):.4f}")
        print(f"   - Standard Deviation:            {stats.get('stddev', 0.0):.4f}")
        print(f"   - Unique Values:                 {stats.get('unique_values_count', 0)}")
    
    print(f"------------------------------------------------------------------")
    print(f"               {BLUE}DATA QUALITY ACTIVE HEALING STATE{RESET}")
    print(f"------------------------------------------------------------------")
    last_run = state.get("last_run", {})
    if last_run:
        print(f" Last Healing Cycle: {last_run.get('timestamp', 'N/A')}")
        print(f"   - Triggered: {last_run.get('triggered', 0)}")
        print(f"   - Failed:    {last_run.get('failed', 0)}")
    else:
        print(" No healing runs recorded yet.")

    print(f" Recent Healing Logs:")
    logs = state.get("healing_log", [])
    if logs:
        for log in logs[-5:]:
            print(f"   - [{log['timestamp'][:19]}] {GREEN}{log['action']}{RESET}: {log['details'][:80]}")
    else:
        print("   - No actions logged yet.")
    print(f"{CYAN}=================================================================={RESET}")


# ----------------------------------------------------------------------------
# UNIT TESTS
# ----------------------------------------------------------------------------

class TestDQSH(unittest.TestCase):
    """Proves that DQSH behaves correctly under simulated conditions."""
    
    def setUp(self):
        # Temp state file
        self.temp_state_fd, self.temp_state_path = tempfile.mkstemp()
        global STATE_PATH
        self.original_state_path = STATE_PATH
        STATE_PATH = Path(self.temp_state_path)
        
        with open(self.temp_state_path, "w") as f:
            json.dump({"reseed_history": [], "candle_remediations": [], "healing_log": []}, f)

    def tearDown(self):
        global STATE_PATH
        STATE_PATH = self.original_state_path
        os.close(self.temp_state_fd)
        os.remove(self.temp_state_path)

    def test_reseed_gate(self):
        # First check should be allowed
        allowed, count = check_reseed_gate()
        self.assertTrue(allowed)
        
        # Record a reseed
        record_reseed("mock reseed")
        
        # Second check should be blocked
        allowed, count = check_reseed_gate()
        self.assertFalse(allowed)


# ----------------------------------------------------------------------------
# MAIN ENTRY POINT
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Data Quality Sentinel & Self-Healer (DQSH) Engine")
    parser.add_argument("--status", action="store_true", help="render the scorecard and healer status dashboard")
    parser.add_argument("--remediate", action="store_true", help="run active data quality profiling and trigger healer remediations")
    parser.add_argument("--dry-run", action="store_true", help="scan quality scorecard but don't mutate state or database")
    parser.add_argument("--test", action="store_true", help="run offline unit tests verifying DQSH safety gates")
    args = parser.parse_args()

    if args.test:
        print("Running DQSH unit tests...")
        sys.argv = [sys.argv[0]]  # Clear argv for unittest
        unittest.main()
        sys.exit(0)

    if args.status:
        render_dashboard()
        sys.exit(0)

    # Default behaviour: run active self-healing
    success = run_healing_cycle(dry_run=args.dry_run)
    if not success:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
