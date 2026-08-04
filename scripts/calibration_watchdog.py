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
# Tier-1 investigate floor (t_e9d0be69 / t_31b59fa7 F3 hardening).
# MIN_CLEAN_N(100) only gates statistical stability of the watchdog's own
# alert. The calibration report's own validation floor for a *validated edge*
# is n >= 300 (VALIDATED_EDGE_STATUS: INSUFFICIENT_SAMPLE below 300). A Tier-1
# breach with 100 <= n < 300 is therefore a statistically real miscalibration
# signal but MUST NOT be surfaced as a confident, validated calibration failure
# — it is surfaced as INVESTIGATE instead. This matches the report's own floor
# and prevents the 112-row Tier-1 breach from being mis-read as conclusive.
TIER1_INVESTIGATE_FLOOR = 300
MIN_SOURCE_PARSE_N = 10
MIN_NEWS_MONITOR_META_N = 100

# --- t_ba7757c8: evidence-based alert cooldown + payload context ------------
# Threshold decision (reviewed 2026-08-03, parent triage t_b7204a09):
#   MCE_THRESHOLD stays 15.0pp. The 15.8pp alert headline was the MERGED-sample
#   figure; the authoritative Tier-1 scored-only MCE was 32.8-41.9pp (~2x
#   UNDERSTATED), so the breach is genuine and raising the threshold would mask
#   it. The noise problem is NOT the threshold — it is the 6h cron re-carding
#   the same persistent breach (standing duplicate chain t_9f92b853,
#   t_6c992632, t_a7ea3cf9, t_fd15d9fa, t_893054a5). We therefore keep the
#   thresholds unchanged and add a per-family CARD-CREATION COOLDOWN: stdout
#   alerts still emit every tick; only the kanban triage card is created at
#   most once per family per BREACH_CARD_COOLDOWN_HOURS. Fail-open: any state
#   read error allows card creation (never suppress on error).
BREACH_CARD_COOLDOWN_HOURS = 24
STATE_DIR = Path(__file__).resolve().parent / "state"
STATE_FILE = STATE_DIR / "calibration_watchdog_state.json"

# --- t_ece49d50: canonical calibration_verdict.json source -------------------
# Weighted MCE and Tier-1 clean n are read from a machine-readable verdict
# v1 produced by the fusion_calibration_report_v2 pipeline. The watchdog uses
# these two fields as the PRIMARY source; when the JSON is absent or corrupt
# it falls back to Section-7 regex parsing (tier1_clean_n only — weighted_mce
# stays None and the DB-computed Tier-1 scored-only MCE in main() carries
# the alert decision). The path is environment-overridable so staging can
# point at an ephemeral /tmp copy without changing production behaviour.
VERDICT_PATH_ENV = os.environ.get(
    "CALIBRATION_VERDICT_PATH",
    "/home/frank/.hermes/var/sycode/calibration_verdict.json"
)
VERDICT_PATH = Path(VERDICT_PATH_ENV)


def load_verdict() -> dict:
    """Read the canonical calibration_verdict.json (best-effort).

    Returns a dict with keys ``weighted_mce`` and ``tier1_clean_n`` when the
    file exists and is valid JSON; otherwise returns ``{}`` (fail-open).
    Never raises — a missing or corrupt verdict simply disables the JSON
    shortcut and lets the caller fall back to regex.
    """
    try:
        if VERDICT_PATH.exists():
            import json
            return json.loads(VERDICT_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

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
    """Parse key metrics from the report output using robust regex.

    JSON-first path (t_ece49d50): ``weighted_mce`` and ``tier1_clean_n`` are
    read from the canonical ``calibration_verdict.json`` produced by the
    fusion-calibration-report pipeline.  When the file is absent or corrupt
    the watchdog falls back to the existing Section-7 regex parsers so the
    monitor never goes silent during pipeline transition or temporary failure.
    """
    metrics = {
        "n": None,
        "merged_n": None,
        "tier1_clean_n": None,
        "win_rate": None,
        "avg_pnl": None,
        "weighted_mce": None,
        "merged_mce": None,
        "tier1_scored_mce": None,
        "tier1_scored_n": None,
        "tier1_scored_error": None,
        "sample_window": None,
        "resolution_span": None,
        "epoch_start_reported": None,
        "sql_errors": 0,
        "has_integrity_warning": False,
        "is_v2_report": "# Fusion Engine Calibration Report v2" in output,
        "is_pinned_report": "(pinned)" in output,
        "source_parseable": None,
        "source_parseable_total": None,
        "news_items_48h": None,
        "news_metadata_rows": None,
        "news_nonnull_sentiment": None,
        "verdict_source": None,  # "json" | "regex" | None
    }

    # --- (t_ece49d50) JSON-first: read canonical calibration_verdict.json ----
    verdict = load_verdict()
    if verdict:
        json_tier1 = verdict.get("tier1_clean_n")
        if isinstance(json_tier1, (int, float)):
            metrics["tier1_clean_n"] = int(json_tier1)
        json_mce = verdict.get("weighted_mce")
        if isinstance(json_mce, (int, float)):
            metrics["weighted_mce"] = float(json_mce)
            metrics["merged_mce"] = float(json_mce)
        if metrics["tier1_clean_n"] is not None or metrics["weighted_mce"] is not None:
            metrics["verdict_source"] = "json"

    # --- Sample size: TWO distinct populations (t_fb422737 / t_016ac4e4) -----
    # merged_n      = Tier-1 realized-exit + Tier-2 synthetic candle-replay.
    #                 Headline `**MERGED clean unique journeys (n)**` row; large
    #                 (thousands) but NOT the calibration sample.
    # tier1_clean_n = Tier-1 realized-exit ONLY — the exact sample the
    #                 sample-weighted MCE is computed on (report Sections 2-3
    #                 weight calibration buckets by tier1 clean_n, never
    #                 merged_n). The confidence gate and the alert label MUST
    #                 use this value, or the alert silently mis-reports the
    #                 sample the miscalibration was measured on.
    # Regex is kept as the FALLBACK when JSON is absent (Section-1 table or
    # Section-7 text form).
    merged_n_match = re.search(
        r'\|\s*\*\*MERGED clean unique journeys \(n\)\*\*\s*\|\s*\*\*([\d,]+)\*\*\s*\|', output
    )
    if merged_n_match:
        metrics["merged_n"] = int(merged_n_match.group(1).replace(",", ""))
    else:
        _m_txt = re.search(
            r'-\s*\*\*MERGED clean unique journeys \(Tier-1 \+ Tier-2 trajectory\): n=([\d,]+)\*\*', output
        )
        if _m_txt:
            metrics["merged_n"] = int(_m_txt.group(1).replace(",", ""))

    # Tier-1 realized-exit clean unique journeys (authoritative calibration n).
    # Only regex-fallback when JSON did not supply tier1_clean_n above.
    if metrics["tier1_clean_n"] is None:
        tier1_match = re.search(
            r'\|\s*\*\*Tier-1 clean unique journeys \(realized-exit, authoritative\)\*\*\s*\|\s*\*\*([\d,]+)\*\*\s*\|', output
        )
        if tier1_match:
            metrics["tier1_clean_n"] = int(tier1_match.group(1).replace(",", ""))
        else:
            _t_txt = re.search(
                r'-\s*\*\*Tier-1 clean unique journeys \(realized-exit\): n=([\d,]+)\*\*', output
            )
            if _t_txt:
                metrics["tier1_clean_n"] = int(_t_txt.group(1).replace(",", ""))

    # n = the calibration-sample identity (Tier-1). Fall back to merged_n ONLY
    # when the Tier-1 line is absent (legacy/pre-Tier-2 reports) so the monitor
    # never goes silently blind — but the MCE is always measured on
    # tier1_clean_n, so labelling the alert with merged_n is wrong and is the
    # root-cause defect fixed by t_016ac4e4.
    metrics["n"] = (
        metrics["tier1_clean_n"]
        if metrics["tier1_clean_n"] is not None
        else metrics["merged_n"]
    )

    # Extract win rate. t_ba7757c8: the pinned v2 report emits
    # `**Tier-1 clean win rate**` (Section 1 table) and
    # `- **Tier-1 clean win rate: 19.0%**` (Section 7 observation) — the old
    # `Clean win rate` regex silently never matched, so the 40% WR breach never
    # fired (parent triage t_b7204a09 P1 "watchdog WR regex stale"). Match the
    # Tier-1 label first, keep the legacy form as a fallback for older reports.
    wr_match = re.search(r'\|\s*\*\*Tier-1 clean win rate\*\*\s*\|\s*\*\*([0-9.]+)%\*\*\s*\|', output)
    if wr_match:
        metrics["win_rate"] = float(wr_match.group(1))
    else:
        wr_match = re.search(r'\|\s*\*\*Clean win rate\*\*\s*\|\s*\*\*([0-9.]+)%\*\*\s*\|', output)
        if wr_match:
            metrics["win_rate"] = float(wr_match.group(1))
        else:
            wr_match_7 = re.search(r'-\s*\*\*Tier-1 clean win rate:\s*([0-9.]+)%\*\*', output)
            if wr_match_7:
                metrics["win_rate"] = float(wr_match_7.group(1))
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

    # Extract sample-weighted MCE.
    # NOTE (t_0bfed6a8 headline-display fix): the pinned v2 report emits exactly
    # ONE "Sample-weighted MCE" line (Section 2 table AND Section 7 text) and it
    # is computed on the MERGED sample — fusion_calibration_report_v2.py Section
    # 2 calls compute_weighted_mce(buckets, merged_n) over Tier-1 + Tier-2
    # synthetic rows. It is a DILUTED display figure (~90% synthetic rows),
    # NOT the authoritative Tier-1 miscalibration. We parse it as merged_mce and
    # NEVER label it Tier-1. The authoritative Tier-1 scored-only MCE is
    # computed read-only by compute_tier1_scored_mce() (see below).
    mce_match = re.search(r'Sample-weighted MCE.*?\*\*([0-9.]+)\s*pp\*\*', output, re.IGNORECASE)
    if mce_match:
        metrics["weighted_mce"] = float(mce_match.group(1))
        metrics["merged_mce"] = float(mce_match.group(1))

    # --- Sample window / resolution span (t_ba7757c8 payload enrichment) -----
    # The report header emits `**Window:** last 30d (30d) of resolved outcomes`
    # and Section 1 emits `| Clean sample resolution span | 2026-07-06 02:57 ->
    # 2026-08-03 08:03 UTC |`. Both are evidence context so the operator knows
    # the exact observation window the breach was measured on without opening
    # the dashboard.
    win_match = re.search(r'\*\*Window:\*\*\s*([^*\n]+?)\s*of resolved outcomes', output)
    if win_match:
        metrics["sample_window"] = win_match.group(1).strip()
    else:
        legacy_win = re.search(r'Window:\s*([^\n|]+)', output)
        if legacy_win:
            metrics["sample_window"] = legacy_win.group(1).strip()
    span_match = re.search(r'Clean sample resolution span\s*\|\s*([^|]+)\|', output)
    if span_match:
        metrics["resolution_span"] = span_match.group(1).strip()
    epoch_match = re.search(r'Clean sample epoch gate\s*\|\s*([^|]+)\|', output)
    if epoch_match:
        metrics["epoch_start_reported"] = epoch_match.group(1).strip()

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

# ---------------------------------------------------------------------------
# Authoritative Tier-1 scored-only MCE (t_0bfed6a8 headline-display fix)
# ---------------------------------------------------------------------------
# The pinned report (run_fusion_calibration_report.sh PIN) computes its single
# "Sample-weighted MCE" headline on the MERGED sample (Tier-1 realized-exit +
# ~90% Tier-2 synthetic candle-replay rows) — fusion_calibration_report_v2.py
# Section 2 calls compute_weighted_mce(buckets, merged_n). Pairing that figure
# with the Tier-1 n (the t_016ac4e4 sample-identity gate) is the display
# defect classified in the 2026-08-03 incident (t_f0b7209a §6): the merged MCE
# is diluted ~2-2.5x below the Tier-1 truth. The authoritative calibration
# denominator per runbook §1b is the Tier-1 sample, NET-of-cost scored-only
# (ledger-reconciled, fail-closed — NS-P1 CHILD-B t_07523bcb). Because the
# pinned report does not emit a Tier-1-only MCE, the watchdog computes it
# directly with the SAME read-only predicate as the report's dedup_rows CTE
# (anchored on signal_journeys + decision_outcomes, LEFT JOIN trade_setups,
# LEFT JOIN v_ledger_reward, DISTINCT ON (sj.id), clean-epoch gate) — the
# method independently verified at 32.72pp / n=381 in t_7865a6af.
def _is_true(v) -> bool:
    return str(v).lower() in ("true", "t", "1", "yes")


def _bucket_key(score: float) -> str:
    """0.05-width conviction bucket key (mirrors fusion_calibration_report_v2)."""
    start = int(score * 20) / 20.0
    end = start + 0.05
    if end >= 1.0:
        return f"[{start:.2f}, 1.00]"
    return f"[{start:.2f}, {end:.2f})"


TIER1_SCORED_SQL = """
    SELECT DISTINCT ON (sj.id)
        sj.id,
        COALESCE(ts.conviction_score::numeric,
                 sj.composite_confidence_score::numeric)::text AS conviction_score,
        (lr.position_id IS NOT NULL)::text AS ledger_matched,
        lr.is_win::text AS ledger_is_win,
        COALESCE(sj.direction, '')::text AS direction,
        COALESCE(sj.timeframe, '')::text AS timeframe,
        COALESCE(d.label_source, '')::text AS lane
    FROM signal_journeys sj
    JOIN decision_outcomes d ON d.journey_id = sj.id
    LEFT JOIN trade_setups ts ON ts.signal_id = sj.id::text
    LEFT JOIN v_ledger_reward lr
        ON lr.correlation_id = sj.correlation_id
        OR lr.signal_id = sj.signal_id
    WHERE d.outcome_class IN ('WIN', 'LOSS')
      AND d.contaminated = false
      AND d.is_counterfactual = false
      AND d.label_source NOT IN ('interim_1h', 'interim_4h')
      AND abs(d.pnl_percent::numeric) <= 1000
      AND COALESCE(d.finalized_at, d.decided_at, d.created_at) >= now() - interval '30 days'
      AND sj.triggered_at >= '{epoch_start}'
    ORDER BY sj.id,
             d.is_final DESC,
             COALESCE(d.finalized_at, d.decided_at, d.created_at) DESC,
             (ts.signal_id IS NOT NULL) DESC,
             ts.generated_at DESC;
"""


def build_tier1_scored_sql(epoch_start: str) -> str:
    """Return the read-only Tier-1 scored-only MCE query for a given epoch.

    Pure function (no DB access) so the predicate can be regression-tested.
    Mirrors the report's dedup_rows CTE exactly (see module docstring above).
    """
    if not epoch_start or not str(epoch_start).strip():
        raise ValueError(
            "epoch_start is required; refusing to compute Tier-1 MCE without a clean-epoch bound")
    return TIER1_SCORED_SQL.format(epoch_start=epoch_start)


def compute_bucket_weighted_mce(rows) -> tuple[float | None, int]:
    """Sample-weighted MCE over ledger-scored Tier-1 rows (pure, testable).

    Faithful to fusion_calibration_report_v2.py Section 2 + _net_win():
      * only rows that reconciled to v_ledger_reward (ledger_matched=true) are
        scored (fail-closed net-of-cost label, NS-P1 CHILD-B t_07523bcb);
      * buckets are 0.05-width conviction intervals with
        expected_wr = bucket_mean_score * 100 and weight = bucket_n / scored_n;
      * weighted_mce = sum(|actual_wr - expected_wr| * weight).

    ``rows`` are dicts with keys conviction_score / ledger_matched /
    ledger_is_win. Returns (weighted_mce_pp, scored_n); (None, 0) when there
    are no scored rows.
    """
    scored = []
    for r in rows:
        if not _is_true(r.get("ledger_matched", "")):
            continue  # fail-closed: unreconciled Tier-1 rows are NOT scored
        try:
            score = float(r.get("conviction_score") or 0)
        except (ValueError, TypeError):
            score = 0.0
        scored.append((score, _is_true(r.get("ledger_is_win", ""))))
    scored_n = len(scored)
    if scored_n == 0:
        return None, 0
    buckets: dict[str, dict] = {}
    for score, win in scored:
        key = _bucket_key(score)
        b = buckets.setdefault(key, {"total": 0, "wins": 0, "score_sum": 0.0})
        b["total"] += 1
        if win:
            b["wins"] += 1
        b["score_sum"] += score
    weighted = 0.0
    for b in buckets.values():
        wr = (b["wins"] / b["total"] * 100) if b["total"] else 0.0
        avg_score = (b["score_sum"] / b["total"]) if b["total"] else 0.0
        expected_wr = avg_score * 100
        weighted += abs(wr - expected_wr) * (b["total"] / scored_n)
    return round(weighted, 2), scored_n


def compute_tier1_scored_mce() -> tuple[float | None, int | None, str | None]:
    """Compute the authoritative Tier-1 scored-only (net-of-cost) MCE.

    Returns (weighted_mce_pp, scored_n, error). Read-only psql using the same
    clean-epoch gate as compute_clean_avg_pnl. Never fabricates a value: on any
    failure it returns (None, None, message) and the caller must fall back to
    the merged figure LABELED non-authoritative rather than claiming a Tier-1
    number it did not measure.
    """
    epoch_start = get_clean_epoch_start()
    if epoch_start is None:
        return None, None, (
            "clean-epoch registry row missing; refusing to compute Tier-1 MCE "
            "on untrusted pre-epoch data")
    try:
        sql = build_tier1_scored_sql(epoch_start)
    except ValueError as e:
        return None, None, str(e)
    env = os.environ.copy()
    env["PGPASSWORD"] = env.get("PGPASSWORD", env.get("PGPASS", "postgres"))
    try:
        res = subprocess.run(
            PSQL_CMD + ["-c", sql],
            capture_output=True, text=True, env=env, timeout=90,
        )
        if res.returncode != 0:
            return None, None, res.stderr.strip() or f"psql exited {res.returncode}"
        rows = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 4:
                continue
            row = {
                "conviction_score": parts[1].strip(),
                "ledger_matched": parts[2].strip(),
                "ledger_is_win": parts[3].strip(),
            }
            if len(parts) >= 7:
                row["direction"] = parts[4].strip()
                row["timeframe"] = parts[5].strip()
                row["lane"] = parts[6].strip()
            rows.append(row)
        mce, scored_n = compute_bucket_weighted_mce(rows)
        return mce, scored_n, None
    except Exception as e:
        return None, None, str(e)


def compute_top_contributing_slice(rows) -> str | None:
    """Return a concise ``strategy slice`` evidence line for the alert payload.

    Mirrors the t_b7204a09 triage segmentation: among the ledger-scored Tier-1
    rows, pick the direction / timeframe / lane slice with the largest
    sample-weighted MCE contribution (|actual_wr - expected_wr| * n/scored_n,
    expected_wr = bucket midpoint of the row's conviction score). Read-only,
    pure function over the same rows as compute_tier1_scored_mce.

    Returns a single line like::

        "Top strategy slice: dir=LONG (n=184, WR 13.6%, contrib 18.70pp)"

    or ``None`` when no scored rows exist. Never raises.
    """
    scored = []
    for r in rows:
        if not _is_true(r.get("ledger_matched", "")):
            continue  # fail-closed: only reconciled rows are scored
        try:
            score = float(r.get("conviction_score") or 0)
        except (ValueError, TypeError):
            score = 0.0
        scored.append((score, _is_true(r.get("ledger_is_win", "")), r))
    if not scored:
        return None

    def slice_contrib(get_key):
        groups = {}
        for score, win, r in scored:
            k = get_key(r)
            g = groups.setdefault(k, {"n": 0, "w": 0, "score_sum": 0.0})
            g["n"] += 1
            if win:
                g["w"] += 1
            g["score_sum"] += score
        best = None
        denom = len(scored)
        for k, g in groups.items():
            act = (g["w"] / g["n"] * 100) if g["n"] else 0.0
            exp = (g["score_sum"] / g["n"] * 100) if g["n"] else 0.0
            contrib = abs(act - exp) * (g["n"] / denom)
            if best is None or contrib > best[0]:
                best = (contrib, k, g["n"], act)
        return best

    candidates = []
    for label, fn in (
        ("dir", lambda r: str(r.get("direction", "")).upper() or "N/A"),
        ("tf", lambda r: str(r.get("timeframe", "")) or "N/A"),
        ("lane", lambda r: str(r.get("lane", "")) or "N/A"),
    ):
        c = slice_contrib(fn)
        if c is not None:
            candidates.append((label, c))
    if not candidates:
        return None
    label, (contrib, key, n, act_wr) = max(
        candidates, key=lambda t: t[1][0])
    return (
        f"Top strategy slice: {label}={key} "
        f"(n={n}, WR {act_wr:.1f}%, contrib {contrib:.2f}pp)"
    )


def compute_tier1_slice_context() -> str | None:
    """Read-only top-slice evidence line from the live DB.

    Best-effort: returns the ``compute_top_contributing_slice`` line or None on
    any failure (never raises, never blocks the alert). Mirrors
    compute_tier1_scored_mce's fetch (same predicate + clean-epoch gate).
    """
    try:
        epoch_start = get_clean_epoch_start()
        if epoch_start is None:
            return None
        sql = build_tier1_scored_sql(epoch_start)
        env = os.environ.copy()
        env["PGPASSWORD"] = env.get("PGPASSWORD", env.get("PGPASS", "postgres"))
        res = subprocess.run(
            PSQL_CMD + ["-c", sql],
            capture_output=True, text=True, env=env, timeout=90,
        )
        if res.returncode != 0:
            return None
        rows = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 4:
                continue
            row = {
                "conviction_score": parts[1].strip(),
                "ledger_matched": parts[2].strip(),
                "ledger_is_win": parts[3].strip(),
            }
            if len(parts) >= 7:
                row["direction"] = parts[4].strip()
                row["timeframe"] = parts[5].strip()
                row["lane"] = parts[6].strip()
            rows.append(row)
        return compute_top_contributing_slice(rows)
    except Exception:
        return None


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

def decide_alert(metrics: dict) -> dict | None:
    """Pure, side-effect-free alert classifier.

    Takes the parsed metric set (same keys produced by ``parse_metrics`` plus
    optional ``avg_pnl`` computed by ``compute_clean_avg_pnl``) and returns the
    alert decision the watchdog should act on, OR ``None`` (no alert / silent).

    Returned dict shape::

        {
            "kind": "INVESTIGATE" | "BREACH" | "THIN_SAMPLE",
            "card": bool,        # whether a kanban triage card should be opened
            "card_title": str,   # title for the triage card ("" when card=False)
            "card_body": str,    # body for the triage card ("" when card=False)
            "stdout_lines": [str, ...],  # verbatim cron notification lines
        }

    Design rules (hardened by t_7223ef5b against the t_d8ccf4bd false-positive):

    * INVESTIGATE is emitted ONLY when a *genuine* breach exists on a thin
      (100 <= n < 300) Tier-1 sample. A genuine MCE breach means
      ``weighted_mce > MCE_THRESHOLD``. Previously INVESTIGATE fired for every
      thin sample and unconditionally claimed an MCE breach even when MCE was
      healthy (e.g. n=150, MCE=10.0pp) — that is the defect this closes.
    * A thin-sample INVESTIGATE (100 <= n < 300) MUST NOT open a kanban card
      or raise any flag (t_ef700332). The monitoring layer only raises a
      flag/alert once the Tier-1 sample reaches the validated-edge floor
      (n >= 300), owned by ``tier1_sample_gate.py`` (READY_FOR_VALIDATION /
      BREACH) and this watchdog's own n >= 300 BREACH path. The thin-sample
      INVESTIGATE instead reports the current ``n`` and marks
      INSUFFICIENT_SAMPLE for accumulation tracking only.
    * A thin sample with *healthy* metrics returns ``THIN_SAMPLE``: it logs the
      four monitored metrics (no breach claim, no card) so sample accumulation
      is still tracked rather than blind.
    * n < MIN_CLEAN_N: also ``THIN_SAMPLE`` but logs explicitly that the sample
      is too thin to gauge (still emits the four metrics, no card). This closes
      the silent n<100 gap from t_d8ccf4bd finding #4.
    * n >= 300 (confident sample): emit BREACH only when at least one of the
      three thresholds is actually breached. Never claim a breach that did not
      occur.

    The watchdog never mutates the engine, trade_intents, or any DB here — it
    only prints + optionally opens a triage card. That invariant is preserved.
    """
    n = metrics.get("n")
    win_rate = metrics.get("win_rate")
    avg_pnl = metrics.get("avg_pnl")
    weighted_mce = metrics.get("weighted_mce")
    # t_0bfed6a8 headline-display fix: two MCE figures with EXPLICIT sample
    # identity. The pinned report's parsed MCE is the MERGED (diluted,
    # non-authoritative) figure; the authoritative Tier-1 scored-only MCE is
    # computed read-only by compute_tier1_scored_mce() in main().
    tier1_scored_mce = metrics.get("tier1_scored_mce")
    tier1_scored_n = metrics.get("tier1_scored_n")
    merged_mce = metrics.get("merged_mce", weighted_mce)
    merged_n = metrics.get("merged_n")

    def fmt(v, suffix=""):
        return f"{v:.1f}{suffix}" if isinstance(v, (int, float)) else "--"

    def mce_decision_value():
        """(value, label) for the MCE the alert decision keys on.

        Prefer the authoritative Tier-1 scored-only MCE (runbook §1b contract:
        MCE computed on the Tier-1 sample, never the MERGED sample). When the
        read-only compute was unavailable, fall back to the parsed report MCE
        but label it MERGED / non-authoritative so it can never be misread as
        Tier-1.
        """
        if tier1_scored_mce is not None:
            return tier1_scored_mce, (
                f"Tier-1 scored-only (authoritative, net-of-cost), "
                f"n={tier1_scored_n if tier1_scored_n is not None else '--'}")
        return merged_mce, (
            f"MERGED sample (non-authoritative), "
            f"n={merged_n if merged_n is not None else '--'}")

    def monitored_block():
        lines = [
            f"- Tier-1 clean unique journeys (n): {n if n is not None else '--'}",
            f"- Tier-1 scored-only MCE (authoritative, net-of-cost): "
            f"{fmt(tier1_scored_mce, 'pp')} "
            f"(n={tier1_scored_n if tier1_scored_n is not None else '--'})",
            f"- Sample-weighted MCE (MERGED sample — non-authoritative): "
            f"{fmt(merged_mce, 'pp')} "
            f"(n={merged_n if merged_n is not None else '--'})",
            f"- Win Rate: {fmt(win_rate, '%')}",
            f"- Average PnL%: {fmt(avg_pnl, '%')}",
        ]
        # t_ba7757c8 evidence context (all optional; absent -> omitted):
        # sample window, resolution span, top strategy slice, baseline/rolling
        # MCE. Read-only values injected by main(); decide_alert stays pure.
        sw = metrics.get("sample_window")
        span = metrics.get("resolution_span")
        if sw or span:
            lines.append(
                f"- Sample window: {sw or 'n/a'} (resolution: {span or 'n/a'})")
        top_slice = metrics.get("top_slice")
        if top_slice:
            lines.append(f"- {top_slice}")
        baseline = metrics.get("baseline_line")
        if baseline:
            lines.append(f"- {baseline}")
        rolling = metrics.get("rolling_line")
        if rolling:
            lines.append(f"- {rolling}")
        return lines

    # Sample too small for any confident statistic — log metrics, never alert.
    if n is None or n < MIN_CLEAN_N:
        lines = [
            "🔎 FUSION CALIBRATION — THIN SAMPLE (below MIN_CLEAN_N) 🔎",
            f"Tier-1 realized-exit sample (n={n}) is too thin to gauge calibration health.",
            "No breach claim is made. Metrics are logged for accumulation tracking only.\n",
            "[Monitored Metrics]",
        ] + monitored_block()
        lines += [
            "",
            "Living Dashboard: [[devops/latest-fusion-calibration-report.md]]",
        ]
        return {
            "kind": "THIN_SAMPLE",
            "card": False,
            "card_title": "",
            "card_body": "",
            "stdout_lines": lines,
        }

    # Genuine breaches (used by both thin-sample INVESTIGATE and confident BREACH).
    breaches = []
    if win_rate is not None and win_rate < WIN_RATE_THRESHOLD:
        breaches.append(f"Win Rate: {win_rate:.1f}% (Threshold: < {WIN_RATE_THRESHOLD:.1f}%)")
    if avg_pnl is not None and avg_pnl < PNL_THRESHOLD:
        breaches.append(f"Average PnL%: {avg_pnl:.2f}% (Threshold: < {PNL_THRESHOLD:.2f}%)")
    _mce, _mce_label = mce_decision_value()
    if _mce is not None and _mce > MCE_THRESHOLD:
        breaches.append(
            f"Sample-weighted MCE ({_mce_label}): {_mce:.1f}pp "
            f"(Threshold: > {MCE_THRESHOLD:.1f}pp)")

    # --- SAMPLE ACCUMULATION TRACKER (t_e79f6568) ---
    # Track cumulative Tier-1 realized-exit outcomes and surface accumulation
    # progress toward the VALIDATED_EDGE floor (n >= TIER1_INVESTIGATE_FLOOR = 300,
    # the report's own VALIDATED_EDGE_STATUS: INSUFFICIENT_SAMPLE threshold).
    # Emitted whenever there is NO breach, for BOTH the accumulation regime
    # (MIN_CLEAN_N <= n < 300) and once the floor is reached (n >= 300). This
    # is a SAMPLE-READINESS status ONLY: it NEVER opens a card and NEVER
    # triggers recalibration (the watchdog has no recalibration code path; the
    # HOLD is governed by t_b4c824c7 / fusion_recalibration_hold_monitor.py).
    if not breaches:
        floor_reached = (n is not None and n >= TIER1_INVESTIGATE_FLOOR)
        if floor_reached:
            state = f"validated-edge floor REACHED (n={n} >= {TIER1_INVESTIGATE_FLOOR})"
        else:
            state = f"accumulating (n={n} / threshold={TIER1_INVESTIGATE_FLOOR})"
        lines = [
            "📈 FUSION CALIBRATION — SAMPLE_ACCUMULATING 📈",
            f"Tier-1 realized-exit cumulative outcomes are {state}.",
            "Recalibration remains a governed Frank/PM change (t_b4c824c7); "
            "this monitor does NOT auto-trigger recalibration.\n",
            "[Monitored Metrics]",
        ] + monitored_block() + [
            "",
            "Living Dashboard: [[devops/latest-fusion-calibration-report.md]]",
        ]
        return {
            "kind": "SAMPLE_ACCUMULATING",
            "card": False,
            "card_title": "",
            "card_body": "",
            "stdout_lines": lines,
        }

    # Thin but statistically-usable sample (100 <= n < 300).
    if n < TIER1_INVESTIGATE_FLOOR:
        if not breaches:
            # Thin AND healthy: log metrics, do NOT claim a breach.
            lines = [
                "🔎 FUSION CALIBRATION — THIN SAMPLE (healthy) 🔎",
                f"Tier-1 realized-exit sample (n={n}) is below the report's validated-edge floor "
                f"({TIER1_INVESTIGATE_FLOOR}) but no monitored metric breached its threshold.",
                "No breach claim is made. Accumulate more Tier-1 outcomes before any recalibration decision.\n",
                "[Monitored Metrics]",
            ] + monitored_block()
            lines += [
                "",
                "Living Dashboard: [[devops/latest-fusion-calibration-report.md]]",
            ]
            return {
                "kind": "THIN_SAMPLE",
                "card": False,
                "card_title": "",
                "card_body": "",
                "stdout_lines": lines,
            }
        # Thin AND genuinely breaching: report as an INSUFFICIENT_SAMPLE
        # investigate-state, NOT a validated failure and NOT an alert/flag.
        # Per t_ef700332 the monitoring layer MUST NOT raise a flag/card until
        # the Tier-1 sample reaches the validated-edge floor (n >= 300). The
        # breach signal is real in direction but statistically imprecise at this
        # n (VALIDATED_EDGE_STATUS: INSUFFICIENT_SAMPLE), so we report the
        # current n and mark INSUFFICIENT_SAMPLE, then accumulate — but we do
        # NOT open a kanban card / raise an alert. The n>=300 flag is owned by
        # tier1_sample_gate.py (READY_FOR_VALIDATION / BREACH) and this
        # watchdog's own n>=300 BREACH path. No recalibration is triggered.
        mce_breach = any("MCE" in b for b in breaches)
        breach_summary = "\n".join(f"- {b}" for b in breaches)
        lines = [
            "🔎 FUSION CALIBRATION — INVESTIGATE / INSUFFICIENT_SAMPLE (thin Tier-1 sample, genuine breach) 🔎",
            f"A genuine breach was detected on a statistically thin Tier-1 sample (n={n} < {TIER1_INVESTIGATE_FLOOR}).",
            f"VALIDATED_EDGE_STATUS: INSUFFICIENT_SAMPLE — this is NOT a validated/confirmed "
            f"calibration failure and NO alert/flag card is raised (t_ef700332). Accumulate more "
            f"Tier-1 realized-exit outcomes until n >= {TIER1_INVESTIGATE_FLOOR} before any "
            f"recalibration decision.\n",
            "[Breached Metrics]",
        ] + [f"- {b}" for b in breaches] + [
            "",
            "[Monitored Metrics]",
        ] + monitored_block() + [
            "",
            "Living Dashboard: [[devops/latest-fusion-calibration-report.md]]",
            "Response Runbook: [[operations/runbooks/fusion-calibration-alert-runbook.md]]",
        ]
        return {
            "kind": "INVESTIGATE",
            "card": False,   # t_ef700332: no flag/alert below the validated-edge floor
            "card_title": "",
            "card_body": "",
            "stdout_lines": lines,
        }

    # Confident sample (n >= 300) with a breach: emit BREACH.
    breach_summary = "\n".join(f"- {b}" for b in breaches)
    _mon_lines = monitored_block()
    body = (
        "The Fusion Engine Calibration watchdog detected one or more performance/calibration "
        f"breaches on a confident sample (n={n}):\n\n"
        f"{breach_summary}\n\n"
        "[All Monitored Metrics]\n"
        + "\n".join(_mon_lines)
        + "\n\n"
        "Please refer to the response runbook: [[operations/runbooks/fusion-calibration-alert-runbook.md]]\n"
        "See detailed report in Obsidian: [[devops/latest-fusion-calibration-report.md]]"
    )
    title = f"Sample-weighted Calibration Threshold Breach: MCE={fmt(_mce, 'pp')}"
    lines = [
        "🚨 FUSION CALIBRATION THRESHOLD BREACH ALERT 🚨",
        "At least one key performance or calibration metric has breached safety thresholds on a confident sample.\n",
        "[Breached Metrics]",
    ] + [f"- {b}" for b in breaches] + [
        "",
        "[All Monitored Metrics]",
    ] + _mon_lines + [
        "",
        "Living Dashboard: [[devops/latest-fusion-calibration-report.md]]",
        "Response Runbook: [[operations/runbooks/fusion-calibration-alert-runbook.md]]",
    ]
    # t_ba7757c8: stable cooldown family for card creation — the sorted set of
    # breached metric names (MCE / WIN_RATE / PNL). A persistent breach that
    # re-fires every 6h maps to the same family, so the 24h cooldown suppresses
    # duplicate triage cards while stdout keeps alerting every tick.
    family = sorted({
        ("MCE" if "MCE" in b else
         "WIN_RATE" if "Win Rate" in b else
         "PNL" if "PnL" in b else "OTHER")
        for b in breaches
    })
    return {
        "kind": "BREACH",
        "card": True,
        "card_title": title,
        "card_body": body,
        "stdout_lines": lines,
        "breach_family": family,
    }


def regression_test() -> int:
    """Deterministic, DB-free assertions pinning the t_d8ccf4bd fixes.

    Returns 0 when all assertions pass, 1 otherwise. Run via
    ``calibration_watchdog.py --regression-test`` (no DB / report / Obsidian /
    kanban side effects).
    """
    # t_ba7757c8: keep the cooldown/baseline state file out of the live
    # ~/.hermes/scripts/state/ dir during the whole DB-free harness.
    global STATE_FILE, STATE_DIR
    import tempfile as _tf
    _orig_state_file, _orig_state_dir = STATE_FILE, STATE_DIR
    _tmp_state = _tf.TemporaryDirectory(prefix="calibration-watchdog-regression-")
    STATE_FILE = Path(_tmp_state.name) / "state.json"
    STATE_DIR = Path(_tmp_state.name)

    failures = []

    def check(name, cond):
        if cond:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name}")
            failures.append(name)

    # (A) Healthy thin sample must NOT claim a breach / open a card; it emits
    #     the SAMPLE_ACCUMULATING tracker (t_e79f6568) with no card.
    d = decide_alert({"n": 150, "win_rate": 52.0, "avg_pnl": 0.4, "weighted_mce": 10.0})
    check("n=150, MCE=10pp -> SAMPLE_ACCUMULATING (no false breach)",
          d is not None and d["kind"] == "SAMPLE_ACCUMULATING")
    check("n=150 -> SAMPLE_ACCUMULATING opens NO card", d is not None and d["card"] is False)
    check("n=150 -> emits current n + threshold",
          d is not None and "n=150" in " ".join(d["stdout_lines"])
          and "300" in " ".join(d["stdout_lines"]))

    # (B) Real thin-sample MCE breach -> INVESTIGATE, NO card (t_ef700332: no
    #     alert/flag below the validated-edge floor). Honest INSUFFICIENT_SAMPLE.
    d = decide_alert({"n": 124, "win_rate": 50.0, "avg_pnl": 0.1, "weighted_mce": 20.07})
    check("n=124, MCE=20.07pp -> INVESTIGATE", d is not None and d["kind"] == "INVESTIGATE")
    check("n=124, MCE=20.07pp -> NO card raised (t_ef700332: floor not reached)",
          d is not None and d["card"] is False)
    check("n=124, MCE=20.07pp -> reports INSUFFICIENT_SAMPLE",
          "INSUFFICIENT_SAMPLE" in " ".join(d["stdout_lines"]))
    check("n=124, MCE=20.07pp -> does NOT claim a validated failure",
          "validated" not in " ".join(d["stdout_lines"]).lower()
          or "not a validated" in " ".join(d["stdout_lines"]).lower())

    # (C) n<100 silent gap closed: metrics logged, no card, no breach claim.
    d = decide_alert({"n": 42, "win_rate": 51.0, "avg_pnl": 0.2, "weighted_mce": 9.0})
    check("n=42 -> THIN_SAMPLE (logs metrics, no card)", d is not None and d["kind"] == "THIN_SAMPLE" and d["card"] is False)
    check("n=42 -> logs Tier-1 n", "n=42" in " ".join(d["stdout_lines"]))
    check("n=42 -> logs MCE metric", "Sample-weighted MCE" in " ".join(d["stdout_lines"]))

    # (D) Confident healthy sample -> SAMPLE_ACCUMULATING (floor reached), no card,
    #     no recalibration side-effect. This is the t_e79f6568 accumulation status
    #     emitted once the validated-edge floor is met.
    d = decide_alert({"n": 400, "win_rate": 55.0, "avg_pnl": 0.7, "weighted_mce": 8.0})
    check("n=400, healthy -> SAMPLE_ACCUMULATING (floor reached, no breach card)",
          d is not None and d["kind"] == "SAMPLE_ACCUMULATING" and d["card"] is False)

    # (E) Confident MCE breach -> BREACH + card.
    d = decide_alert({"n": 400, "win_rate": 55.0, "avg_pnl": 0.7, "weighted_mce": 18.0})
    check("n=400, MCE=18pp -> BREACH", d is not None and d["kind"] == "BREACH" and d["card"] is True)

    # (F) Thin win-rate breach -> INVESTIGATE (not BREACH), honest.
    d = decide_alert({"n": 200, "win_rate": 35.0, "avg_pnl": -0.5, "weighted_mce": 9.0})
    check("n=200, WR=35% -> INVESTIGATE (thin genuine breach)", d is not None and d["kind"] == "INVESTIGATE")
    check("n=200, WR=35% -> no false MCE-breach claim", "mce breach" not in " ".join(d["stdout_lines"]).lower())

    # (G) SAMPLE_ACCUMULATING guarantees: explicitly no recalibration wording and
    #     that the status string is present exactly once in stdout.
    d = decide_alert({"n": 260, "win_rate": 54.0, "avg_pnl": 0.6, "weighted_mce": 11.0})
    check("n=260 -> SAMPLE_ACCUMULATING status present",
          d is not None and d["kind"] == "SAMPLE_ACCUMULATING"
          and any("SAMPLE_ACCUMULATING" in ln for ln in d["stdout_lines"]))
    check("n=260 -> no recalibration side-effect / no card",
          d is not None and d["card"] is False
          and "recalibrat" in " ".join(d["stdout_lines"]).lower())

    # (H) Below-floor delivery suppression (t_5c238cc5 defect 2): when the
    # Tier-1 sample is below the validated-edge floor (n < 300), the watchdog
    # must NOT emit any outbound stdout (no Discord/alert delivery) and must NOT
    # open a card. Accumulation evidence persists only via the living dashboard
    # (save_to_obsidian, run every tick at line 640) which is bypassed here in a
    # DB-free harness so this test has no Obsidian / kanban / DB side effects.
    global run_report, save_to_obsidian, parse_metrics, compute_tier1_scored_mce, compute_tier1_slice_context
    import io as _io, contextlib as _cl
    _orig_run, _orig_save, _orig_parse, _orig_t1mce, _orig_slice = (
        run_report, save_to_obsidian, parse_metrics, compute_tier1_scored_mce, compute_tier1_slice_context)
    try:
        run_report = lambda: (0, "")
        save_to_obsidian = lambda c: None
        compute_tier1_scored_mce = lambda: (None, None, "mocked")
        compute_tier1_slice_context = lambda: None
        parse_metrics = lambda out: {
            "n": 127, "win_rate": 40.16, "avg_pnl": 0.4772,
            "weighted_mce": 19.4, "has_integrity_warning": False,
            "sql_errors": 0, "epoch_start": "2026-07-05",
        }
        _buf = _io.StringIO()
        with _cl.redirect_stdout(_buf):
            _rc = main()
        _stdout = _buf.getvalue()
        check("below floor (n=127): NO outbound stdout delivered",
              _stdout.strip() == "")
        check("below floor (n=127): main returns 0",
              _rc == 0)
    finally:
        run_report, save_to_obsidian, parse_metrics, compute_tier1_scored_mce, compute_tier1_slice_context = (
            _orig_run, _orig_save, _orig_parse, _orig_t1mce, _orig_slice)

    # (I) LIVE-PATH n<100 gap (t_7223ef5b acceptance #2): the live main() must
    # route a numeric n<100 through decide_alert() so the THIN_SAMPLE verdict
    # (four monitored metrics + explicit "too thin to gauge" note) is PERSISTED
    # to the living dashboard, while staying silent on outbound stdout and
    # opening NO card (below-floor suppression preserved). This is the actual
    # defect closed by this task: previously main() early-returned `return 0`
    # for n<MIN_CLEAN_N and the THIN_SAMPLE verdict was dead code in the live
    # path, so the smallest samples were a blind spot on the dashboard.
    _orig_run, _orig_save, _orig_parse, _orig_t1mce, _orig_slice = (
        run_report, save_to_obsidian, parse_metrics, compute_tier1_scored_mce, compute_tier1_slice_context)
    _dash = {}
    try:
        run_report = lambda: (0, "RAW-REPORT")
        save_to_obsidian = lambda c: _dash.update(content=c)
        compute_tier1_scored_mce = lambda: (None, None, "mocked")
        compute_tier1_slice_context = lambda: None
        parse_metrics = lambda out: {
            "n": 42, "win_rate": 51.0, "avg_pnl": 0.2,
            "weighted_mce": 9.0, "has_integrity_warning": False,
            "sql_errors": 0, "epoch_start": "2026-07-05",
        }
        _buf = _io.StringIO()
        with _cl.redirect_stdout(_buf):
            _rc = main()
        _stdout = _buf.getvalue()
        _dash_c = _dash.get("content", "")
        check("live n=42: main returns 0", _rc == 0)
        check("live n=42: NO outbound stdout (below-floor suppression)",
              _stdout.strip() == "")
        check("live n=42: dashboard persisted THIN_SAMPLE verdict",
              "too thin to gauge" in _dash_c.lower())
        check("live n=42: dashboard logged Tier-1 n=42",
              "n=42" in _dash_c)
        check("live n=42: dashboard logged Sample-weighted MCE metric",
              "Sample-weighted MCE" in _dash_c)
        check("live n=42: dashboard logged Tier-1 scored-only MCE metric",
              "Tier-1 scored-only MCE (authoritative" in _dash_c)
        check("live n=42: dashboard labels merged MCE non-authoritative",
              "MERGED sample — non-authoritative" in _dash_c)
        check("live n=42: dashboard logged Win Rate metric",
              "Win Rate" in _dash_c)
        check("live n=42: dashboard logged Average PnL% metric",
              "Average PnL%" in _dash_c)
        check("live n=42: NO breach CLAIM made (THIN_SAMPLE, no MCE breach line)",
              "MCE breach" not in _dash_c.lower()
              and "Sample-weighted MCE: 9.0pp (Threshold" not in _dash_c)
    finally:
        run_report, save_to_obsidian, parse_metrics, compute_tier1_scored_mce, compute_tier1_slice_context = (
            _orig_run, _orig_save, _orig_parse, _orig_t1mce, _orig_slice)

    # (J) LIVE-PATH n=None stays fully silent (runbook §5 taxonomy): no
    # verdict persisted, no stdout, no card.
    _orig_run, _orig_save, _orig_parse, _orig_t1mce, _orig_slice = (
        run_report, save_to_obsidian, parse_metrics, compute_tier1_scored_mce, compute_tier1_slice_context)
    _dash_none = {}
    try:
        run_report = lambda: (0, "RAW-REPORT")
        save_to_obsidian = lambda c: _dash_none.update(content=c)
        compute_tier1_scored_mce = lambda: (None, None, "mocked")
        compute_tier1_slice_context = lambda: None
        parse_metrics = lambda out: {
            "n": None, "win_rate": None, "avg_pnl": None,
            "weighted_mce": None, "has_integrity_warning": False,
            "sql_errors": 0, "epoch_start": "2026-07-05",
        }
        _buf = _io.StringIO()
        with _cl.redirect_stdout(_buf):
            _rc = main()
        _stdout = _buf.getvalue()
        check("live n=None: main returns 0", _rc == 0)
        check("live n=None: NO outbound stdout", _stdout.strip() == "")
        check("live n=None: NO dashboard verdict persisted",
              "too thin" not in _dash_none.get("content", "").lower()
              and "BREACH" not in _dash_none.get("content", ""))
    finally:
        run_report, save_to_obsidian, parse_metrics, compute_tier1_scored_mce, compute_tier1_slice_context = (
            _orig_run, _orig_save, _orig_parse, _orig_t1mce, _orig_slice)

    # (K) t_0bfed6a8 headline-display fix: the watchdog must NEVER pair the
    # merged-sample MCE with the Tier-1 n. When the authoritative Tier-1
    # scored-only MCE is available, the breach/headline keys on it and the
    # merged figure is labeled non-authoritative with its own n.
    d = decide_alert({
        "n": 400, "win_rate": 55.0, "avg_pnl": 0.7,
        "weighted_mce": 15.8, "merged_mce": 15.8, "merged_n": 7842,
        "tier1_scored_mce": 32.72, "tier1_scored_n": 381,
    })
    check("t_0bfed6a8: authoritative MCE returns a BREACH decision", d is not None)
    if d is not None:
        check("t_0bfed6a8: authoritative MCE drives the BREACH decision",
              d["kind"] == "BREACH")
        check("t_0bfed6a8: breach line labels Tier-1 scored-only as authoritative",
              any("Tier-1 scored-only (authoritative" in ln for ln in d["stdout_lines"]))
        check("t_0bfed6a8: merged figure labeled non-authoritative with own n",
              any("MERGED sample — non-authoritative" in ln and "n=7842" in ln
                  for ln in d["stdout_lines"]))
        check("t_0bfed6a8: authoritative MCE + scored n in monitored block",
              any("Tier-1 scored-only MCE (authoritative, net-of-cost): 32.7pp (n=381)"
                  in ln for ln in d["stdout_lines"]))
        check("t_0bfed6a8: headline n is Tier-1 clean n, not merged",
              any("Tier-1 clean unique journeys (n): 400" in ln
                  for ln in d["stdout_lines"]))

    # (L) Fallback: when the authoritative compute is unavailable, the merged
    # figure is used but explicitly labeled non-authoritative (never misread
    # as Tier-1).
    d = decide_alert({
        "n": 400, "win_rate": 55.0, "avg_pnl": 0.7,
        "weighted_mce": 15.8, "merged_mce": 15.8, "merged_n": 7842,
    })
    check("t_0bfed6a8: merged fallback returns a BREACH decision", d is not None)
    if d is not None:
        check("t_0bfed6a8: merged fallback breach line labeled non-authoritative",
              d["kind"] == "BREACH"
              and any("MERGED sample (non-authoritative)" in ln
                      for ln in d["stdout_lines"]))
        check("t_0bfed6a8: authoritative line renders -- when unavailable",
              any("Tier-1 scored-only MCE (authoritative, net-of-cost): --" in ln
                  for ln in d["stdout_lines"]))

    # (M) Pure math parity: compute_bucket_weighted_mce excludes unreconciled
    # rows and reproduces the report's bucket math.
    _fixture = [
        {"conviction_score": "0.62", "ledger_matched": "true", "ledger_is_win": "false"},
        {"conviction_score": "0.62", "ledger_matched": "true", "ledger_is_win": "false"},
        {"conviction_score": "0.62", "ledger_matched": "true", "ledger_is_win": "false"},
        {"conviction_score": "0.62", "ledger_matched": "true", "ledger_is_win": "true"},
        {"conviction_score": "0.90", "ledger_matched": "true", "ledger_is_win": "true"},
        {"conviction_score": "0.90", "ledger_matched": "true", "ledger_is_win": "false"},
        {"conviction_score": "0.30", "ledger_matched": "false", "ledger_is_win": ""},
    ]
    _mce, _n = compute_bucket_weighted_mce(_fixture)
    check("t_0bfed6a8: compute_bucket_weighted_mce excludes unreconciled rows",
          _n == 6)
    # [0.60,0.65): n=4, wr=25.0, expected=62.0 -> err 37.0, weight 4/6
    # [0.90,0.95): n=2, wr=50.0, expected=90.0 -> err 40.0, weight 2/6
    # weighted = 37.0*4/6 + 40.0*2/6 = 38.0
    check("t_0bfed6a8: compute_bucket_weighted_mce math matches report",
          _mce is not None and abs(_mce - 38.0) < 0.01)

    # (N) t_ba7757c8: WR regex now matches the pinned report's Tier-1 label.
    _parsed = parse_metrics(
        "| **Tier-1 clean win rate** | **19.0%** |\n"
        "- **Tier-1 clean win rate: 19.0%** (72W / 307L).\n"
        "**Window:** last 30d (30d) of resolved outcomes\n"
        "| Clean sample resolution span | 2026-07-06 02:57 -> 2026-08-03 08:03 UTC |\n"
        "| Clean sample epoch gate | 2026-07-05 22:08:00+00 |\n")
    check("t_ba7757c8: WR regex matches 'Tier-1 clean win rate' table row",
          _parsed.get("win_rate") == 19.0)
    check("t_ba7757c8: WR observation form fallback works",
          parse_metrics("- **Tier-1 clean win rate: 17.5%**").get("win_rate") == 17.5)
    check("t_ba7757c8: sample_window parsed from report header",
          _parsed.get("sample_window") == "last 30d (30d)")
    check("t_ba7757c8: resolution_span parsed from Section 1",
          "2026-07-06" in (_parsed.get("resolution_span") or ""))

    # (O) t_ba7757c8: top strategy slice computation (pure, no DB).
    _slice_rows = [
        {"conviction_score": "0.60", "ledger_matched": "true", "ledger_is_win": "false",
         "direction": "LONG", "timeframe": "1h", "lane": "trade_close"},
        {"conviction_score": "0.60", "ledger_matched": "true", "ledger_is_win": "false",
         "direction": "LONG", "timeframe": "1h", "lane": "trade_close"},
        {"conviction_score": "0.60", "ledger_matched": "true", "ledger_is_win": "true",
         "direction": "LONG", "timeframe": "1h", "lane": "trade_close"},
        {"conviction_score": "0.90", "ledger_matched": "true", "ledger_is_win": "true",
         "direction": "SHORT", "timeframe": "1m", "lane": "trade_close"},
        {"conviction_score": "0.90", "ledger_matched": "true", "ledger_is_win": "false",
         "direction": "SHORT", "timeframe": "1m", "lane": "journey_finalizer"},
        {"conviction_score": "0.30", "ledger_matched": "false", "ledger_is_win": "",
         "direction": "LONG", "timeframe": "1h", "lane": "journey_finalizer"},
    ]
    _slice_line = compute_top_contributing_slice(_slice_rows)
    check("t_ba7757c8: top slice excludes unreconciled rows and returns a line",
          _slice_line is not None and "Top strategy slice:" in _slice_line)
    check("t_ba7757c8: top slice picks the largest MCE contributor",
          "contrib" in (_slice_line or ""))

    # (P) t_ba7757c8: BREACH payload includes evidence context + report links.
    d = decide_alert({
        "n": 400, "win_rate": 55.0, "avg_pnl": 0.7,
        "weighted_mce": 15.8, "merged_mce": 15.8, "merged_n": 7842,
        "tier1_scored_mce": 32.72, "tier1_scored_n": 381,
        "sample_window": "last 30d (30d)",
        "resolution_span": "2026-07-06 -> 2026-08-03",
        "top_slice": "Top strategy slice: dir=LONG (n=184, WR 13.6%, contrib 18.70pp)",
        "baseline_line": "Baseline (previous run): Tier-1 scored-only MCE 32.8pp (n=382) at 2026-08-03T07:44:00+00:00",
        "rolling_line": "Rolling MCE (last 2 runs): 32.8pp, 32.7pp",
    })
    check("t_ba7757c8: BREACH decision produced", d is not None and d["kind"] == "BREACH")
    if d is not None:
        _all = "\n".join(d["stdout_lines"])
        check("t_ba7757c8: payload includes sample window",
              "Sample window: last 30d (30d)" in _all)
        check("t_ba7757c8: payload includes top strategy slice",
              "Top strategy slice: dir=LONG" in _all)
        check("t_ba7757c8: payload includes baseline (previous run)",
              "Baseline (previous run)" in _all)
        check("t_ba7757c8: payload includes rolling MCE",
              "Rolling MCE (last 2 runs)" in _all)
        check("t_ba7757c8: payload keeps report links",
              "[[devops/latest-fusion-calibration-report.md]]" in _all
              and "[[operations/runbooks/fusion-calibration-alert-runbook.md]]" in _all)
        check("t_ba7757c8: breach family computed (MCE only here)",
              d.get("breach_family") == ["MCE"])

    # (Q) t_ba7757c8: cooldown suppresses duplicate card for the SAME family
    # within BREACH_CARD_COOLDOWN_HOURS, but a fresh family / expired window
    # still creates a card. Fail-open: unreadable state allows card creation.
    global create_kanban_task
    _orig_ckt = create_kanban_task
    _cards = []
    create_kanban_task = lambda title, body, key: _cards.append(key)
    _now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
    try:
        _rc1 = emit_verdict({"kind": "BREACH", "card": True,
                             "card_title": "T", "card_body": "B",
                             "stdout_lines": ["alert"],
                             "breach_family": ["MCE"]}, 400)
        check("t_ba7757c8: first BREACH fires a card", _rc1 == 0 and len(_cards) == 1)
        _buf2 = _io.StringIO()
        with _cl.redirect_stdout(_buf2):
            _rc2 = emit_verdict({"kind": "BREACH", "card": True,
                                 "card_title": "T", "card_body": "B",
                                 "stdout_lines": ["alert"],
                                 "breach_family": ["MCE"]}, 400)
        check("t_ba7757c8: same-family re-fire within 24h suppresses card",
              _rc2 == 0 and len(_cards) == 1 and "cooldown" in _buf2.getvalue().lower())
        _rc3 = emit_verdict({"kind": "BREACH", "card": True,
                             "card_title": "T", "card_body": "B",
                             "stdout_lines": ["alert"],
                             "breach_family": ["PNL"]}, 400)
        check("t_ba7757c8: different breach family creates a new card",
              _rc3 == 0 and len(_cards) == 2)
    finally:
        create_kanban_task = _orig_ckt

    # (R) t_ba7757c8: state-file fail-open — an unreadable/corrupt state file
    # must NOT wedge card creation (returns not-in-cooldown).
    _orig_state_file2 = STATE_FILE
    try:
        STATE_FILE = Path(_tmp_state.name) / "missing.json"
        check("t_ba7757c8: missing state file -> not in cooldown (fail-open)",
              _card_in_cooldown("MCE", _now) is False)
    finally:
        STATE_FILE = _orig_state_file2

    # Restore the temp state redirect used for the whole harness.
    STATE_FILE, STATE_DIR = _orig_state_file, _orig_state_dir
    _tmp_state.cleanup()

    if failures:
        print(f"\nREGRESSION FAILED: {len(failures)} assertion(s) failed: {failures}")
        return 1
    print("\nREGRESSION PASSED: all t_d8ccf4bd / t_7223ef5b / t_0bfed6a8 / t_ba7757c8 assertions hold.")
    return 0


def _load_state() -> dict:
    """Fail-open JSON state reader. Never raises; on any error returns {} so
    the caller behaves as if there is no prior state (i.e. allow card
    creation / emit no baseline) — a state bug must never wedge the alert."""
    try:
        if STATE_FILE.exists():
            import json
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    """Best-effort atomic JSON state writer. Never raises (warn on failure)."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        import json
        import tempfile
        fd, tmp = tempfile.mkstemp(
            dir=str(STATE_DIR), prefix=".calibration_watchdog_state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, sort_keys=True)
            os.replace(tmp, STATE_FILE)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except Exception as e:
        print(f"Warning: could not persist watchdog state: {e}", file=sys.stderr)


def _card_in_cooldown(family_key: str, now) -> bool:
    """True when a kanban card for this breach family was created within
    BREACH_CARD_COOLDOWN_HOURS. Fail-open: any read error returns False so a
    card is still created (never suppress on error)."""
    try:
        state = _load_state()
        last = (state.get("last_card_at") or {}).get(family_key)
        if not last:
            return False
        last_dt = datetime.fromisoformat(last)
        return (now - last_dt).total_seconds() < BREACH_CARD_COOLDOWN_HOURS * 3600
    except Exception:
        return False


def _record_card(family_key: str, now) -> None:
    """Record a card creation timestamp for a breach family (best-effort)."""
    try:
        state = _load_state()
        state.setdefault("last_card_at", {})[family_key] = now.isoformat()
        _save_state(state)
    except Exception:
        pass


def _build_baseline_rolling(metrics: dict) -> tuple[str | None, str | None]:
    """Build baseline/rolling MCE evidence lines from the persisted run history.

    Returns (baseline_line, rolling_line); both None when no history exists.
    The history is a list of {at, tier1_scored_mce, tier1_scored_n} dicts
    written every tick by _record_run_history. Fail-open: unreadable/missing
    history yields (None, None), never an exception.
    """
    try:
        state = _load_state()
        hist = state.get("run_history") or []
        if not hist:
            return None, None
        prev = hist[-1]
        baseline = None
        if prev.get("tier1_scored_mce") is not None:
            baseline = (
                f"Baseline (previous run): Tier-1 scored-only MCE "
                f"{prev['tier1_scored_mce']:.1f}pp (n={prev.get('tier1_scored_n')}) "
                f"at {prev.get('at', '?')}"
            )
        mce_vals = [
            r["tier1_scored_mce"] for r in hist
            if r.get("tier1_scored_mce") is not None
        ][-5:]
        rolling = None
        if len(mce_vals) >= 2:
            rolling = (
                "Rolling MCE (last "
                f"{len(mce_vals)} runs): "
                + ", ".join(f"{v:.1f}pp" for v in mce_vals)
            )
        return baseline, rolling
    except Exception:
        return None, None


def _record_run_history(metrics: dict) -> None:
    """Append the current run's Tier-1 scored MCE to the rolling history
    (capped at 40 runs). Best-effort; never raises."""
    try:
        t1 = metrics.get("tier1_scored_mce")
        if t1 is None:
            return
        state = _load_state()
        hist = state.get("run_history") or []
        hist.append({
            "at": datetime.now(timezone.utc).isoformat(),
            "tier1_scored_mce": t1,
            "tier1_scored_n": metrics.get("tier1_scored_n"),
        })
        state["run_history"] = hist[-40:]
        _save_state(state)
    except Exception:
        pass


def emit_verdict(decision: dict | None, n: int | None) -> int:
    """Persist an alert decision to the living dashboard and deliver it.

    Single post-classification sink for the live path. Records the verdict
    (including THIN_SAMPLE / SAMPLE_ACCUMULATING accumulation notes) to the
    living dashboard so the four monitored metrics are always observable,
    then delivers to stdout ONLY when the Tier-1 sample has reached the
    validated-edge floor (n >= TIER1_INVESTIGATE_FLOOR). Below the floor the
    evidence persists via the dashboard but must NOT raise an outbound alert /
    flag / card (t_ef700332 + t_5c238cc5). The watchdog never mutates the
    engine, trade_intents, or DB here.

    ``n`` is passed explicitly because decide_alert()'s returned dict does not
    carry the sample size; the floor check depends on it.
    """
    if decision is None:
        return 0
    # Persist the verdict to the living dashboard on every tick so the
    # four monitored metrics (and the thin-sample note) remain observable
    # even when stdout delivery is suppressed below the validated-edge floor.
    save_to_obsidian("\n".join(decision["stdout_lines"]))
    below_floor = (n is not None and n < TIER1_INVESTIGATE_FLOOR)
    if below_floor:
        return 0
    for line in decision["stdout_lines"]:
        print(line)
    if decision["card"]:
        now = datetime.now(timezone.utc)
        family_key = "|".join(
            decision.get("breach_family") or [decision["kind"]])
        if _card_in_cooldown(family_key, now):
            # t_ba7757c8: persistent breach — stdout alert already emitted
            # above; suppress only the duplicate triage card while the same
            # breach family re-fires within the cooldown window.
            print(
                f"(cooldown) {decision['kind']} triage card suppressed for "
                f"family [{family_key}] — last card created within "
                f"{BREACH_CARD_COOLDOWN_HOURS}h; stdout alert still delivered"
            )
            return 0
        id_key = (
            f"calibration-{decision['kind'].lower()}-"
            f"{now.strftime('%Y%m%dT%H')}"
        )
        create_kanban_task(decision["card_title"], decision["card_body"], id_key)
        _record_card(family_key, now)
    return 0


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

    # t_0bfed6a8 headline-display fix: compute the authoritative Tier-1
    # scored-only (net-of-cost) MCE directly. The pinned report's parsed
    # "Sample-weighted MCE" is the MERGED figure (diluted by ~90% synthetic
    # Tier-2 rows); pairing it with the Tier-1 n was the display defect. The
    # decision in decide_alert() keys on this authoritative value when
    # available and otherwise falls back to the merged figure LABELED
    # non-authoritative. Best-effort read-only: a failure here never silences
    # the alert, it only degrades the headline to the labeled merged figure.
    tier1_mce, tier1_n, tier1_err = compute_tier1_scored_mce()
    metrics["tier1_scored_mce"] = tier1_mce
    metrics["tier1_scored_n"] = tier1_n
    metrics["tier1_scored_error"] = tier1_err

    # t_ba7757c8 payload enrichment: top strategy slice + baseline/rolling MCE.
    # All best-effort read-only; failures degrade to omitted lines, never block.
    metrics["top_slice"] = compute_tier1_slice_context()
    _bl, _rl = _build_baseline_rolling(metrics)
    metrics["baseline_line"] = _bl
    metrics["rolling_line"] = _rl

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
    # n is now the Tier-1 realized-exit calibration sample (NOT the MERGED
    # Tier-1+Tier-2 synthetic count) — see parse_metrics / t_016ac4e4.
    if n is None:
        # Unparseable sample: stay completely silent. The no_agent cron only
        # delivers non-empty stdout, and runbook §5 taxonomizes n=None as
        # "(silent)" — no verdict, no card. (The t<100 THIN_SAMPLE verdict is
        # handled by decide_alert() below and IS persisted to the dashboard.)
        return 0

    # 2b. Route EVERY numeric sample through the hardened, regression-tested
    # alert classifier — the single source of truth for classification
    # (THIN_SAMPLE / SAMPLE_ACCUMULATING / INVESTIGATE / BREACH / None). This
    # embeds the t_7223ef5b / t_d8ccf4bd hardening: no false INVESTIGATE on a
    # healthy thin sample, the SAMPLE_ACCUMULATING tracker (t_e79f6568), the
    # closed n<100 THIN_SAMPLE gap (four metrics + explicit thin note emitted
    # for the smallest samples instead of silence), and NO recalibration
    # side-effects (the watchdog has no recalibration code path; the HOLD is
    # governed by t_b4c824c7 / fusion_recalibration_hold_monitor.py).
    #
    # Previously the live path early-returned `return 0` for n < MIN_CLEAN_N,
    # which silently dropped decide_alert()'s correct THIN_SAMPLE verdict
    # (t_7223ef5b defect 2): the four metrics + "too thin to gauge" note never
    # reached the dashboard exactly when the sample was smallest. Routing all
    # numeric n through decide_alert() -> emit_verdict() closes that gap while
    # preserving below-floor stdout suppression (t_ef700332 + t_5c238cc5):
    # emit_verdict() persists the verdict to the living dashboard on every tick
    # but only prints/opens a card above the validated-edge floor.
    decision = decide_alert(metrics)
    # t_ba7757c8: persist this run's Tier-1 scored MCE into the rolling history
    # for the next tick's baseline comparison (best-effort, never raises).
    _record_run_history(metrics)
    return emit_verdict(decision, n)

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
    elif "--regression-test" in sys.argv:
        # DB-free assertion suite pinning the t_d8ccf4bd / t_7223ef5b /
        # t_e79f6568 alert-classification contract. Never touches the report,
        # Obsidian, or kanban.
        sys.exit(regression_test())
    else:
        sys.exit(main())
