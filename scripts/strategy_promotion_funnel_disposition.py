#!/usr/bin/env python3
"""Fail-closed disposition labels for clean strategy-funnel arms."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
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


def _control_only(state: dict[str, Any]) -> bool:
    meta = state.get("meta") or {}
    for key in ("name", "engine"):
        value = state.get(key) or meta.get(key) or ""
        normalized = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
        if normalized in {"random_entry", "random_entry_control"}:
            return True
    return False


def _pace_note(arm: dict[str, Any], n: int, target: int, remaining: int) -> str:
    if n >= target:
        return ""
    if n < 2:
        return "ETA unavailable: need at least 2 resolved clean outcomes to estimate pace"
    try:
        first = datetime.fromisoformat(str(arm["first_signal_time"]).replace("Z", "+00:00"))
        last = datetime.fromisoformat(str(arm["last_signal_time"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return "ETA unavailable: clean outcome timestamps unavailable"
    observed_days = (last - first).total_seconds() / 86400.0
    if observed_days <= 0:
        return "ETA unavailable: clean outcomes share the same signal_time"
    pace = n / observed_days
    eta = datetime.now(timezone.utc) + timedelta(days=remaining / pace)
    return f"ETA {eta:%Y-%m-%d} at {pace:.2f}/day observed pace"


def classify_arm(arm: dict[str, Any], states: dict[str, dict[str, Any]] | None, target: int = 300, lookup_error: bool = False) -> dict[str, Any]:
    arm_id = str(arm["arm_id"])
    n = int(arm["n"])
    remaining = max(target - n, 0)
    result = {"arm_id": arm_id, "n": n, "target": target, "remaining": remaining}
    if "first_signal_time" in arm:
        result["first_signal_time"] = arm["first_signal_time"]
    if "last_signal_time" in arm:
        result["last_signal_time"] = arm["last_signal_time"]
    result["eta"] = _pace_note(arm, n, target, remaining)

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
        elif _control_only(state):
            result.update(disposition="CONTROL_ONLY_NON_PROMOTABLE", status="CONTROL_ONLY_NON_PROMOTABLE")
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
            f"remaining={item['remaining']}"
            f"{'; ' + item['eta'] if item.get('eta') else ''}; "
            f"disposition={item['disposition']}; status={item['status']}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", help="JSON fixture containing arms, strategies, and optional lookup_error")
    parser.add_argument("--target", type=int, default=300)
    args = parser.parse_args()
    if args.fixture:
        with open(args.fixture, encoding="utf-8") as fixture_file:
            payload = json.load(fixture_file)
    else:
        payload = json.load(sys.stdin)
    states = payload.get("strategies")
    results = [
        classify_arm(arm, states, args.target, bool(payload.get("lookup_error")))
        for arm in payload.get("arms", [])
    ]
    output = {"arms": results, "quality_statement": quality_statement(), "markdown": render(results)}
    print(json.dumps(output, sort_keys=True))
    return 0


def quality_statement() -> str:
    return (
        "Sample threshold alone does not satisfy promotionQuality; any candidate still requires "
        "net-of-fee, leak-free signal-time, OOS/temporal-stability, and independent risk review."
    )


if __name__ == "__main__":
    raise SystemExit(main())
