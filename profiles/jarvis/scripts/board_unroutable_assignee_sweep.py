#!/usr/bin/env python3
"""board_unroutable_assignee_sweep.py — daily board-wide phantom-assignee watchdog.

Wired for t_97819b7d (child of t_c17d0998). Canonical rules live in
validate_assignees.py (kanban-orchestrator skill); this wrapper is the cron
face: it runs the `--board-rows` sweep across EVERY board under
~/.hermes/kanban/boards/*/kanban.db and alerts only when a dispatchable row
(ready/todo/triage/scheduled) is assigned to a PHANTOM profile:

  - not a real on-disk profile under ~/.hermes/profiles/, AND
  - not a recognized terminal lane (fable/codex/grok/operator, orion-*/codex-*/
    external-*), AND
  - has ZERO rows in task_runs (never ran = silent dispatcher drop, not a
    proven-live seat).

Distinguishes PHANTOM (the real failure — silent drop, the exact class that
stranded 5 cards on 'reviewer' for weeks) from BENIGN (named lane with run
history / recognized terminal lane) so the daily sweep is NOT noisy.

WATCHDOG PATTERN (no_agent cron):
  - Green  (0 phantoms): prints NOTHING. Empty stdout = silent (cron sends
    nothing). Also exits 0.
  - Red    (>=1 phantom): prints the human report to stdout AND self-delivers
    to discord:#fleet-reports via `hermes send` (verified path — the
    cron-layer `deliver: discord` forwarding is fleet-wide broken, same
    rationale as nonspawnable-fleet-alert-guard.py / deadpid-fleet-alert-guard.py).

Read-only by contract: this script NEVER mutates any board. It reports; the
operator (or a remediation card) reassigns the stranded rows.

DELIVERY TARGET / PROFILE are overridable via env for dry runs:
  BOARD_UNROUTABLE_SWEEP_DRY_RUN=1  -> print report only, no send, exit code
                                       reflects findings (1=phantoms)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

VALIDATE_BIN = os.environ.get(
    "BOARD_UNROUTABLE_VALIDATE",
    "/home/frank/.hermes/skills/kanban-orchestrator/scripts/validate_assignees.py",
)
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
HERMES_PROFILE = os.environ.get("BOARD_UNROUTABLE_SWEEP_PROFILE", "jarvis")
TARGET = os.environ.get("BOARD_UNROUTABLE_SWEEP_TARGET", "discord:#fleet-reports")
SUBJECT = "[board-unroutable-assignee-sweep]"
DRY_RUN = os.environ.get("BOARD_UNROUTABLE_SWEEP_DRY_RUN", "0") in ("1", "true", "True")

SWEEP_ARGS = [sys.executable, VALIDATE_BIN, "--board-rows", "--board-json"]


def _run_sweep() -> dict:
    proc = subprocess.run(
        SWEEP_ARGS,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode not in (0, 1):
        # Sweep itself failed (schema drift, missing python, etc.) — that is
        # itself a reportable event, not silence.
        raise RuntimeError(
            f"sweep failed rc={proc.returncode}: {(proc.stderr or proc.stdout)[:500]}"
        )
    return json.loads(proc.stdout)


def _human_report(report: dict) -> str:
    s = report.get("summary", {})
    phantoms = report.get("phantoms", [])
    lines = [
        "BOARD-WIDE UNROUTABLE-ASSIGNEE SWEEP (t_97819b7d)",
        f"  boards scanned      : {s.get('boards_scanned')}",
        f"  dispatchable rows   : {s.get('total_dispatchable')}",
        f"  benign              : {s.get('benign')}",
        f"  unassigned          : {s.get('unassigned')}",
        f"  PHANTOM (silent-drop): {s.get('phantoms')}",
        "  --- phantoms ---",
    ]
    for p in phantoms:
        lines.append(
            f"    [{p['board']}] {p['id']} assignee={p['assignee']} "
            f"status={p['status']} pri={p['priority']}"
        )
    lines.append(
        "  FIX: reassign these rows to a real on-disk profile (or register the "
        "lane) — they are silently dropped by the dispatcher."
    )
    return "\n".join(lines)


def _deliver(body: str) -> None:
    """Deliver the alert to #fleet-reports via `hermes send` (verified path).

    A delivery outage must not crash the watchdog or suppress the local
    signal — log loudly to stderr and continue.
    """
    import shutil

    if not shutil.which(HERMES_BIN) and not os.path.exists(HERMES_BIN):
        print(f"{SUBJECT} HERMES_BIN not found, cannot deliver", file=sys.stderr)
        return
    try:
        proc = subprocess.run(
            [HERMES_BIN, "-p", HERMES_PROFILE, "send", "-q", "-t", TARGET,
             "-s", SUBJECT, body],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "")[:300]
            print(f"{SUBJECT} hermes send FAILED rc={proc.returncode}: {detail}",
                  file=sys.stderr)
    except Exception as ex:  # pragma: no cover
        print(f"{SUBJECT} hermes send exception: {ex}", file=sys.stderr)


def main() -> int:
    try:
        report = _run_sweep()
    except Exception as ex:
        # A broken monitor must not fail silently — surface it.
        print(f"{SUBJECT} SWEEP ERROR: {ex}", file=sys.stderr)
        return 2

    phantoms = report.get("phantoms", [])
    if not phantoms:
        # Green: empty stdout = silent (no_agent watchdog contract).
        return 0

    body = _human_report(report)
    print(body)
    if not DRY_RUN:
        _deliver(body)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
