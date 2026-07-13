#!/usr/bin/env python3
import os
import re
import sys
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from second_brain_writer import write_markdown_atomic

REPORT_WRAPPER = "/home/frank/sycode-trading/execution/run_fusion_calibration_report.sh"
HERMES_CLI = "/home/frank/.local/bin/hermes"
OBSIDIAN_PATH = "/home/frank/obsidian/quant-team/devops/latest-fusion-calibration-report.md"
KANBAN_BOARD = "sycode-trading"

PSQL_CMD = [
    "psql",
    "-h", os.environ.get("PGHOST", "localhost"),
    "-p", os.environ.get("PGPORT", "5432"),
    "-U", os.environ.get("PGUSER", "postgres"),
    "-d", os.environ.get("PGDB", "postgres"),
    "-X", "-A", "-t", "--pset", "footer=off",
]

WIN_RATE_THRESHOLD = 40.0
PNL_THRESHOLD = 0.0
MCE_THRESHOLD = 15.0
MIN_CLEAN_N = 100
MIN_SOURCE_PARSE_N = 10
MIN_NEWS_MONITOR_META_N = 100

def run_report() -> tuple[int, str]:
    """Run the report wrapper and capture its output."""
    env = os.environ.copy()
    # Ensure localhost defaults if not set
    env["PGHOST"] = env.get("PGHOST", "localhost")
    env["PGPORT"] = env.get("PGPORT", "5432")
    env["PGUSER"] = env.get("PGUSER", "postgres")
    env["PGDB"] = env.get("PGDB", "postgres")
    env["PGPASSWORD"] = env.get("PGPASSWORD", "postgres")

    try:
        res = subprocess.run(
            [REPORT_WRAPPER],
            capture_output=True,
            text=True,
            env=env,
            timeout=180
        )
        # Combine stdout and stderr to make sure we don't miss errors
        full_output = res.stdout + "\n" + res.stderr
        return res.returncode, full_output
    except subprocess.TimeoutExpired:
        return -1, "Error: Report execution timed out after 180 seconds."
    except Exception as e:
        return -2, f"Error running report: {e}"

def parse_metrics(output: str) -> dict:
    """Parse key metrics from the report output using robust regex."""
    metrics = {
        "n": None,
        "win_rate": None,
        "avg_pnl": None,
        "weighted_mce": None,
        "sql_errors": 0,
        "has_integrity_warning": False,
        "is_v2_report": "# Fusion Engine Calibration Report v2" in output,
        "is_pinned_report": "(pinned)" in output,
        "source_parseable": None,
        "source_parseable_total": None,
        "news_items_48h": None,
        "news_metadata_rows": None,
        "news_nonnull_sentiment": None,
    }

    # Extract sample size n.
    # PRIORITY (Tier-2): the pinned v2 report emits the MERGED clean unique
    # journeys count (Tier-1 realized-exit + Tier-2 trajectory augmentation),
    # which is the sample that actually clears the n>=100 confidence gate.
    # It no longer emits the pre-Tier-2 "Clean unique journeys (deduped)" label,
    # so matching that would leave n=None and silence the monitor forever.
    # We match both the Section-1 summary table form and the Section-7
    # observations text form (same underlying merged_n value).
    n_match = re.search(
        r'\|\s*\*\*MERGED clean unique journeys \(n\)\*\*\s*\|\s*\*\*([\d,]+)\*\*\s*\|', output
    )
    if n_match:
        metrics["n"] = int(n_match.group(1).replace(",", ""))
    else:
        n_match_txt = re.search(
            r'-\s*\*\*MERGED clean unique journeys \(Tier-1 \+ Tier-2 trajectory\): n=([\d,]+)\*\*', output
        )
        if n_match_txt:
            metrics["n"] = int(n_match_txt.group(1).replace(",", ""))
        else:
            # Legacy pre-Tier-2 fallback (last resort, for old pinned reports)
            n_match_legacy = re.search(
                r'\|\s*\*\*Clean unique journeys \(deduped\)\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|', output
            )
            if n_match_legacy:
                metrics["n"] = int(n_match_legacy.group(1))
            else:
                n_match_legacy_7 = re.search(r'-\s*\*\*Clean unique journeys:\s*n=(\d+)\*\*', output)
                if n_match_legacy_7:
                    metrics["n"] = int(n_match_legacy_7.group(1))

    # Extract win rate
    wr_match = re.search(r'\|\s*\*\*Clean win rate\*\*\s*\|\s*\*\*([0-9.]+)%\*\*\s*\|', output)
    if wr_match:
        metrics["win_rate"] = float(wr_match.group(1))
    else:
        wr_match_7 = re.search(r'-\s*\*\*Clean win rate:\s*([0-9.]+)%\*\*', output)
        if wr_match_7:
            metrics["win_rate"] = float(wr_match_7.group(1))

    # Extract average PnL%
    pnl_match = re.search(r'\|\s*\*\*Clean average PnL%\*\*\s*\|\s*\*\*([0-9.-]+)%\*\*\s*\|', output)
    if pnl_match:
        metrics["avg_pnl"] = float(pnl_match.group(1))
    else:
        pnl_match_7 = re.search(r'-\s*\*\*Clean average PnL%:\s*([0-9.-]+)%\*\*', output)
        if pnl_match_7:
            metrics["avg_pnl"] = float(pnl_match_7.group(1))

    # Extract sample-weighted MCE
    # Might find it in the table or the observations
    mce_match = re.search(r'Sample-weighted MCE.*?\*\*([0-9.]+)\s*pp\*\*', output, re.IGNORECASE)
    if mce_match:
        metrics["weighted_mce"] = float(mce_match.group(1))

    # Check for SQL/Database failures
    if "SQL failed" in output or "SQL timed out" in output or "DATA-INTEGRITY WARNING" in output:
        metrics["has_integrity_warning"] = True
        err_match = re.search(r'(\d+)\s+SQL\s+queries?\s+failed', output, re.IGNORECASE)
        if err_match:
            metrics["sql_errors"] = int(err_match.group(1))
        else:
            metrics["sql_errors"] = 1

    return metrics

CLEAN_EPOCH_NAME = "clean-candidate-599f58e7e"


def get_clean_epoch_start() -> str | None:
    """Return the UTC ISO start of the certified clean epoch from
    ``data_epoch_registry``. The calibration sample must be bounded to this
    epoch; rows resolved before it belong to contaminated epochs
    (pre-0629-leak, dirty-label-1p03m, funding-null, oi-sparse,
    corrupted-candle15m-backfill-20260705) and must NOT enter the calibration
    sample even when they happen to carry ``contaminated=false``.

    Mirrors ``fusion_calibration_report_v2.get_clean_epoch_start`` exactly so
    the watchdog PnL monitor and the calibration report share one source of
    truth. If the registry row is missing we return ``None`` and let the
    caller fail loudly rather than silently including pre-epoch data.
    """
    sql = (
        "SELECT starts_at::text AS s FROM data_epoch_registry "
        f"WHERE name = '{CLEAN_EPOCH_NAME}' LIMIT 1;"
    )
    env = os.environ.copy()
    env["PGPASSWORD"] = env.get("PGPASSWORD", env.get("PGPASS", "postgres"))
    try:
        res = subprocess.run(
            PSQL_CMD + ["-c", sql],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        if res.returncode != 0:
            return None
        raw = res.stdout.strip()
        return raw or None
    except Exception:
        return None


def build_clean_avg_pnl_sql(epoch_start: str) -> str:
    """Build the read-only PnL query for the Tier-1 realized-exit clean sample.

    Pure function (no DB access) so the predicate can be regression-tested
    without a live database. Mirrors ``fusion_calibration_report_v2``'s
    ``dedup_rows`` CTE exactly:

      * anchored on ``signal_journeys`` + ``decision_outcomes`` with a
        ``LEFT JOIN trade_setups`` (NOT an INNER JOIN — the fusion engine
        persists only the top-200 signals/day, so an INNER JOIN silently
        drops the trade_setup-less clean-epoch journeys, which are net-positive);
      * bounded to the certified clean epoch read from ``data_epoch_registry``;
      * one outcome per journey via ``DISTINCT ON (sj.id)`` with the same
        ordering/precedence as the report.

    ``epoch_start`` must be a non-empty ISO timestamp; callers must refuse to
    pass ``None`` (that means the registry row is missing — never compute PnL
    on untrusted pre-epoch data).
    """
    if not epoch_start or not str(epoch_start).strip():
        raise ValueError("epoch_start is required; refusing to build PnL query without a clean-epoch bound")
    resolved_at = "COALESCE(d.finalized_at, d.decided_at, d.created_at)"
    return f"""
        WITH dedup AS (
            SELECT DISTINCT ON (sj.id)
                d.pnl_percent::numeric AS pnl_pct
            FROM signal_journeys sj
            JOIN decision_outcomes d ON d.journey_id = sj.id
            LEFT JOIN trade_setups ts ON ts.signal_id = sj.id::text
            WHERE d.outcome_class IN ('WIN', 'LOSS')
              AND d.contaminated = false
              AND d.is_counterfactual = false
              AND d.label_source NOT IN ('interim_1h', 'interim_4h')
              AND abs(d.pnl_percent::numeric) <= 1000
              AND {resolved_at} >= now() - interval '30 days'
              AND {resolved_at} >= '{epoch_start}'
            ORDER BY sj.id,
                     d.is_final DESC,
                     {resolved_at} DESC,
                     (ts.signal_id IS NOT NULL) DESC,
                     ts.generated_at DESC
        )
        SELECT ROUND(AVG(pnl_pct), 4)::text FROM dedup;
    """


def compute_clean_avg_pnl() -> tuple[float | None, str | None]:
    """Compute clean deduped average PnL directly from Postgres.

    This mirrors the v2 report's Tier-1 REALIZED-EXIT clean sample predicate so
    average PnL% monitoring does not depend on the report exposing the metric
    in its stdout.

    Fix-C alignment (task t_021a50c5, validation t_c8cde089): the previous
    watchdog predicate joined ``trade_setups`` with an INNER JOIN and applied NO
    clean-epoch gate. That silently dropped ~70 clean-epoch journeys that have
    an outcome but no ``trade_setup`` row (the fusion engine persists only the
    top-200 signals/day). Those dropped rows are net-positive, so the INNER
    JOIN produced a loss-heavy subset (n=41, avg PnL=-0.2151%) and fired a
    spurious ``Avg PnL% < 0%`` breach that the report's own authoritative
    sample (LEFT JOIN + clean-epoch gate, n=96, avg PnL=+0.6866%) did not show.

    We now anchor on ``signal_journeys`` + ``decision_outcomes`` with a LEFT
    JOIN to ``trade_setups`` (same as the report's dedup_rows CTE) and apply the
    certified clean-epoch gate read from ``data_epoch_registry``. The MCE breach
    (genuine Tier-1 miscalibration) is intentionally unaffected — only the
    measurement-consistent PnL sample is corrected.
    """
    epoch_start = get_clean_epoch_start()
    if epoch_start is None:
        return None, "clean-epoch registry row missing; refusing to compute PnL on untrusted pre-epoch data"

    sql = build_clean_avg_pnl_sql(epoch_start)
    env = os.environ.copy()
    env["PGPASSWORD"] = env.get("PGPASSWORD", env.get("PGPASS", "postgres"))
    try:
        res = subprocess.run(
            PSQL_CMD + ["-c", sql],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        if res.returncode != 0:
            return None, res.stderr.strip() or f"psql exited {res.returncode}"
        raw = res.stdout.strip()
        if not raw:
            return None, "average PnL query returned empty output"
        return float(raw), None
    except Exception as e:
        return None, str(e)

def save_to_obsidian(content: str):
    """Write the living calibration report through the canonical atomic writer."""
    report_date = datetime.now(timezone.utc).date().isoformat()
    try:
        write_markdown_atomic(
            OBSIDIAN_PATH,
            content,
            title="Latest Fusion Calibration Report",
            type="task-evidence",
            status="active",
            created="2026-07-06",
            updated=report_date,
            confidence="high",
            tags=["sycode-trading", "fusion-engine", "calibration", "live-dashboard"],
            sources=[REPORT_WRAPPER, "sycodetrading-supabase-db:signal_journeys"],
            project="sycode-trading",
            owners=["sycode-trading-pm"],
            knowledge_tier="evidence",
            generated=True,
            generator="calibration_watchdog.py",
        )
    except Exception as e:
        print(f"Warning: Could not save report to Obsidian: {e}", file=sys.stderr)

def create_kanban_task(title: str, body: str, id_key: str):
    """Create an automated kanban task via Hermes CLI."""
    try:
        subprocess.run([
            HERMES_CLI, "kanban", "create",
            "--triage",
            "--assignee", "sycode-trading-pm",
            "--priority", "80",
            "--idempotency-key", id_key,
            "--body", body,
            title
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"Warning: Failed to create kanban triage task: {e}", file=sys.stderr)

def main() -> int:
    exit_code, report_output = run_report()

    # Save the latest report to Obsidian on every tick (even on script failure, to record raw diagnostics)
    save_to_obsidian(report_output)

    if exit_code != 0:
        print(f"🚨 CRITICAL: Fusion Calibration Report wrapper failed with exit code {exit_code}.")
        print("Last report output for diagnosis:")
        print(report_output)
        return exit_code

    metrics = parse_metrics(report_output)
    n = metrics["n"]
    win_rate = metrics["win_rate"]
    avg_pnl = metrics["avg_pnl"]
    weighted_mce = metrics["weighted_mce"]

    # The pinned v2 report may not yet expose overall clean average PnL%; compute
    # it directly with the same read-only dedup predicate so the PnL monitor is live.
    avg_pnl_error = None
    if avg_pnl is None:
        avg_pnl, avg_pnl_error = compute_clean_avg_pnl()
        metrics["avg_pnl"] = avg_pnl
        if avg_pnl is None and n is not None and n >= MIN_CLEAN_N:
            metrics["has_integrity_warning"] = True
            metrics["sql_errors"] = max(metrics["sql_errors"], 1)

    # 1. Check database/SQL errors (Critical Alert — regardless of n)
    if metrics["has_integrity_warning"] or metrics["sql_errors"] > 0:
        id_key = f"calibration-integrity-error-{datetime.now(timezone.utc).strftime('%Y%m%dT%H')}"
        title = f"CRITICAL: Calibration Pipeline SQL Failures Detected"
        body = (
            f"The calibration report watchdog detected {metrics['sql_errors']} SQL or database query failures during execution.\n\n"
            f"This is a critical pipeline infrastructure failure that blinds calibration monitoring.\n\n"
            f"Please refer to the response runbook: [[operations/runbooks/fusion-calibration-alert-runbook.md]]\n"
            f"See raw report output in Obsidian: [[devops/latest-fusion-calibration-report.md]]"
        )
        # Print alert to stdout (verbatim cron notification)
        print("🚨 CRITICAL: FUSION CALIBRATION DATABASE FAILURE 🚨")
        print(f"The calibration report watchdog detected query failures: {metrics['sql_errors']} SQL errors.")
        print(f"Living Dashboard: [[devops/latest-fusion-calibration-report.md]]")
        print(f"Response Runbook: [[operations/runbooks/fusion-calibration-alert-runbook.md]]")
        create_kanban_task(title, body, id_key)
        return 0

    # 2. Check sample confidence size.
    # If clean unique journeys (n) is less than 100, we suppress performance/calibration alerts
    # as they are statistically unstable and would cause alert-channel crowding.
    if n is None or n < MIN_CLEAN_N:
        # Stay completely silent on low-confidence clean samples. The no-agent
        # cron only delivers non-empty stdout, so this prevents false-positive
        # alert-channel crowding while post-fix outcomes accrue.
        return 0

    # 3. Check performance and calibration breaches (only on confident samples)
    breaches = []
    if win_rate is not None and win_rate < WIN_RATE_THRESHOLD:
        breaches.append(f"Win Rate: {win_rate:.1f}% (Threshold: < {WIN_RATE_THRESHOLD:.1f}%)")
    if avg_pnl is not None and avg_pnl < PNL_THRESHOLD:
        breaches.append(f"Average PnL%: {avg_pnl:.2f}% (Threshold: < {PNL_THRESHOLD:.2f}%)")
    if weighted_mce is not None and weighted_mce > MCE_THRESHOLD:
        breaches.append(f"Sample-weighted MCE: {weighted_mce:.1f}pp (Threshold: > {MCE_THRESHOLD:.1f}pp)")

    if breaches:
        id_key = f"calibration-threshold-breach-{datetime.now(timezone.utc).strftime('%Y%m%dT%H')}"
        title = f"Sample-weighted Calibration Threshold Breach: MCE={weighted_mce or '--'}pp"
        
        breach_summary_text = "\n".join([f"- {b}" for b in breaches])
        body = (
            f"The Fusion Engine Calibration watchdog detected one or more performance/calibration breaches on a confident sample (n={n}):\n\n"
            f"{breach_summary_text}\n\n"
            f"[All Monitored Metrics]\n"
            f"- Clean Unique Journeys (n): {n}\n"
            f"- Win Rate: {win_rate or '--'}%\n"
            f"- Average PnL%: {avg_pnl or '--'}%\n"
            f"- Sample-weighted MCE: {weighted_mce or '--'}pp\n\n"
            f"Please refer to the response runbook: [[operations/runbooks/fusion-calibration-alert-runbook.md]]\n"
            f"See detailed report in Obsidian: [[devops/latest-fusion-calibration-report.md]]"
        )

        # Print alert to stdout (verbatim cron notification)
        print("🚨 FUSION CALIBRATION THRESHOLD BREACH ALERT 🚨")
        print("At least one key performance or calibration metric has breached safety thresholds on a confident sample.\n")
        print("[Breached Metrics]")
        for b in breaches:
            print(f"- {b}")
        print("\n[All Monitored Metrics]")
        print(f"- Clean Unique Journeys (n): {n}")
        print(f"- Win Rate: {win_rate:.1f}%" if win_rate is not None else "- Win Rate: --")
        print(f"- Average PnL%: {avg_pnl:.2f}%" if avg_pnl is not None else "- Average PnL%: --")
        print(f"- Sample-weighted MCE: {weighted_mce:.1f}pp" if weighted_mce is not None else "- Sample-weighted MCE: --")
        print(f"\nLiving Dashboard: [[devops/latest-fusion-calibration-report.md]]")
        print(f"Response Runbook: [[operations/runbooks/fusion-calibration-alert-runbook.md]]")

        create_kanban_task(title, body, id_key)

    return 0

def self_test() -> None:
    global OBSIDIAN_PATH
    original = OBSIDIAN_PATH
    try:
        with tempfile.TemporaryDirectory(prefix="calibration-watchdog-writer-") as directory:
            OBSIDIAN_PATH = str(Path(directory) / "report.md")
            save_to_obsidian("# Fixture report\n\nRead-only calibration evidence.\n")
            text = Path(OBSIDIAN_PATH).read_text(encoding="utf-8")
            assert 'type: "task-evidence"' in text
            assert 'status: "active"' in text
            assert 'confidence: "high"' in text
            assert 'generator: "calibration_watchdog.py"' in text
            assert not list(Path(directory).glob(".*.incoming-*"))
    finally:
        OBSIDIAN_PATH = original

if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
        print('{"status":"pass","writer":"calibration_watchdog.py"}')
    else:
        sys.exit(main())
