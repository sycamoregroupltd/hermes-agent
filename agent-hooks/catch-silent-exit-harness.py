#!/usr/bin/env python3
"""Regression harness for catch-silent-exit.sh no-signal exits.

The harness is intentionally narrow: it feeds fixture payloads directly to the
subagent_stop hook and asserts the desired terminal-signal contract without
reading or mutating live kanban boards, provider config, credentials, or network
state. Fixtures use the real agent/shell_hooks.py _serialize_payload wire shape
(hook_event_name + extra.child_status), not a synthetic top-level shape.

The primary regression models a worker that exits rc=0 with
extra.child_status=completed but did not call kanban_complete or kanban_block.

On current behavior this harness must fail: catch-silent-exit.sh treats
"completed" as a clean terminal state and emits {} even when the payload lacks a
terminal kanban signal.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


CLEAN_DECISION = "allow"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hook",
        default=str(Path(__file__).with_name("catch-silent-exit.sh")),
        help="Path to catch-silent-exit.sh under test.",
    )
    parser.add_argument(
        "--fixtures",
        default=str(Path(__file__).with_name("catch-silent-exit.fixtures.json")),
        help="JSON fixture corpus.",
    )
    return parser.parse_args()


def run_hook(hook: str, payload: dict[str, Any]) -> tuple[int, str, str, dict[str, Any]]:
    proc = subprocess.run(
        ["bash", hook],
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    try:
        parsed = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        parsed = {"_parse_error": proc.stdout}
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip(), parsed


def observed_decision(parsed: dict[str, Any]) -> str:
    decision = parsed.get("decision")
    if decision:
        return str(decision)
    return CLEAN_DECISION


def assert_fixture(hook: str, fixture: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    rc, stdout, stderr, parsed = run_hook(hook, fixture["payload"])
    expect = fixture["expect"]
    decision = observed_decision(parsed)

    if rc != 0:
        errors.append(f"hook exited {rc}; stderr={stderr!r}; stdout={stdout!r}")
    if decision != expect["decision"]:
        extra = fixture["payload"].get("extra", {})
        errors.append(
            f"decision: expected {expect['decision']!r}, got {decision!r}; "
            f"stdout={stdout!r}; "
            f"extra.child_status={extra.get('child_status')!r}; "
            f"extra.child_summary={extra.get('child_summary','')[:60]!r}"
        )
    reason_contains = expect.get("reason_contains")
    if reason_contains and reason_contains not in str(parsed.get("reason", "")):
        errors.append(f"reason missing {reason_contains!r}; parsed={parsed!r}")
    if expect.get("forbid_clean_allow") and decision == CLEAN_DECISION:
        extra = fixture["payload"].get("extra", {})
        errors.append(
            "no useful terminal signal: hook emitted clean allow ({}) for "
            f"extra.child_status={extra.get('child_status')!r} payload "
            "with no kanban_complete/kanban_block evidence"
        )
    return errors


def main() -> int:
    args = parse_args()
    fixtures = json.loads(Path(args.fixtures).read_text(encoding="utf-8"))
    failures: list[str] = []
    for fixture in fixtures:
        errors = assert_fixture(args.hook, fixture)
        if errors:
            failures.append(f"FAIL {fixture['name']}: " + "; ".join(errors))
        else:
            print(f"PASS {fixture['name']}")

    if failures:
        print("catch-silent-exit regression harness FAIL", file=sys.stderr)
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print("catch-silent-exit regression harness PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
