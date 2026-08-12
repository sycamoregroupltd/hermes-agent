#!/usr/bin/env python3
"""STAGED exact-host paper-risk WakeAgent candidate; not installed.

Runtime target: /home/frank/.hermes/profiles/jarvis/scripts/paper_risk_gate.py
The structured calibration gate is surfaced deterministically and an active or
unknown gate wakes risk review even when no new close exists. Enforcement does
not depend on an LLM interpreting prose.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

SOURCE_ROOT = os.environ.get("SIGNAL_FUSION_SOURCE_ROOT", "/home/frank/sycode-trading")
if SOURCE_ROOT not in sys.path:
    sys.path.insert(0, SOURCE_ROOT)

# Re-exported seam from fusion_calibration_gate — the canonical fail-closed gate.
# No separate calibration_verdict module exists; use the actual implementation.
from execution.fusion_calibration_gate import (
    load_calibration_gate as _load_gate,
    DEFAULT_HIGH_CONVICTION_MIN as HIGH_CONVICTION_FLOOR,
)

PGPASSWORD = os.environ.get("POSTGRES_PASSWORD") or os.environ.get("PGPASSWORD") or ""
DB = [
    "docker", "exec", "-e", f"PGPASSWORD={PGPASSWORD}",
    "sycodetrading-supabase-db", "psql", "-U", "postgres", "-d", "postgres",
    "-t", "-A",
]
STATE_FILE = os.environ.get(
    "PAPER_RISK_STATE_FILE",
    "/home/frank/.hermes/profiles/jarvis/cron/state/paper_risk_last.txt",
)


def db(sql: str) -> str:
    result = subprocess.run(DB + ["-c", sql], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        # Table/view not found — treat as empty (0 results) for safety.
        stderr_lower = result.stderr.strip().lower()
        if any(tok in stderr_lower for tok in (
            "undefined_table",
            "does not exist",
            "relation",  # catches PostgreSQL "relation X does not exist"
        )):
            return ""
        raise RuntimeError(f"DB query failed: {result.stderr.strip()}")
    return result.stdout.strip()


def build_calibration_gate_context(gate_decision) -> dict:
    """Map CalibrationGateDecision into the gate-context contract."""
    validation_status = gate_decision.status
    reason_codes = gate_decision.reasons
    blocks = (validation_status != "VALIDATED")
    provenance = {
        "quant_path": gate_decision.verdict.quant_report_path,
        "f052_path": gate_decision.verdict.f052_report_path,
        "tier1_win_rate_pct": gate_decision.verdict.tier1_win_rate_pct,
        "weighted_mce_pp": gate_decision.verdict.weighted_mce_pp,
        "validated_edge_status": gate_decision.verdict.validated_edge_status,
    }
    return {
        "validation_status": validation_status,
        "reason_codes": reason_codes,
        "blocks_high_conviction_paper_opens": blocks,
        "provenance": {k: v for k, v in provenance.items() if v is not None},
    }


def should_wake(
    last_seen: str,
    latest_close: str,
    gate_decision,
) -> bool:
    gate = build_calibration_gate_context(gate_decision)
    return latest_close != last_seen or gate["blocks_high_conviction_paper_opens"]


def main() -> int:
    dry_run = os.environ.get("PAPER_RISK_GATE_DRY_RUN", "").lower() in {
        "1", "true", "yes",
    }
    gate_decision = _load_gate()
    latest_close = db(
        "SELECT max(closed_at)::text FROM managed_positions "
        "WHERE closed_at IS NOT NULL;"
    )
    last_seen = ""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as handle:
            last_seen = handle.read().strip()
    if not should_wake(last_seen, latest_close, gate_decision):
        return 0

    if not dry_run:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as handle:
            handle.write(latest_close or "none")

    # Individual queries so a missing table only zeroes its metric instead of
    # swallowing every other data point.
    def _sql_default(sql: str, fallback: str = "0") -> str:
        r = db(sql)
        return r if r else fallback

    open_positions = int(_sql_default(
        "SELECT count(*) FROM managed_positions WHERE status = 'open';"))
    drawdown_24h = float(_sql_default(
        "SELECT COALESCE(round(sum(realized_pnl)::numeric, 2), 0) "
        "FROM managed_positions WHERE closed_at >= now() - interval '24 hours' "
        "AND realized_pnl < 0;"))
    largest_loss_24h = float(_sql_default(
        "SELECT COALESCE(round(min(realized_pnl)::numeric, 2), 0) "
        "FROM managed_positions WHERE closed_at >= now() - interval '24 hours' "
        "AND realized_pnl IS NOT NULL;"))
    losing_24h_raw = db(
        "SELECT json_agg(sub) FROM ("
        "SELECT strategy_name, round(sum(realized_pnl)::numeric,2) as pnl, "
        "count(*) as trades FROM managed_positions WHERE closed_at >= now() - interval '24 hours' "
        "AND realized_pnl < 0 GROUP BY strategy_name ORDER BY sum(realized_pnl) ASC LIMIT 3) sub;")
    losing_strategies_24h = json.loads(losing_24h_raw) if losing_24h_raw else []
    stale_jarvis_positions = int(_sql_default(
        "SELECT count(*) FROM jarvis_positions WHERE status = 'open' "
        "AND open_time < extract(epoch from now() - interval '4 hours')::bigint * 1000;",
        fallback="0"))
    critical_risk_events = int(_sql_default(
        "SELECT count(*) FROM managed_positions WHERE closed_at >= now() - interval '1 hour' "
        "AND realized_pnl < -5;"))

    risk_context: dict[str, Any] = {
        "open_positions": open_positions,
        "drawdown_24h": drawdown_24h,
        "largest_loss_24h": largest_loss_24h,
        "losing_strategies_24h": losing_strategies_24h,
        "stale_jarvis_positions": stale_jarvis_positions,
        "critical_risk_events": critical_risk_events,
    }
    risk_context["calibration_gate"] = build_calibration_gate_context(gate_decision)
    print("=== WAKEAGENT: Paper Risk Context ===")
    print(json.dumps(risk_context, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
