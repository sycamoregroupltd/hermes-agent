#!/usr/bin/env python3
"""Dry-run report for stale provider-capacity kanban blockers.

Read-only by design: this script only inspects SQLite board state and prints
JSON/text findings.  It never edits task rows, provider/model/fallback routing,
credentials, cron routing, or guardrails.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli.kanban_stale_provider_gates import (  # noqa: E402
    findings_to_json,
    scan_all_boards,
)


def _default_home() -> Path:
    return Path.home() / ".hermes"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only stale provider-capacity kanban blocker report",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=_default_home(),
        help="Hermes root containing kanban.db / kanban/boards (default: ~/.hermes)",
    )
    parser.add_argument(
        "--board",
        action="append",
        dest="boards",
        help="Board slug to scan; repeat to scan multiple. Default: discover all boards.",
    )
    parser.add_argument(
        "--reset-window-hours",
        type=float,
        default=24.0,
        help="Minimum age before a capacity blocker is considered stale (default: 24h)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit full JSON findings instead of the compact text report.",
    )
    args = parser.parse_args(argv)

    findings = scan_all_boards(
        args.home,
        boards=args.boards,
        reset_window_seconds=int(args.reset_window_hours * 3600),
    )
    if args.json:
        print(findings_to_json(findings))
        return 0

    stale = [f for f in findings if f.stale]
    active_auth = [f for f in findings if f.classification == "active_credential_auth_failure"]
    print(
        "stale_provider_gate_report "
        f"findings={len(findings)} stale_capacity={len(stale)} "
        f"active_auth={len(active_auth)} reset_window_hours={args.reset_window_hours:g}"
    )
    for finding in findings:
        status = "STALE_CAPACITY" if finding.stale else finding.classification.upper()
        print(
            f"- {status} {finding.board}/{finding.task_id} "
            f"assignee={finding.assignee or '-'} age_h={finding.age_seconds / 3600:.1f}: "
            f"{finding.reason}"
        )
        for success in finding.same_profile_successes[:3]:
            print(f"  success: {success.task_id} completed_at={success.completed_at} {success.title[:80]}")
        for dep in finding.dependents[:5]:
            gate = "eligible" if dep.eligible else "not-eligible"
            parents = ",".join(dep.open_parent_ids) if dep.open_parent_ids else "none"
            print(
                f"  dependent: {dep.board}/{dep.task_id} {gate} "
                f"safe_read_only={dep.safe_read_only_signal} open_parents={parents}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
