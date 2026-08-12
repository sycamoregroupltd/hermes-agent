#!/usr/bin/env python3
"""No-agent cron wrapper for kanban diagnostics and failure classification.

Rewritten 2026-07-23: the previous version depended on
kanban_failure_classifier_cron which was missing (file not found). Using
native `hermes kanban diagnostics` CLI instead — more reliable, same output.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone


def run_board_diagnostics(board: str) -> dict | None:
    """Run `hermes kanban diagnostics --json` on a board."""
    try:
        proc = subprocess.run(
            ["hermes", "kanban", "--board", board, "diagnostics", "--json"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def _manifest_boards(flag: str, fallback: list[str]) -> list[str]:
    """Board list is DATA (t_911a916c) — read the fleet boards manifest."""
    sys.path.insert(0, "/home/frank/.hermes/scripts")
    try:
        from fleet_boards import boards_for  # type: ignore

        return list(boards_for(flag))
    except Exception:
        return fallback


def main() -> int:
    boards = _manifest_boards(
        "triage", ["jarvis-os", "sycode-trading", "sycode-ai", "upero", "yorkstone-supplies"]
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    total_diags = 0
    report_lines = [f"# Kanban Diagnostics — {now}", ""]

    for board in boards:
        diags = run_board_diagnostics(board)
        if diags is None:
            report_lines.append(f"## {board}\n\n  ⚠️  diagnostics unavailable or timed out\n")
            continue

        if isinstance(diags, list):
            count = len(diags)
        elif isinstance(diags, dict):
            count = len(diags.get("diagnostics", diags.get("results", [])))
        else:
            count = 0

        total_diags += count

        if count == 0:
            report_lines.append(f"## {board}\n\n  ✅ No diagnostics\n")
        else:
            report_lines.append(f"## {board}\n\n  ⚠️  {count} diagnostic(s):\n")
            items = diags if isinstance(diags, list) else diags.get("diagnostics", diags.get("results", []))
            for d in items[:10]:  # Show top 10
                if isinstance(d, dict):
                    task = d.get("task", d.get("task_id", "?"))
                    severity = d.get("severity", "?")
                    msg = d.get("message", d.get("diagnostic", ""))[:120]
                    report_lines.append(f"  - [{severity}] task={task}: {msg}")
                else:
                    report_lines.append(f"  - {str(d)[:120]}")
            if count > 10:
                report_lines.append(f"  ... and {count - 10} more")

    report_lines.append("")
    report_lines.append(f"Total diagnostics across {len(boards)} boards: {total_diags}")

    report = "\n".join(report_lines)
    print(report)

    # If nothing to report, stay silent
    if total_diags == 0:
        return 0

    # If there are diagnostics, they'll be delivered by the cron
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
