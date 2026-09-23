#!/usr/bin/env python3
"""gate-kanban-complete-latency-watch.py — NAMED CONSUMER for the completion-gate latency tripwire.

Producer (owned by kanban t_6b9abe2d): /home/frank/.hermes/agent-hooks/gate-kanban-complete.sh
appends one line to the tripwire log when a kanban_complete gate run takes
>= HERMES_HOOK_GATE_LATENCY_WARN (default 10s), in the shape:

    <iso-ts> gate-kanban-complete <elapsed>s (warn>=<warn>)s tool=<tool> tid=<tid>

Every line is BY CONSTRUCTION an approaching-budget event: the effective
plugin-callback timeout is 30s (plugins.hook_callback_timeout, config_defaults.py),
so a >=10s gate run is >=33% of the cap and the old gate proved it can hit 30.01s
abandonment (t_f9e39b7a). Before this check the log had no reader, no rotation and
no liveness proof.

This check is the consumer, wired into the jarvis guard-bundle 15m tick
(cron_guard_bundle_runner.py, check name `gate-kanban-complete-latency-watch`):

* Reads the tripwire log; NEW lines since the last run are reported and the check
  exits 1 -> guard-bundle escalation path (aggregate report -> report-to-board.py
  -> discord:#fleet-reports + jarvis-os card). Exit 0 + silent when nothing new.
* First run only records a baseline (pre-existing lines never alert).
* Rotates the store at a size cap (keeps one .1 backup) so it stays bounded.

Guard-bundle watchdog semantics: healthy = silent rc=0, alert = printed rc!=0.
Watermark state: <jarvis cron>/state/gate-kanban-complete-latency-watch.json
Env overrides for hermetic tests: GATE_LATENCY_LOG, GATE_LATENCY_STATE.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

DEFAULT_LOG = Path("/home/frank/.hermes/logs/gate-kanban-complete-latency.log")
DEFAULT_STATE = Path(
    "/home/frank/.hermes/profiles/jarvis/cron/state/gate-kanban-complete-latency-watch.json"
)
# Rotate at ~1MB. The gate writes only on >=10s runs (~100 bytes/line), so this
# bounds the store to roughly 10k tripwire events between rotations.
MAX_LOG_BYTES = 1_000_000


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True) + "\n")
        tmp.replace(path)
    except Exception as e:
        print(f"GATE-LATENCY-WATCH: could not write state: {e}", file=sys.stderr)


def main() -> int:
    log = Path(os.environ.get("GATE_LATENCY_LOG", str(DEFAULT_LOG)))
    state_path = Path(os.environ.get("GATE_LATENCY_STATE", str(DEFAULT_STATE)))
    state = _load_state(state_path)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if not log.is_file():
        # No tripwire store yet: producer not installed / nothing recorded.
        # Record the baseline and stay silent.
        _save_state(state_path, {"baseline_at": now_iso, "lines_seen": 0})
        return 0

    rotated = False
    try:
        size = log.stat().st_size
        if size > MAX_LOG_BYTES:
            bak = log.with_name(log.name + ".1")
            bak.unlink(missing_ok=True)
            log.replace(bak)
            rotated = True
            state["rotated_at"] = now_iso
            state["rotated_size"] = size
            state["lines_seen"] = 0  # fresh file -> reset the watermark
    except Exception as e:
        print(f"GATE-LATENCY-WATCH: rotation failed: {e}", file=sys.stderr)

    try:
        lines = log.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        # Rotation just moved the store away and the producer has not re-created
        # the file yet (the gate appends lazily) — treat it as an empty fresh file.
        lines = []
    except Exception as e:
        print(f"GATE-LATENCY-WATCH: cannot read {log}: {e}", file=sys.stderr)
        return 1

    lines_seen = int(state.get("lines_seen", 0) or 0)

    # Store shrank (external truncation): reset the watermark to the new tail so
    # old lines are not re-alerted and future lines are not missed.
    if len(lines) < lines_seen:
        state["lines_seen"] = len(lines)
        _save_state(state_path, state)
        return 0

    # First real baseline (log exists but we have never seen it) — never alert
    # on pre-existing history.
    if not state.get("baseline_at"):
        _save_state(state_path, {
            "baseline_at": now_iso, "lines_seen": len(lines),
            "rotated_at": state.get("rotated_at"), "rotated_size": state.get("rotated_size"),
        })
        return 0

    # A rotation just happened: the current file is post-rotation, so its lines
    # are the new baseline (pre-rotation history moved to .1).
    if rotated:
        _save_state(state_path, {
            "baseline_at": state.get("baseline_at", now_iso),
            "lines_seen": len(lines),
            "rotated_at": state.get("rotated_at"), "rotated_size": state.get("rotated_size"),
        })
        return 0

    new_lines = lines[lines_seen:]
    if not new_lines:
        return 0  # silent watchdog: nothing new

    # New tripwire line(s) since the last run — the approaching-budget signal.
    state["lines_seen"] = len(lines)
    state["last_alert_at"] = now_iso
    _save_state(state_path, state)
    print(f"GATE-LATENCY-WATCH: {len(new_lines)} new tripwire line(s) at {now_iso} "
          f"(kanban_complete gate ran >= warn threshold — approaching the 30s callback budget):")
    for ln in new_lines:
        print(f"  {ln}")
    print("consumer: jarvis guard-bundle 15m tick -> report-to-board -> discord:#fleet-reports / jarvis-os card")
    return 1


if __name__ == "__main__":
    sys.exit(main())
