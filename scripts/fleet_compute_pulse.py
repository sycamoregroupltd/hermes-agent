#!/usr/bin/env python3
"""fleet_compute_pulse.py — compute-share + cost-per-verified-experiment pulse
(card t_a737a025, RESHAPE POR standing decision 8 "Compute is capital").

Read-only, fail-open, no-agent helper called from fleet-status-refresh.sh. Emits
a "## Compute pulse" markdown section appended to FLEET-STATUS.md (the phone/
governor-read fleet pulse) and writes a machine-checkable JSON for consumers.

Three queries on existing stores — no new apparatus (deliberately does NOT reuse
the D6 scorecard machinery of succession_maturity_metrics.py; red-team ruling):

  Q1 dispatch-share by lane  -> kanban.db task_runs (all boards, 30d), profile
                                classified into RESEARCH vs PROCESS/REVIEW lanes.
  Q2 token spend             -> per-profile state.db session_model_usage (30d):
                                input+output tokens + estimated_cost_usd.
  Q3 verified experiments    -> MULTIPLE-TESTING-LEDGER.md rows (the existing
                                research ledger; same parse pattern as
                                generate_truth_json.py q2_claimed_edges).

Metrics surfaced:
  - research dispatch-share % (research runs / total runs, fleet + sycode board),
    compared against the >=40% research-lane target (POR decision 8; experiment
    factory targets).
  - cost-per-verified-experiment = 30d token spend / ledger rows (the "cost
    divisor" the panel said nobody is shrinking); target falling. Also reported
    as ledger-rows-per-M-tokens (efficiency, rising).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BOARD_ROOT = Path("/home/frank/.hermes/kanban/boards")
PROFILE_ROOT = Path("/home/frank/.hermes/profiles")
LEDGER_PATHS = [
    Path("/home/frank/obsidian/sycode-trading/research/MULTIPLE-TESTING-LEDGER.md"),
    Path("/home/frank/obsidian/quant-team/research/MULTIPLE-TESTING-LEDGER.md"),
    Path("/home/frank/obsidian/quant-team/MULTIPLE-TESTING-LEDGER.md"),
]
JSON_OUT = Path(os.environ.get("FLEET_COMPUTE_PULSE_JSON", "/home/frank/uaa-rules/fleet-compute-pulse.json"))
RESEARCH_TARGET_PCT = 40.0
WINDOW_DAYS = 30

# Lane classification: RESEARCH = profiles whose primary output is research,
# hypotheses, experiments or analysis. Everything else (devops, reviewers, PMs,
# builders, integrators, ops, arena seats) is PROCESS/REVIEW. The >=40% research
# target is the share of DISPATCHED RUNS landing on the research lane.
RESEARCH_LANES = {
    "research-trading", "trading-data-oracle", "trading-market-analyst",
    "trading-strategy-dev", "trading-backtest-runner", "trading-ml-ensemble",
    "trading-trend-follower", "trading-mean-reversion", "trading-breakout-trader",
    "trading-volatility-arb", "research", "research-ai", "research-upero",
    "paper-analyst", "paper-trader", "paper-risk",
}

# Verdict buckets for the ledger (same taxonomy as generate_truth_json.py).
VERDICT_BUCKETS = {
    "dead": ["KILL", "REJECTED", "DISSOLVED", "NO_EDGE", "VOID", "NO CLAIM SURVIVES",
             "NOT AN EDGE", "NOT A TRADEABLE EDGE", "NO CURRENT", "FAIL-CLOSED"],
    "positive": ["SUPPORTED", "CONFIRM", "ESTABLISHED", "PROMISING"],
    "weak": ["WEAK"],
    "pending": ["PENDING", "ACCRUING", "REGISTERED", "SCORING BLOCKED", "ACCUMULATE"],
}
VERDICT_RE = re.compile(r"\*\*([^*]+?)\*\*")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


# ---------------------------------------------------------------------------
# Q1 — dispatch-share by lane (kanban.db task_runs)
# ---------------------------------------------------------------------------
def q1_dispatch_share() -> dict:
    since = int(datetime.now(timezone.utc).timestamp()) - WINDOW_DAYS * 86400
    out = {"source": "kanban.db task_runs (all boards, 30d)", "boards": {},
           "fleet": {"research_runs": 0, "process_runs": 0, "total_runs": 0,
                     "research_share_pct": None, "target_pct": RESEARCH_TARGET_PCT,
                     "target_met": None}}
    if not BOARD_ROOT.is_dir():
        out["error"] = f"board root missing: {BOARD_ROOT}"
        return out
    for db in sorted(BOARD_ROOT.glob("*/kanban.db")):
        board = db.parent.name
        try:
            with connect_ro(db) as conn:
                rows = conn.execute(
                    "SELECT profile, COUNT(*) FROM task_runs "
                    "WHERE started_at >= ? GROUP BY profile", (since,)
                ).fetchall()
        except Exception as e:  # noqa: BLE001
            out["boards"][board] = {"error": f"{type(e).__name__}: {e}"}
            continue
        b_res = b_proc = b_tot = 0
        lanes = {}
        for profile, cnt in rows:
            if not profile:
                continue
            cnt = int(cnt)
            b_tot += cnt
            lane = "research" if profile in RESEARCH_LANES else "process"
            lanes[profile] = {"runs": cnt, "lane": lane}
            if lane == "research":
                b_res += cnt
            else:
                b_proc += cnt
        share = round(100.0 * b_res / b_tot, 1) if b_tot else None
        out["boards"][board] = {
            "research_runs": b_res, "process_runs": b_proc, "total_runs": b_tot,
            "research_share_pct": share, "lanes": lanes,
        }
        out["fleet"]["research_runs"] += b_res
        out["fleet"]["process_runs"] += b_proc
        out["fleet"]["total_runs"] += b_tot
    tot = out["fleet"]["total_runs"]
    if tot:
        out["fleet"]["research_share_pct"] = round(
            100.0 * out["fleet"]["research_runs"] / tot, 1)
        out["fleet"]["target_met"] = out["fleet"]["research_share_pct"] >= RESEARCH_TARGET_PCT
    return out


# ---------------------------------------------------------------------------
# Q2 — token spend (per-profile state.db session_model_usage)
# ---------------------------------------------------------------------------
def q2_token_spend() -> dict:
    since = int(datetime.now(timezone.utc).timestamp()) - WINDOW_DAYS * 86400
    out = {"source": "per-profile state.db session_model_usage (30d)",
           "total_tokens": 0, "estimated_cost_usd": 0.0,
           "profiles_queried": 0, "profiles_with_usage": 0, "profiles": {}}
    if not PROFILE_ROOT.is_dir():
        out["error"] = f"profile root missing: {PROFILE_ROOT}"
        return out
    for db in sorted(PROFILE_ROOT.glob("*/state.db")):
        profile = db.parent.name
        out["profiles_queried"] += 1
        try:
            with connect_ro(db) as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(input_tokens+output_tokens),0),"
                    "       COALESCE(SUM(estimated_cost_usd),0) "
                    "FROM session_model_usage WHERE last_seen >= ?", (since,)
                ).fetchone()
        except Exception as e:  # noqa: BLE001
            out["profiles"][profile] = {"error": f"{type(e).__name__}: {e}"}
            continue
        tokens = int(row[0] or 0)
        cost = float(row[1] or 0.0)
        if tokens > 0:
            out["profiles_with_usage"] += 1
        out["profiles"][profile] = {"tokens": tokens, "estimated_cost_usd": round(cost, 2)}
        out["total_tokens"] += tokens
        out["estimated_cost_usd"] += cost
    out["estimated_cost_usd"] = round(out["estimated_cost_usd"], 2)
    return out


# ---------------------------------------------------------------------------
# Q3 — verified experiments (MULTIPLE-TESTING-LEDGER.md rows)
# ---------------------------------------------------------------------------
def _match_marker(text: str):
    up = text.upper()
    for bucket, markers in VERDICT_BUCKETS.items():
        for m in markers:
            if m in up:
                return bucket
    return None


def _classify_verdict(row_text: str) -> str:
    for tok in VERDICT_RE.findall(row_text):
        b = _match_marker(tok)
        if b:
            return b
    return _match_marker(row_text) or "other"


def q3_ledger_experiments() -> dict:
    out = {"source": "MULTIPLE-TESTING-LEDGER.md rows", "ledger_path": None,
           "data_rows": 0, "by_bucket": {}}
    path = next((p for p in LEDGER_PATHS if p.exists()), None)
    if path is None:
        out["error"] = "ledger not found"
        return out
    out["ledger_path"] = str(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    buckets = {"dead": 0, "positive": 0, "weak": 0, "pending": 0, "other": 0}
    data_rows = 0
    for ln in text.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        if not re.match(r"^\| *(\d+(?: \([^)]*\))?) *\|", s):
            continue
        data_rows += 1
        b = _classify_verdict(s)
        buckets[b] = buckets.get(b, 0) + 1
    out["data_rows"] = data_rows
    out["by_bucket"] = buckets
    return out


# ---------------------------------------------------------------------------
# Assemble + render
# ---------------------------------------------------------------------------
def main() -> int:
    q1 = q1_dispatch_share()
    q2 = q2_token_spend()
    q3 = q3_ledger_experiments()

    fleet = q1.get("fleet", {})
    tokens = int(q2.get("total_tokens", 0) or 0)
    cost = float(q2.get("estimated_cost_usd", 0.0) or 0.0)
    ledger_rows = int(q3.get("data_rows", 0) or 0)

    # cost-per-verified-experiment = tokens / ledger row (the "cost divisor");
    # target falling. Also efficiency = ledger rows per M tokens (rising).
    cost_per_experiment = round(tokens / ledger_rows) if (tokens and ledger_rows) else None
    rows_per_mtok = round(ledger_rows / (tokens / 1e6), 2) if (tokens and ledger_rows) else None

    payload = {
        "generated_at": now_iso(),
        "generator": "fleet_compute_pulse.py t_a737a025",
        "window_days": WINDOW_DAYS,
        "research_target_pct": RESEARCH_TARGET_PCT,
        "dispatch_share": q1,
        "token_spend": q2,
        "ledger": q3,
        "metrics": {
            "research_share_pct": fleet.get("research_share_pct"),
            "research_target_met": fleet.get("target_met"),
            "total_runs_30d": fleet.get("total_runs"),
            "research_runs_30d": fleet.get("research_runs"),
            "process_runs_30d": fleet.get("process_runs"),
            "tokens_30d": tokens,
            "estimated_cost_usd_30d": cost,
            "ledger_rows": ledger_rows,
            "cost_per_verified_experiment_tokens": cost_per_experiment,
            "ledger_rows_per_m_tokens": rows_per_mtok,
        },
    }
    try:
        JSON_OUT.write_text(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as e:  # noqa: BLE001
        payload["json_write_error"] = str(e)

    # ---- markdown section for FLEET-STATUS.md ----
    lines = ["", "## Compute pulse", f"- generated {now_iso()} | window {WINDOW_DAYS}d | source t_a737a025"]
    # dispatch-share
    if fleet.get("total_runs"):
        share = fleet["research_share_pct"]
        met = "MET" if fleet.get("target_met") else "MISS"
        lines.append(
            f"- dispatch-share: research {fleet['research_runs']}/{fleet['total_runs']} "
            f"({share}%) vs process/review {fleet['process_runs']} "
            f"| >=40% research target: {met}"
        )
        sycode = q1.get("boards", {}).get("sycode-trading", {})
        if sycode.get("total_runs"):
            lines.append(
                f"  - sycode-trading board: research {sycode.get('research_share_pct')}% "
                f"({sycode.get('research_runs')}/{sycode.get('total_runs')})"
            )
    else:
        lines.append("- dispatch-share: unknown (no run data)")
    # cost-per-verified-experiment
    lines.append(
        f"- token spend 30d: {tokens:,} tokens (est ${cost:,.2f})"
    )
    lines.append(
        f"- ledger rows (verified experiments): {ledger_rows}"
    )
    if cost_per_experiment is not None:
        lines.append(
            f"- cost-per-verified-experiment: {cost_per_experiment:,} tokens/row "
            f"| {rows_per_mtok} rows/M-tokens (target: cost falling, rows/token rising)"
        )
    else:
        lines.append("- cost-per-verified-experiment: N/A (need tokens and ledger rows)")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - must fail visibly, never silently
        print("FLEET_COMPUTE_PULSE_FAIL " + json.dumps(
            {"error": f"{type(exc).__name__}: {exc}"}, sort_keys=True), file=sys.stderr)
        raise
