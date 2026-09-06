#!/usr/bin/env python3
"""Fail-closed disposition labels for clean strategy-funnel arms."""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
COHORT_RE = re.compile(r"^(?:LONG|SHORT)_[0-9]+[mhd]$", re.IGNORECASE)
CONTROL_ARM = "random_entry_control"


def _retired(state: dict[str, Any]) -> bool:
    meta = state.get("meta") or {}
    disposition = str(state.get("disposition") or meta.get("disposition") or "").lower()
    return (
        state.get("enabled") is not True
        or str(state.get("trading_mode") or "").lower() != "paper"
        or any(state.get(key) or meta.get(key) for key in ("quarantined_at", "paperDisabledAt", "retired_at"))
        or disposition in {"retired", "quarantined"}
    )


def classify_arm(arm: dict[str, Any], states: dict[str, dict[str, Any]] | None, target: int = 300, lookup_error: bool = False) -> dict[str, Any]:
    arm_id = str(arm["arm_id"])
    n = int(arm["n"])
    remaining = max(target - n, 0)
    result = {"arm_id": arm_id, "n": n, "target": target, "remaining": remaining}

    if lookup_error:
        result.update(disposition="UNKNOWN_BLOCKED", status="UNKNOWN_BLOCKED")
    elif arm_id == CONTROL_ARM:
        result.update(disposition="CONTROL_ONLY_NON_PROMOTABLE", status="CONTROL_ONLY_NON_PROMOTABLE")
    elif COHORT_RE.fullmatch(arm_id):
        result.update(
            disposition="COHORT_ONLY",
            status="READY_FOR_EVALUATION" if n >= target else "COLLECT_MORE",
        )
    elif UUID_RE.fullmatch(arm_id):
        state = (states or {}).get(arm_id)
        if state is None:
            result.update(disposition="UNKNOWN_BLOCKED", status="UNKNOWN_BLOCKED")
        elif _retired(state):
            result.update(disposition="RETIRED_NON_PROMOTABLE", status="RETIRED_NON_PROMOTABLE")
        else:
            result.update(
                disposition="ACTIVE_CANDIDATE",
                status="READY_FOR_EVALUATION" if n >= target else "COLLECT_MORE",
            )
    else:
        result.update(disposition="UNKNOWN_BLOCKED", status="UNKNOWN_BLOCKED")
    return result


def render(results: list[dict[str, Any]]) -> str:
    lines = []
    for item in results:
        lines.append(
            f"- {item['arm_id']}: {item['n']} / {item['target']} clean outcomes; "
            f"remaining={item['remaining']}; disposition={item['disposition']}; status={item['status']}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", help="JSON fixture containing arms, strategies, and optional lookup_error")
    parser.add_argument("--target", type=int, default=300)
    args = parser.parse_args()
    payload = json.load(open(args.fixture, encoding="utf-8")) if args.fixture else json.load(sys.stdin)
    states = payload.get("strategies")
    results = [
        classify_arm(arm, states, args.target, bool(payload.get("lookup_error")))
        for arm in payload.get("arms", [])
    ]
    output = {"arms": results, "quality_statement": quality_statement()}
    print(json.dumps(output, sort_keys=True))
    return 0


def quality_statement() -> str:
    return (
        "Sample threshold alone does not satisfy promotionQuality; any candidate still requires "
        "net-of-fee, leak-free signal-time, OOS/temporal-stability, and independent risk review."
    )


if __name__ == "__main__":
    raise SystemExit(main())
