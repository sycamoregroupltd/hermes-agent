#!/usr/bin/env python3
"""
Systematic Strategy Discovery Pipeline
=======================================
Auto-discovers winning pattern clusters from signal_fingerprints and
registers them as strategies in the strategies table.

Schedule: Every 7 days at 06:00 (configured via cron 'strategy-discovery-weekly')
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DB_HOST = "127.0.0.1"
DB_PORT = "5432"
DB_USER = "postgres"
DB_NAME = "postgres"
DB_PASSWORD = os.environ.get("POSTGRES_PASSWORD") or os.environ.get("PGPASSWORD") or "postgres"

DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000000"
DEFAULT_ENGINE = "custom"
DEFAULT_TRADING_MODE = "paper"
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
KANBAN_BOARD = os.environ.get("STRATEGY_DISCOVERY_KANBAN_BOARD", "sycode-trading")
REVIEW_ASSIGNEE = os.environ.get("STRATEGY_DISCOVERY_REVIEW_ASSIGNEE", "trading-risk-reviewer")
PARENT_TASK_ID = os.environ.get("STRATEGY_DISCOVERY_PARENT_TASK_ID", "t_98a7f2d6")

# Final_status values that map to "win" and "loss" in the actual DB
WIN_STATUSES = ("COMPLETED_WIN", "win")
LOSS_STATUSES = ("COMPLETED_LOSS", "loss")
ALL_CLOSED_STATUSES = WIN_STATUSES + LOSS_STATUSES

# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------
DISCOVERY_QUERY = """
SELECT
    trajectory_label,
    direction,
    timeframe,
    trigger_patterns->0->>'type' AS pattern,
    COUNT(*) AS samples,
    ROUND(AVG(pnl_percent)::numeric, 4) AS avg_pnl,
    ROUND(
        SUM(CASE WHEN final_status IN {win_statuses} THEN 1 ELSE 0 END)::numeric
        / NULLIF(COUNT(*), 0), 4
    ) AS win_rate,
    ROUND(AVG(mfe_mae_ratio)::numeric, 4) AS avg_mfe_mae
FROM signal_fingerprints
WHERE final_status IN {closed_statuses}
  AND triggered_at > now() - interval '30d'
GROUP BY trajectory_label, direction, timeframe, pattern
HAVING COUNT(*) >= 15
   AND ROUND(AVG(pnl_percent)::numeric, 4) > 0
   AND ROUND(
        SUM(CASE WHEN final_status IN {win_statuses} THEN 1 ELSE 0 END)::numeric
        / NULLIF(COUNT(*), 0), 4
   ) > 0.50
ORDER BY avg_pnl DESC;
""".format(
    win_statuses=repr(tuple(WIN_STATUSES)),
    closed_statuses=repr(tuple(ALL_CLOSED_STATUSES)),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_psql(query: str, parser: str = "discovery") -> List[Dict[str, Any]]:
    """Execute SQL and return list of dicts (column_name -> value)."""
    cmd = [
        "psql",
        "-h", DB_HOST,
        "-p", DB_PORT,
        "-U", DB_USER,
        "-d", DB_NAME,
        "-t",      # tuples only
        "-A",      # unaligned output
        "-F", "|", # pipe delimiter (safer than tab with JSON fields)
        "-c", query,
    ]
    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASSWORD
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60, env=env
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        raise RuntimeError(f"psql error (exit {result.returncode}): {err}")

    lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    rows = []
    for line in lines:
        parts = line.split("|")
        if not parts or len(parts) < 4:
            # Single-column or short-row response (e.g. EXISTS check)
            if parser == "exists":
                if parts and parts[0].strip() == "1":
                    return [{"exists": True}]
            continue
        rows.append(
            {
                "trajectory_label": parts[0].strip() or None,
                "direction": parts[1].strip(),
                "timeframe": parts[2].strip(),
                "pattern": parts[3].strip(),
                "samples": int(parts[4].strip()),
                "avg_pnl": float(parts[5].strip()),
                "win_rate": float(parts[6].strip()),
                "avg_mfe_mae": float(parts[7].strip()) if parts[7].strip() else 0.0,
            }
        )
    return rows


def run_psql_insert(query: str) -> str | None:
    """Execute an INSERT with RETURNING and return the id, or None if skipped."""
    cmd = [
        "psql",
        "-h", DB_HOST,
        "-p", DB_PORT,
        "-U", DB_USER,
        "-d", DB_NAME,
        "-t",
        "-A",
        "-F", "|",
        "-c", query,
    ]
    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASSWORD
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30, env=env
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        raise RuntimeError(f"psql insert error (exit {result.returncode}): {err}")
    out = result.stdout.strip()
    # psql returns "INSERT 0 0" command tag when ON CONFLICT DO NOTHING fires
    if not out or out.startswith("INSERT"):
        return None
    return out


def run_psql_scalar(query: str) -> str | None:
    """Execute a single-column SQL query and return the first non-empty line."""
    cmd = [
        "psql",
        "-h", DB_HOST,
        "-p", DB_PORT,
        "-U", DB_USER,
        "-d", DB_NAME,
        "-t",
        "-A",
        "-c", query,
    ]
    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASSWORD
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
    if result.returncode != 0:
        err = result.stderr.strip()
        raise RuntimeError(f"psql scalar error (exit {result.returncode}): {err}")
    for line in result.stdout.splitlines():
        line = line.strip()
        if line and not line.startswith("SELECT"):
            return line
    return None


def make_strategy_name(
    trajectory_label: str | None, pattern: str, direction: str, timeframe: str
) -> str:
    """
    Generate a human-readable, unique strategy name.
    E.g.: CLEAN_CONTINUATION_HIGHER_LOW_15m_LONG
    """
    parts = []
    if trajectory_label:
        # Normalise: replace non-alphanumeric with underscore, uppercase
        label = re.sub(r"[^a-zA-Z0-9]", "_", trajectory_label).upper().strip("_")
        parts.append(label)
    pat = re.sub(r"[^a-zA-Z0-9]", "_", pattern).upper().strip("_")
    parts.append(pat)
    parts.append(timeframe)
    parts.append(direction.upper())
    return "_".join(parts)


def build_signal_filter(
    pattern: str, direction: str, timeframe: str, trajectory_label: str | None
) -> dict:
    """Build a signal_filter JSONB for the strategies table."""
    filt = {
        "direction": direction.upper(),
        "timeframe": timeframe,
        "trajectory_label": trajectory_label,
        "pattern_type": pattern,
    }
    return filt


def strategy_exists(name: str) -> bool:
    """Check if a strategy with this name already exists (unique per user)."""
    safe_name = name.replace("'", "''")
    query = (
        f"SELECT 1 FROM strategies "
        f"WHERE name = '{safe_name}' "
        f"AND user_id = '{DEFAULT_USER_ID}'::uuid "
        f"LIMIT 1"
    )
    try:
        rows = run_psql(query, parser="exists")
        return len(rows) > 0 and rows[0].get("exists", False)
    except (RuntimeError, IndexError):
        return False


def get_existing_strategy_id(name: str) -> str | None:
    """Return existing strategy id by name without modifying it."""
    safe_name = name.replace("'", "''")
    query = (
        f"SELECT id::text FROM strategies "
        f"WHERE name = '{safe_name}' "
        f"AND user_id = '{DEFAULT_USER_ID}'::uuid "
        f"LIMIT 1"
    )
    return run_psql_scalar(query)


def register_strategy(
    name: str,
    pattern_info: dict,
) -> str | None:
    """Insert a new quarantined strategy, returning its id. Never enables or modifies existing."""
    if strategy_exists(name):
        return None  # Already exists — skip

    safe_name = name.replace("'", "''")
    description = (
        f"Auto-discovered from signal_fingerprints: "
        f"{pattern_info['pattern']} | trajectory={pattern_info['trajectory_label']}, "
        f"dir={pattern_info['direction']}, tf={pattern_info['timeframe']}, "
        f"samples={pattern_info['samples']}, "
        f"avg_pnl={pattern_info['avg_pnl']}, win_rate={pattern_info['win_rate']}"
    ).replace("'", "''")

    signal_filter = build_signal_filter(
        pattern=pattern_info["pattern"],
        direction=pattern_info["direction"],
        timeframe=pattern_info["timeframe"],
        trajectory_label=pattern_info["trajectory_label"],
    )
    signal_filter_json = json.dumps(signal_filter).replace("'", "''")

    meta = json.dumps(
        {
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "avg_mfe_mae": pattern_info["avg_mfe_mae"],
            "trajectory_label": pattern_info["trajectory_label"],
            "source": "strategy_discovery_pipeline",
            "quarantine_status": "pending_risk_review",
            "quarantine_parent_task": PARENT_TASK_ID,
        }
    ).replace("'", "''")

    query = f"""
    INSERT INTO strategies
        (user_id, name, description, engine, enabled, signal_filter,
         risk_profile, exit_guidelines, meta, trading_mode,
         total_trades, winning_trades, total_pnl)
    VALUES
        ('{DEFAULT_USER_ID}'::uuid,
         '{safe_name}',
         '{description}',
         '{DEFAULT_ENGINE}',
         false,
         '{signal_filter_json}'::jsonb,
         '{{}}'::jsonb,
         '{{}}'::jsonb,
         '{meta}'::jsonb,
         '{DEFAULT_TRADING_MODE}',
         0, 0, 0)
    ON CONFLICT (user_id, name) DO NOTHING
    RETURNING id;
    """
    return run_psql_insert(query)


def review_idempotency_key(name: str) -> str:
    """Stable key so each candidate maps to one risk-review card."""
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-").lower()
    return f"strategy-discovery-review-{slug[:120]}"


def create_review_card(name: str, strategy_id: str, pattern_info: dict) -> str | None:
    """File an idempotent independent review card for a disabled candidate."""
    body = f"""PAPER-ONLY independent quarantine-boundary review.

Source pipeline: /home/frank/.hermes/scripts/strategy_discovery.py
Parent quarantine task: {PARENT_TASK_ID}
Candidate strategy id: {strategy_id}
Candidate name: {name}

The weekly strategy-discovery pipeline mined this candidate from signal_fingerprints but now inserts it with enabled=false. Review must decide whether this candidate can ever enter an explicit approved allowlist/apply packet. Do not enable or apply during review.

Pattern evidence from pipeline query:
- trajectory_label: {pattern_info['trajectory_label']}
- direction: {pattern_info['direction']}
- timeframe: {pattern_info['timeframe']}
- pattern: {pattern_info['pattern']}
- samples: {pattern_info['samples']}
- avg_pnl: {pattern_info['avg_pnl']}
- win_rate: {pattern_info['win_rate']}
- avg_mfe_mae: {pattern_info['avg_mfe_mae']}

Acceptance: REVIEW_VERDICT states APPROVED_FOR_ALLOWLIST, REJECTED_REFUTED_DATA, or NEEDS_MORE_CLEAN_DATA with evidence. Paper-only boundary throughout.
"""
    cmd = [
        HERMES_BIN,
        "kanban",
        "--board",
        KANBAN_BOARD,
        "create",
        f"Risk review: weekly strategy-discovery candidate {name}",
        "--assignee",
        REVIEW_ASSIGNEE,
        "--parent",
        PARENT_TASK_ID,
        "--priority",
        "70",
        "--idempotency-key",
        review_idempotency_key(name),
        "--body",
        body,
        "--json",
    ]
    env = os.environ.copy()
    env.setdefault("HERMES_KANBAN_BOARD", KANBAN_BOARD)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    if result.returncode != 0:
        print(
            f"[strategy_discovery] ERROR: Failed to create review card for {name}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    try:
        payload = json.loads(result.stdout.strip())
        return payload.get("id") or payload.get("task_id") or result.stdout.strip()
    except json.JSONDecodeError:
        return result.stdout.strip()


def print_report(
    candidates: List[Dict[str, Any]],
    registered: List[Tuple[str, str]],
    review_cards: List[Tuple[str, str]],
) -> None:
    """Print a structured discovery report to stdout."""
    print("=" * 72)
    print("  STRATEGY DISCOVERY REPORT")
    print(f"  Generated: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 72)
    print()
    print(f"  Candidate patterns found:  {len(candidates)}")
    print(f"  New strategies registered: {len(registered)} (all enabled=false)")
    print(f"  Review cards filed:        {len(review_cards)}")
    print()

    if candidates:
        # Top 3 by avg_pnl
        top3 = sorted(candidates, key=lambda c: c["avg_pnl"], reverse=True)[:3]
        print("  ── Top 3 Patterns by avg_pnl ──")
        print()
        for i, c in enumerate(top3, 1):
            print(f"    #{i}: [{c['direction']}] {c['pattern']} on {c['timeframe']}")
            print(f"        Trajectory:  {c['trajectory_label'] or 'N/A'}")
            print(f"        Samples:     {c['samples']}")
            print(f"        Avg PnL:     {c['avg_pnl']:+.4f}%")
            print(f"        Win Rate:    {c['win_rate']:.2%}")
            print(f"        MFE/MAE:     {c['avg_mfe_mae']:.2f}")
            print()

    if registered:
        print("  ── Newly Registered Quarantined Strategies ──")
        print()
        for name, sid in registered:
            print(f"    {name}")
            print(f"    ID: {sid}")
            print()

    if review_cards:
        print("  ── Review Cards ──")
        print()
        for name, card_id in review_cards:
            print(f"    {name}")
            print(f"    Review: {card_id}")
            print()

    print("─" * 72)
    print("  Note: Existing strategies were never modified; new strategies are disabled pending risk review.")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------


def main() -> int:
    print("[strategy_discovery] Phase 1: Querying signal_fingerprints...")
    try:
        candidates = run_psql(DISCOVERY_QUERY)
    except RuntimeError as e:
        print(f"[strategy_discovery] ERROR: Query failed: {e}", file=sys.stderr)
        return 1

    print(f"[strategy_discovery] Found {len(candidates)} candidate patterns.")
    if not candidates:
        print("[strategy_discovery] No candidates met the threshold. Nothing to register.")
        print_report([], [], [])
        return 0

    print("[strategy_discovery] Phase 2: Registering new quarantined strategies...")
    registered: List[Tuple[str, str]] = []
    review_cards: List[Tuple[str, str]] = []
    skipped = 0

    for c in candidates:
        name = make_strategy_name(
            trajectory_label=c["trajectory_label"],
            pattern=c["pattern"],
            direction=c["direction"],
            timeframe=c["timeframe"],
        )
        sid = register_strategy(name, c)
        if sid:
            sid_clean = sid.strip()
            registered.append((name, sid_clean))
            print(f"  [+] Registered disabled: {name} (id={sid_clean})")
            review_id = create_review_card(name, sid_clean, c)
            if review_id:
                review_cards.append((name, review_id))
                print(f"      Review card: {review_id}")
        else:
            skipped += 1
            existing_id = get_existing_strategy_id(name)
            print(f"  [=] Existing quarantined candidate: {name} (id={existing_id or 'unknown'})")
            if existing_id:
                review_id = create_review_card(name, existing_id, c)
                if review_id:
                    review_cards.append((name, review_id))
                    print(f"      Review card: {review_id}")

    print(
        f"[strategy_discovery] Registered disabled {len(registered)}, "
        f"review cards {len(review_cards)}, skipped {skipped}."
    )
    if len(review_cards) != len(candidates):
        print(
            "[strategy_discovery] ERROR: At least one candidate lacks a review card.",
            file=sys.stderr,
        )
        print_report(candidates, registered, review_cards)
        return 1
    print_report(candidates, registered, review_cards)
    return 0


if __name__ == "__main__":
    sys.exit(main())
