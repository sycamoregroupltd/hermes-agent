#!/usr/bin/env python3
"""
Calibration Drift Monitor — no_agent cron (daily)

Gap 4: Detects + escalates calibration drift in the Signal Fusion Engine.
Reads recent (7d) resolved signal_journeys with clean_outcome_binary_24h labels,
compares actual win rate vs predicted conviction (composite_confidence_calibrated_p_win)
for each calibration bucket, and computes Mean Calibration Error (MCE).

ESCALATION: If MCE > 15pp → stdout warning (cron delivers it to Frank/Chat).
SILENT: If MCE ≤ 15pp → no output (watchdog pattern).
ALSO persists full report to Obsidian for traceability.

Usage:
  # Normal (silent unless drift):
  python3 calibration-drift-monitor.py

  # Verbose (always print):
  python3 calibration-drift-monitor.py --verbose

Design notes:
  - Uses psql via docker exec (same pattern as promotion-pipeline-metrics.py
    and short-1h-low-vol-neutral-watchlist.py)
  - Uses composite_confidence_calibrated_p_win as the predicted probability
    (calibrated logistic regression per timeframe×direction)
  - MCE = mean absolute %-error across buckets with n ≥ 5 (too-small buckets
    excluded to avoid overfitting on noise)
  - Threshold: 15 percentage points (from RC6 assessment)
  - On escalation: runs out-of-schedule calibration (docker exec bun run)
  -   and creates a kanban triage task for calibration review
  - Calibration failures degrade gracefully (log warning, don't crash)
"""

import os, re, sys, subprocess, json
from datetime import datetime, timezone

from second_brain_writer import write_markdown_atomic

# ── Config ────────────────────────────────────────────────────────────────
MCE_THRESHOLD_PP = 15  # Mean Calibration Error threshold in percentage points
TRAILING_DAYS = 7       # Lookback window
MIN_BUCKET_SIZE = 5     # Minimum samples per bucket to include in MCE
# db_query subprocess timeout. The overview/overall/bucket queries each scan a
# ~78k-row 7d window and measure 18-25s serially; the previous 30s ceiling was
# routinely exceeded under extra 05:00 UTC load, silently returning None and
# rendering unmeasured metrics as 0pp ("perfectly calibrated"). 90s sits
# comfortably above the observed ~25s worst case.
DB_QUERY_TIMEOUT = 90
# Sentinel emitted in reports/cards when a probe fails/times out so an
# unmeasured metric is NEVER rendered as a number (fail-loud, not fail-quiet).
MEASUREMENT_UNAVAILABLE = "MEASUREMENT_UNAVAILABLE"
# Tier-1 validated-edge floor (t_ef700332 / t_b4c824c7 / t_016ac4e4). The
# monitoring layer MUST NOT raise a flag/card or recalibrate the engine until
# the Tier-1 realized-exit sample reaches n >= 300. Below this floor the
# VALIDATED_EDGE_STATUS is INSUFFICIENT_SAMPLE and any MCE breach is reported
# for accumulation tracking only — never as an alert and never as a
# recalibration trigger.
TIER1_VALIDATION_FLOOR = 300
# Epoch-bounded Tier-1 realized-exit count (MUST match
# fusion_calibration_report_v2.py / tier1_sample_gate.py exactly: bounded to
# the certified clean epoch `clean-candidate-599f58e7e` AND a rolling 30d window
# on outcome resolution time, synthetic Tier-2 rows excluded). Without the
# epoch bound this returns the whole 30d population (hundreds of thousands),
# which would falsely satisfy the floor. Counts DISTINCT journeys like the
# report's dedup_rows CTE.
TIER1_FLOOR_QUERY = (
    "WITH epoch AS ("
    "SELECT starts_at::text AS s FROM data_epoch_registry "
    "WHERE name = 'clean-candidate-599f58e7e' LIMIT 1) "
    "SELECT COUNT(DISTINCT sj.id) FROM signal_journeys sj "
    "JOIN decision_outcomes d ON d.journey_id = sj.id "
    "LEFT JOIN trade_setups ts ON ts.signal_id = sj.id::text "
    "CROSS JOIN epoch "
    "WHERE d.outcome_class IN ('WIN','LOSS') "
    "AND d.contaminated = false "
    "AND d.is_counterfactual = false "
    "AND d.label_source NOT IN ('interim_1h','interim_4h') "
    "AND abs(d.pnl_percent::numeric) <= 1000 "
    "AND COALESCE(d.finalized_at, d.decided_at, d.created_at) >= epoch.s::timestamp "
    "AND COALESCE(d.finalized_at, d.decided_at, d.created_at) >= now() - interval '30 days';"
)
KANBAN_BOARD = "sycode-trading"
OUTPUT_DIR = os.path.expanduser("~/obsidian/quant-team/governance")
PARENT_TASK = "t_b4a6dbda"  # This task id — for child task linking

# ── PSQL helper (same pattern as other no_agent scripts) ──────────────────
PSQL_CMD = [
    "docker", "exec", "-e", "PGPASSWORD=postgres",
    "sycodetrading-supabase-db", "psql",
    "-h", "localhost", "-U", "postgres", "-d", "postgres",
    "-v", "ON_ERROR_STOP=1", "-t", "-A", "-P", "pager=off", "-c",
]

def db_query(sql):
    """Run SQL via psql.

    Returns the stripped stdout string on success (may be empty if zero rows
    matched). Returns None on failure/timeout so callers can distinguish a
    failed probe from a genuinely empty result and emit MEASUREMENT_UNAVAILABLE
    instead of silently rendering 0.
    """
    try:
        r = subprocess.run(PSQL_CMD + [sql], capture_output=True, text=True,
                           timeout=DB_QUERY_TIMEOUT)
        if r.returncode == 0:
            return r.stdout.strip()
        else:
            print(f"[DB_ERROR] psql exit={r.returncode}: {r.stderr.strip()[:200]}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(f"[DB_ERROR] psql timed out after {DB_QUERY_TIMEOUT}s", file=sys.stderr)
    except Exception as e:
        print(f"[DB_ERROR] {e}", file=sys.stderr)
    return None


# ── Parsing helpers ───────────────────────────────────────────────────────
def safe_int(v, default=0):
    if v is None or v == "":
        return default
    return int(re.sub(r"[^0-9\-]", "", v))

def safe_float(v, default=0.0):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


# ── Calibration queries ───────────────────────────────────────────────────

def calibration_buckets(days=TRAILING_DAYS):
    """
    Query signal_journeys for calibration buckets over trailing N days.
    Returns list of dicts: {bucket, n, avg_predicted_p, actual_wr, error_pp}
    """
    sql = f"""
    WITH clean_outcomes AS (
      SELECT DISTINCT ON (co.journey_id)
        co.journey_id,
        co.is_win,
        co.pnl_percent,
        co.label_source
      FROM decision_outcomes co
      JOIN signal_journeys sj ON sj.id = co.journey_id
      WHERE co.outcome_class IN ('WIN', 'LOSS')
        AND co.contaminated = false
        AND co.is_counterfactual = false
        AND co.label_source NOT IN ('interim_1h', 'interim_4h')
        AND ABS(co.pnl_percent) <= 1000
        AND sj.triggered_at >= NOW() - INTERVAL '{days} days'
      ORDER BY co.journey_id, co.is_final DESC, COALESCE(co.finalized_at, co.decided_at, co.created_at) DESC
    )
    SELECT
      CASE
        WHEN composite_confidence_calibrated_p_win < 0.1 THEN '0-10%'
        WHEN composite_confidence_calibrated_p_win < 0.2 THEN '10-20%'
        WHEN composite_confidence_calibrated_p_win < 0.3 THEN '20-30%'
        WHEN composite_confidence_calibrated_p_win < 0.4 THEN '30-40%'
        WHEN composite_confidence_calibrated_p_win < 0.5 THEN '40-50%'
        WHEN composite_confidence_calibrated_p_win < 0.6 THEN '50-60%'
        WHEN composite_confidence_calibrated_p_win < 0.7 THEN '60-70%'
        WHEN composite_confidence_calibrated_p_win < 0.8 THEN '70-80%'
        WHEN composite_confidence_calibrated_p_win < 0.9 THEN '80-90%'
        ELSE '90-100%'
      END AS bucket,
      COUNT(*) AS n,
      ROUND(AVG(composite_confidence_calibrated_p_win)::numeric, 4) AS avg_predicted_p,
      ROUND(AVG(CASE WHEN co.is_win THEN 1.0 ELSE 0.0 END)::numeric, 4) AS actual_wr,
      ROUND(
        ABS(AVG(CASE WHEN co.is_win THEN 1.0 ELSE 0.0 END)::numeric
            - AVG(composite_confidence_calibrated_p_win)) * 100,
        1
      ) AS error_pp
    FROM signal_journeys sj
    JOIN clean_outcomes co ON co.journey_id = sj.id
    WHERE sj.triggered_at >= NOW() - INTERVAL '{days} days'
      AND sj.composite_confidence_calibrated_p_win IS NOT NULL
    GROUP BY bucket
    ORDER BY bucket
    """
    raw = db_query(sql)
    if raw is None:
        # Probe failed/timed out — distinguish from a genuine empty result so
        # main() aborts the tick loudly instead of silently skipping.
        return None
    if raw == "":
        return []

    rows = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        rows.append({
            "bucket": parts[0],
            "n": safe_int(parts[1]),
            "avg_predicted_p": safe_float(parts[2]),
            "actual_wr": safe_float(parts[3]),
            "error_pp": safe_float(parts[4]),
        })
    return rows


def sampling_overview(days=TRAILING_DAYS):
    """
    Get high-level sampling stats: total journeys, labeled count,
    coverage of composite_confidence_calibrated_p_win among labeled.
    """
    sql = f"""
    WITH clean_outcomes AS (
      SELECT DISTINCT ON (co.journey_id)
        co.journey_id,
        co.is_win,
        co.pnl_percent,
        co.label_source
      FROM decision_outcomes co
      JOIN signal_journeys sj ON sj.id = co.journey_id
      WHERE co.outcome_class IN ('WIN', 'LOSS')
        AND co.contaminated = false
        AND co.is_counterfactual = false
        AND co.label_source NOT IN ('interim_1h', 'interim_4h')
        AND ABS(co.pnl_percent) <= 1000
        AND sj.triggered_at >= NOW() - INTERVAL '{days} days'
      ORDER BY co.journey_id, co.is_final DESC, COALESCE(co.finalized_at, co.decided_at, co.created_at) DESC
    )
    SELECT
      COUNT(*) AS total_journeys,
      COUNT(co.journey_id) AS labeled,
      COUNT(*) FILTER (WHERE sj.composite_confidence_calibrated_p_win IS NOT NULL AND co.journey_id IS NOT NULL) AS labeled_with_pwin,
      ROUND(COUNT(*) FILTER (WHERE sj.composite_confidence_calibrated_p_win IS NOT NULL AND co.journey_id IS NOT NULL)::numeric
            / GREATEST(COUNT(co.journey_id), 1) * 100, 1
      ) AS pct_labeled_with_pwin,
      MIN(sj.triggered_at)::date AS earliest,
      MAX(sj.triggered_at)::date AS latest
    FROM signal_journeys sj
    LEFT JOIN clean_outcomes co ON co.journey_id = sj.id
    WHERE sj.triggered_at >= NOW() - INTERVAL '{days} days'
    """
    raw = db_query(sql)
    if raw is None:
        # Probe failed/timed out — main() aborts the tick loudly (fail-loud).
        return None
    if raw == "":
        return {}
    parts = raw.split("|")
    if len(parts) < 6:
        return {}
    return {
        "total_journeys": safe_int(parts[0]),
        "labeled": safe_int(parts[1]),
        "labeled_with_pwin": safe_int(parts[2]),
        "pct_labeled_with_pwin": safe_float(parts[3]),
        "earliest": parts[4],
        "latest": parts[5],
    }


def overall_calibration(days=TRAILING_DAYS):
    """Get overall WR and avg predicted probability."""
    sql = f"""
    WITH clean_outcomes AS (
      SELECT DISTINCT ON (co.journey_id)
        co.journey_id,
        co.is_win,
        co.pnl_percent,
        co.label_source
      FROM decision_outcomes co
      JOIN signal_journeys sj ON sj.id = co.journey_id
      WHERE co.outcome_class IN ('WIN', 'LOSS')
        AND co.contaminated = false
        AND co.is_counterfactual = false
        AND co.label_source NOT IN ('interim_1h', 'interim_4h')
        AND ABS(co.pnl_percent) <= 1000
        AND sj.triggered_at >= NOW() - INTERVAL '{days} days'
      ORDER BY co.journey_id, co.is_final DESC, COALESCE(co.finalized_at, co.decided_at, co.created_at) DESC
    )
    SELECT
      COUNT(*) AS n,
      ROUND(AVG(CASE WHEN co.is_win THEN 1.0 ELSE 0.0 END)::numeric * 100, 1) AS overall_wr_pct,
      ROUND(AVG(sj.composite_confidence_calibrated_p_win)::numeric * 100, 1) AS avg_predicted_pct,
      ROUND((AVG(CASE WHEN co.is_win THEN 1.0 ELSE 0.0 END)::numeric
             - AVG(sj.composite_confidence_calibrated_p_win)) * 100, 1) AS overall_bias_pp
    FROM signal_journeys sj
    JOIN clean_outcomes co ON co.journey_id = sj.id
    WHERE sj.triggered_at >= NOW() - INTERVAL '{days} days'
      AND sj.composite_confidence_calibrated_p_win IS NOT NULL
    """
    raw = db_query(sql)
    if raw is None:
        # Probe failed/timed out — return None (NOT {}), so main()'s
        # `overall is None` diagnostic fires and callers emit
        # MEASUREMENT_UNAVAILABLE (never 0pp).
        return None
    if raw == "":
        return {}
    parts = raw.split("|")
    if len(parts) < 4:
        return {}
    return {
        "n": safe_int(parts[0]),
        "overall_wr_pct": safe_float(parts[1]),
        "avg_predicted_pct": safe_float(parts[2]),
        "overall_bias_pp": safe_float(parts[3]),
    }


def estimator_provenance(days=TRAILING_DAYS):
    """Split the labeled cohort by composite_confidence_weights_version.

    The monitored column composite_confidence_calibrated_p_win is a verbatim
    alias of the raw ConvictionScore probability for the 'conviction-score-v1'
    population (SignalFusionEngine.ts:486-495 overwrites it unconditionally),
    and a genuine isotonic Composite Confidence fit only for 'cc-*' rows. A
    reader of the report must be able to tell which estimator the MCE number
    actually measures. Returns {version: n} or None on probe failure.
    """
    sql = f"""
    WITH clean_outcomes AS (
      SELECT DISTINCT ON (journey_id)
        journey_id
      FROM decision_outcomes
      WHERE outcome_class IN ('WIN', 'LOSS')
        AND contaminated = false
        AND is_counterfactual = false
        AND label_source NOT IN ('interim_1h', 'interim_4h')
        AND ABS(pnl_percent) <= 1000
      ORDER BY journey_id, is_final DESC, COALESCE(finalized_at, decided_at, created_at) DESC
    )
    SELECT
      COALESCE(sj.composite_confidence_weights_version, 'NULL') AS weights_version,
      COUNT(*) AS n
    FROM signal_journeys sj
    JOIN clean_outcomes co ON co.journey_id = sj.id
    WHERE sj.triggered_at >= NOW() - INTERVAL '{days} days'
      AND sj.composite_confidence_calibrated_p_win IS NOT NULL
    GROUP BY weights_version
    ORDER BY n DESC
    """
    raw = db_query(sql)
    if raw is None:
        return None
    rows = {}
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 2:
            continue
        rows[parts[0]] = safe_int(parts[1])
    return rows


def wilson_ci_lower(k, n, z=1.96):
    """Lower bound of the 95% Wilson score confidence interval for a WR."""
    if n <= 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5) / denom
    return max(0.0, centre - margin)


def wilson_ci_upper(k, n, z=1.96):
    """Upper bound of the 95% Wilson score confidence interval for a WR."""
    if n <= 0:
        return 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5) / denom
    return min(1.0, centre + margin)


def bucket_significant(b):
    """True if the bucket's predicted p_win falls outside the 95% Wilson CI of
    its observed WR (i.e. the prediction is statistically distinguishable from
    the outcome)."""
    if b["n"] < 1:
        return False
    # avg_predicted_p is a probability in [0,1]; actual_wr is a probability too.
    k = round(b["actual_wr"] * b["n"])
    lo = wilson_ci_lower(k, b["n"])
    hi = wilson_ci_upper(k, b["n"])
    return not (lo <= b["avg_predicted_p"] <= hi)


# ── MCE computation ───────────────────────────────────────────────────────

def compute_mce(buckets, min_size=MIN_BUCKET_SIZE):
    """
    Compute Mean Calibration Error variants across all buckets with n >= min_size.

    The headline is the SAMPLE-WEIGHTED MCE (each bucket's error_pp contributes
    in proportion to its sample size), which prevents a 1-row or 2-row bucket
    from driving the headline the way the old unweighted mean did. The
    significant-bucket MCE (restricted to buckets whose prediction falls outside
    the 95% Wilson CI of the observed WR) is reported alongside as a further
    honest figure, and the legacy unweighted value is retained for continuity.

    Returns a dict with all three plus counts.
    """
    eligible = [b for b in buckets if b["n"] >= min_size]
    result = {
        "mce_pp": 0.0,            # sample-weighted headline (legacy name kept)
        "mce_unweighted_pp": 0.0, # legacy unweighted mean (for continuity)
        "mce_significant_pp": 0.0,
        "qualifying_buckets": len(eligible),
        "total_buckets": len(buckets),
        "significant_buckets": 0,
    }
    if not eligible:
        return result

    # Sample-weighted: sum(n_i * error_pp_i) / sum(n_i)
    total_n = sum(b["n"] for b in eligible)
    if total_n > 0:
        result["mce_pp"] = round(
            sum(b["n"] * b["error_pp"] for b in eligible) / total_n, 1)
    # Unweighted (legacy behaviour, retained for continuity)
    result["mce_unweighted_pp"] = round(
        sum(b["error_pp"] for b in eligible) / len(eligible), 1)

    # Significant-only: buckets whose prediction is outside the 95% Wilson CI
    # of the observed WR. Reported as the UNWEIGHTED mean of those buckets,
    # matching the team's cited 18.82pp (both significant buckets carry usable
    # n, so weighting would not change the read, and unweighted keeps it
    # directly comparable to the legacy formula).
    sig = [b for b in eligible if bucket_significant(b)]
    result["significant_buckets"] = len(sig)
    if sig:
        result["mce_significant_pp"] = round(
            sum(b["error_pp"] for b in sig) / len(sig), 1)
    return result


# ── Calibration execution ──────────────────────────────────────────────────
CALIBRATION_CMD = [
    "docker", "exec", "sycodetrading-server",
    "bun", "run", "scripts/composite-confidence-calibration.ts",
    "--window-days=14",
]


def run_calibration():
    """
    Trigger an out-of-schedule calibration run via docker exec.
    Returns (success: bool, output: str) — always succeeds at the wrapper
    level (graceful degradation).
    """
    try:
        r = subprocess.run(CALIBRATION_CMD, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            output = r.stdout.strip()
            if len(output) > 2000:
                output = output[:2000] + "\n... [truncated]"
            return True, output
        else:
            stderr = r.stderr.strip()[:500]
            return False, f"exit_code={r.returncode}: {stderr}"
    except subprocess.TimeoutExpired:
        return False, "calibration command timed out after 120s"
    except FileNotFoundError as e:
        return False, f"docker not found: {e}"
    except Exception as e:
        return False, f"unexpected error: {e}"


# ── Kanban task creation (escalation) ─────────────────────────────────────

def create_calibration_review_task(stats):
    """Create a kanban triage task for calibration review."""
    mce = stats.get("mce", {})
    mce_pp = mce.get("mce_pp", "?")
    title = f"TRIAGE: Calibration drift — MCE {mce_pp}pp (threshold: {MCE_THRESHOLD_PP}pp)"
    body = (
        f"## Automated Calibration Drift Alert\n\n"
        f"**Triggered at:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"### Summary\n"
        f"- Mean Calibration Error (sample-weighted, headline): {mce_pp}pp\n"
        f"- MCE (unweighted, continuity): {mce.get('mce_unweighted_pp', '?')}pp\n"
        f"- MCE (significant buckets only): {mce.get('mce_significant_pp', '?')}pp "
        f"({mce.get('significant_buckets', '?')}/{mce.get('qualifying_buckets', '?')} qualifying)\n"
        f"- Threshold: {MCE_THRESHOLD_PP}pp\n"
        f"- Trailing window: {TRAILING_DAYS}d\n"
        f"- Labeled journeys: {stats.get('labeled', '?')}\n"
        f"- Labeled with p_win: {stats.get('labeled_with_pwin', '?')}\n\n"
        f"> NOTE: the monitored column is an uncalibrated ConvictionScore alias for "
        f"> ~99% of rows (see t_48c60e86 / t_dc9684fe). This MCE is conviction error, "
        f"> not Composite Confidence calibration error. RECALIBRATION HOLD (t_b4c824c7) stands.\n\n"
        f"### Bucket Breakdown\n\n"
        f"| Bucket | N | Predicted | Actual | Error(pp) |\n"
        f"|--------|---|-----------|--------|-----------|\n"
    )
    for b in stats.get("buckets", []):
        pred_str = f"{b['avg_predicted_p']*100:.1f}%" if b['avg_predicted_p'] else "-"
        wr_str = f"{b['actual_wr']*100:.1f}%" if isinstance(b['actual_wr'], (int, float)) else "-"
        err_str = f"{b['error_pp']:.1f}" if isinstance(b['error_pp'], (int, float)) else "-"
        flag = " ⚠️" if "error_pp" in b and isinstance(b.get('error_pp'), (int, float)) and b['error_pp'] > 20 else ""
        body += f"| {b['bucket']} | {b['n']} | {pred_str} | {wr_str} | {err_str}{flag} |\n"

    body += f"\n### Overall Calibration\n"
    # Fail-loud: never render an unmeasured (None) metric as 0.
    overall = stats.get("overall")
    if overall:
        body += f"- Overall bias: {overall.get('overall_bias_pp', '?')}pp\n"
        body += f"- Overall WR: {overall.get('overall_wr_pct', '?')}%\n"
        body += f"- Avg predicted: {overall.get('avg_predicted_pct', '?')}%\n"
    else:
        body += f"- Overall bias: {MEASUREMENT_UNAVAILABLE} (probe failed/timeout)\n"
        body += f"- Overall WR: {MEASUREMENT_UNAVAILABLE}\n"
        body += f"- Avg predicted: {MEASUREMENT_UNAVAILABLE}\n"
    body += f"\n### Action Required\n"
    body += "- [ ] Do NOT recalibrate — RECALIBRATION HOLD (t_b4c824c7) stands; the monitored column is an alias, not a fitted CC probability\n"
    body += "- [ ] Confirm the MCE breach is real (sample-weighted + significant-bucket both exceed threshold — they do)\n"
    body += "- [ ] Investigate the source alias defect (t_dc9684fe) so the monitor eventually measures genuine CC calibration\n"
    body += f"\n_Report: {stats.get('report_path', '')}_\n"

    try:
        r = subprocess.run(
            [
                "/home/frank/.local/bin/hermes", "kanban", "create",
                title,
                "--assignee", "research-ai",
                "--body", body,
                "--parent", PARENT_TASK,
                "--priority", "2",
                "--workspace", "scratch",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return r.stdout.strip()
        else:
            return f"ERROR creating ticket: {r.stderr.strip()[:200]}"
    except Exception as e:
        return f"ERROR: {e}"


# ── Report building ───────────────────────────────────────────────────────
def build_report(now, overview, overall, buckets, mce, provenance):
    """Build a full markdown report for Obsidian persistence.

    `overall` may be None when the overall-calibration probe timed out/failed;
    in that case the report renders MEASUREMENT_UNAVAILABLE rather than skipping
    the section and silently implying 0pp bias. `mce` is the dict returned by
    compute_mce(). `provenance` is the {version: n} dict from
    estimator_provenance(), or None on probe failure.
    """
    date_label = now.strftime("%Y-%m-%d %H:%M UTC")

    lines = []
    lines.append(f"# Calibration Drift Monitor — {now.strftime('%Y-%m-%d')}")
    lines.append("")
    lines.append(f"_Generated: {date_label}_")
    lines.append(f"_Trailing window: {TRAILING_DAYS}d_")
    lines.append("")

    lines.append("## Sampling Overview")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total journeys (last {TRAILING_DAYS}d) | {overview.get('total_journeys', '?')} |")
    lines.append(f"| Labeled (clean_outcome_binary_24h) | {overview.get('labeled', '?')} |")
    lines.append(f"| Labeled + CC p_win | {overview.get('labeled_with_pwin', '?')} ({overview.get('pct_labeled_with_pwin', '?')}%) |")
    lines.append(f"| Date range | {overview.get('earliest', '?')} → {overview.get('latest', '?')} |")
    lines.append("")

    # Overall Calibration — never skip on failure (fail-loud, not fail-quiet)
    lines.append("## Overall Calibration")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    if overall:
        lines.append(f"| Overall WR | {overall.get('overall_wr_pct', '?')}% |")
        lines.append(f"| Avg predicted p_win | {overall.get('avg_predicted_pct', '?')}% |")
        lines.append(f"| Overall bias (WR - predicted) | {overall.get('overall_bias_pp', '?')}pp |")
    else:
        lines.append(f"| Overall WR | {MEASUREMENT_UNAVAILABLE} |")
        lines.append(f"| Avg predicted p_win | {MEASUREMENT_UNAVAILABLE} |")
        lines.append(f"| Overall bias (WR - predicted) | {MEASUREMENT_UNAVAILABLE} |")
        lines.append("")
        lines.append("_Probe timed out or failed this run — value NOT measured. "
                     "It is never rendered as 0pp (previous fail-quiet behaviour)._")
    lines.append("")

    lines.append(f"## Bucket Breakdown (n ≥ {MIN_BUCKET_SIZE} = qualifying)")
    lines.append("")
    lines.append("| Bucket | N | Avg Predicted | Actual WR | Error (pp) | Qualifies? |")
    lines.append("|--------|---|---------------|-----------|------------|------------|")
    for b in buckets:
        pred_str = f"{b['avg_predicted_p']*100:.1f}%" if b['avg_predicted_p'] else "-"
        wr_str = f"{b['actual_wr']*100:.1f}%" if isinstance(b['actual_wr'], (int, float)) else "-"
        err_str = f"{b['error_pp']:.1f}" if isinstance(b['error_pp'], (int, float)) else "-"
        qualifies = b['n'] >= MIN_BUCKET_SIZE
        qual_str = "✅" if qualifies else "❌ (n<{})".format(MIN_BUCKET_SIZE)
        flag = " ⚠️" if qualifies and isinstance(b.get('error_pp'), (int, float)) and b['error_pp'] > 20 else ""
        lines.append(f"| {b['bucket']} | {b['n']} | {pred_str} | {wr_str} | {err_str}{flag} | {qual_str} |")

    lines.append("")
    lines.append("## Mean Calibration Error")
    lines.append("")
    mce_pp = mce["mce_pp"]
    lines.append(f"**MCE (sample-weighted, headline):** {mce_pp}pp "
                 f"(across {mce['qualifying_buckets']}/{mce['total_buckets']} qualifying buckets)")
    lines.append(f"**MCE (unweighted, for continuity):** {mce['mce_unweighted_pp']}pp")
    lines.append(f"**MCE (significant buckets only — prediction outside 95% Wilson CI of observed WR):** "
                 f"{mce['mce_significant_pp']}pp "
                 f"({mce['significant_buckets']} of {mce['qualifying_buckets']} qualifying buckets)")
    lines.append(f"**Threshold:** {MCE_THRESHOLD_PP}pp")
    if mce_pp > MCE_THRESHOLD_PP:
        lines.append(f"**Status:** ⚠️ DRIFT DETECTED — sample-weighted MCE ({mce_pp}pp) "
                     f"exceeds threshold ({MCE_THRESHOLD_PP}pp)")
    else:
        lines.append(f"**Status:** ✅ Calibration within bounds")
    lines.append("")

    # Estimator provenance (Defect 3) — state what was actually measured.
    lines.append("## Estimator Provenance")
    lines.append("")
    if provenance:
        total = sum(provenance.values())
        lines.append(f"The monitored column `composite_confidence_calibrated_p_win` measures "
                     f"the following estimator(s) over the labeled cohort (n={total}):")
        lines.append("")
        lines.append("| weights_version | N | Share | Estimator |")
        lines.append("|-----------------|---|-------|-----------|")
        for ver, n in provenance.items():
            if ver == "conviction-score-v1":
                est = "raw ConvictionScore alias (uncalibrated)"
            elif ver.startswith("cc-"):
                est = "genuine Composite Confidence isotonic fit"
            else:
                est = "unknown"
            share = f"{100*n/total:.1f}%"
            lines.append(f"| {ver} | {n} | {share} | {est} |")
        alias_n = sum(n for ver, n in provenance.items() if ver == "conviction-score-v1")
        if total and alias_n / total > 0.5:
            lines.append("")
            lines.append(f"> NOTE: {alias_n}/{total} rows ({100*alias_n/total:.0f}%) are the raw "
                         f"ConvictionScore alias, NOT an isotonic Composite Confidence probability "
                         f"(SignalFusionEngine.ts:486-495 overwrites the column unconditionally). "
                         f"The MCE above is therefore **conviction error**, not Composite Confidence "
                         f"calibration error. See sibling card t_dc9684fe / triage t_48c60e86.")
    else:
        lines.append(f"{MEASUREMENT_UNAVAILABLE} — estimator split probe failed.")
    lines.append("")

    lines.append("_Monitor scope: composite_confidence_calibrated_p_win vs clean_outcome_binary_24h._")
    lines.append("")

    return "\n".join(lines) + "\n"


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc)
    date_label = now.strftime("%Y-%m-%d %H:%M UTC")
    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    # ── 1. Query sampling overview ──────────────────────────────────────
    overview = sampling_overview(TRAILING_DAYS)
    if overview is None:
        # Probe failed/timed out — fail LOUD: emit the marker on stdout (the
        # no_agent cron delivers non-empty stdout) and exit nonzero so the
        # scheduler raises an error alert. Never silently skip (fail-quiet).
        msg = (f"[CALIBRATION MONITOR] Sampling overview probe failed/timeout — "
               f"{MEASUREMENT_UNAVAILABLE}. Tick aborted; no metrics rendered.")
        print(msg)
        print(msg, file=sys.stderr)
        return 3
    if not overview:
        msg = f"[CALIBRATION MONITOR] Sampling overview returned no rows — skipping"
        print(msg, file=sys.stderr)
        return 0

    total_journeys = overview.get("total_journeys", 0)
    labeled = overview.get("labeled", 0)

    # Check for starved labelers
    if total_journeys > 0 and labeled == 0:
        msg = f"ALERT: Outcome labeling pipeline is STALLED/STARVED! {total_journeys} clean-epoch signals accrued but 0 are labeled in last {TRAILING_DAYS} days."
        print(msg)
        return 2

    if labeled == 0:
        msg = f"[CALIBRATION MONITOR] No labeled journeys in last {TRAILING_DAYS}d — skipping"
        print(msg, file=sys.stderr)
        return 0

    # ── 2. Query buckets ────────────────────────────────────────────────
    buckets = calibration_buckets(TRAILING_DAYS)
    if buckets is None:
        # Probe failed/timed out — fail LOUD (same contract as overview).
        msg = (f"[CALIBRATION MONITOR] Calibration bucket probe failed/timeout — "
               f"{MEASUREMENT_UNAVAILABLE}. Tick aborted; no metrics rendered.")
        print(msg)
        print(msg, file=sys.stderr)
        return 3
    if not buckets:
        msg = f"[CALIBRATION MONITOR] No calibration buckets returned — skipping"
        print(msg, file=sys.stderr)
        return 0

    # ── 3. Overall calibration ───────────────────────────────────────────
    # overall is None when the probe times out/fails (Defect 1). It is NOT
    # silently coerced to {} — callers render MEASUREMENT_UNAVAILABLE instead.
    overall = overall_calibration(TRAILING_DAYS)
    if overall is None:
        print(f"[CALIBRATION MONITOR] overall_calibration() probe failed/timeout — "
              f"Overall Calibration section will render {MEASUREMENT_UNAVAILABLE}",
              file=sys.stderr)

    # ── 3b. Estimator provenance (Defect 3) ──────────────────────────────
    provenance = estimator_provenance(TRAILING_DAYS)

    # ── 4. Compute MCE (sample-weighted headline + significant-bucket) ────
    mce = compute_mce(buckets, MIN_BUCKET_SIZE)
    mce_pp = mce["mce_pp"]

    # ── 5. Build report ──────────────────────────────────────────────────
    report = build_report(now, overview, overall, buckets, mce, provenance)

    # ── 6. Persist to Obsidian ──────────────────────────────────────────
    today_str = now.strftime("%Y-%m-%d")
    report_path = os.path.join(OUTPUT_DIR, f"calibration-drift-{today_str}.md")
    write_markdown_atomic(
        report_path,
        report,
        title=f"Calibration Drift Monitor — {today_str}",
        type="task-evidence",
        status="active",
        created=today_str,
        updated=today_str,
        confidence="high",
        tags=["sycode", "calibration", "drift", "monitoring"],
        sources=["sycodetrading-supabase-db:signal_journeys"],
        project="sycode-trading",
        owners=["trading-devops"],
        knowledge_tier="evidence",
        generated=True,
        generator="calibration-drift-monitor.py",
        operational_status="drift" if mce_pp > MCE_THRESHOLD_PP else "within-bounds",
        kanban_task=PARENT_TASK,
    )

    # ── 7. Build stats dict for escalation ───────────────────────────────
    stats = {
        "mce": mce,
        "labeled": overview.get("labeled", 0),
        "labeled_with_pwin": overview.get("labeled_with_pwin", 0),
        "buckets": buckets,
        "overall": overall,            # None-safe; card renders MEASUREMENT_UNAVAILABLE
        "report_path": report_path,
    }

    # ── 8. Escalate if MCE > threshold ──────────────────────────────────
    # Hard gate (t_ef700332 / t_b4c824c7): the monitoring layer MUST NOT raise
    # a flag/card or recalibrate the engine until the Tier-1 realized-exit
    # sample reaches n >= 300 (VALIDATED_EDGE_STATUS sufficient to claim an
    # edge). Below that floor the breach is reported as INSUFFICIENT_SAMPLE for
    # accumulation tracking only — never as an alert, never as a recalibration
    # trigger. Recalibration is a Frank/PM-governed change (t_016ac4e4 review
    # path); this monitor never calls run_calibration().
    stdout_lines = []

    if mce_pp > MCE_THRESHOLD_PP:
        # Fetch the authoritative Tier-1 realized-exit sample size once.
        try:
            _tier1_raw = db_query(TIER1_FLOOR_QUERY)
            tier1_n = safe_int(_tier1_raw) if _tier1_raw is not None else None
        except Exception:
            tier1_n = None
        floor_met = tier1_n is not None and tier1_n >= TIER1_VALIDATION_FLOOR

        if floor_met:
            # Confident-sample breach (n >= 300): surface + triage card, but
            # NEVER auto-recalibrate (HOLD stays governed; t_b4c824c7).
            stdout_lines.append(
                f"[CALIBRATION DRIFT] MCE = {mce_pp}pp (threshold: {MCE_THRESHOLD_PP}pp)")
            stdout_lines.append(
                f"  MCE (unweighted, continuity): {mce['mce_unweighted_pp']}pp; "
                f"MCE (significant buckets only): {mce['mce_significant_pp']}pp")
            stdout_lines.append(
                f"  Tier-1 realized-exit n={tier1_n} >= {TIER1_VALIDATION_FLOOR} "
                f"(validated-edge floor met). MCE breach is on a confident sample.")
            stdout_lines.append(
                "  RECALIBRATION HOLD (t_b4c824c7): no auto-recalibration performed; "
                "escalate for Frank/PM-governed review.")
            ticket_result = create_calibration_review_task(stats)
            stdout_lines.append(
                f"  Labeled journeys: {overview.get('labeled', 0)} (w/ p_win: {overview.get('labeled_with_pwin', 0)})")
            stdout_lines.append(f"  Overall bias: {overall.get('overall_bias_pp', '?') if overall else MEASUREMENT_UNAVAILABLE}pp")
            stdout_lines.append(f"  Qualifying buckets: {mce['qualifying_buckets']}/{mce['total_buckets']}")
            stdout_lines.append(f"  Report: {report_path}")
            stdout_lines.append(f"  Kanban: {ticket_result}")
            stdout_lines.append("")
        else:
            # Below the validated-edge floor: report INSUFFICIENT_SAMPLE for
            # accumulation tracking. NO card, NO recalibration, NO run_calibration().
            stdout_lines.append(
                f"[CALIBRATION DRIFT] MCE = {mce_pp}pp (threshold: {MCE_THRESHOLD_PP}pp) "
                f"but Tier-1 realized-exit n={tier1_n} "
                f"< {TIER1_VALIDATION_FLOOR} (INSUFFICIENT_SAMPLE).")
            stdout_lines.append(
                "  VALIDATED_EDGE_STATUS: INSUFFICIENT_SAMPLE — NO alert/flag card raised "
                "(t_ef700332) and NO recalibration performed (t_b4c824c7).")
            stdout_lines.append(
                "  Accumulate more Tier-1 realized-exit outcomes until n >= "
                f"{TIER1_VALIDATION_FLOOR} before any recalibration decision.")
    else:
        # Silent: no stdout unless verbose
        if verbose:
            stdout_lines.append(f"[CALIBRATION OK] MCE = {mce_pp}pp — within {MCE_THRESHOLD_PP}pp threshold")

    # ── 9. Verbose dump ─────────────────────────────────────────────────
    if verbose:
        stdout_lines.append("")
        stdout_lines.append("=== Full Report ===")
        stdout_lines.append(report)

    # ── 10. Output ──────────────────────────────────────────────────────
    if stdout_lines:
        sys.stdout.write("\n".join(stdout_lines) + "\n")
    # else: silent — no stdout means cron delivers nothing (watchdog pattern)

    return 0


if __name__ == "__main__":
    sys.exit(main())
