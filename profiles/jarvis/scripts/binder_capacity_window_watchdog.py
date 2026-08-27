#!/usr/bin/env python3
"""Binder-activation capacity-window watchdog (t_4a7979bb / t_5412bbd1).

Gate (from the Grok readiness receipt on t_5412bbd1): DGX one-minute load
below 10 AND swap used below 4 GiB, on two consecutive samples at least
4 minutes apart. This watchdog samples on each cron tick (5m), keeps state in
~/.hermes/state/, stays SILENT while the gate fails or after it has already
fired, and on the first two-sample PASS:
  - prints one alert line (non-empty stdout -> cron delivery), and
  - posts an evidence comment to jarvis-os/t_4a7979bb so the card owner
    (codex-ava-front-seat-20260823) has a durable consumer.
It never unblocks, dispatches, installs, resumes, or mutates anything else.
Re-arms automatically after any failing sample.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

STATE = Path("/home/frank/.hermes/state/binder-capacity-window-watchdog.json")
HERMES = "/home/frank/.local/bin/hermes"
BOARD = "jarvis-os"
CARD = "t_4a7979bb"
LOAD_MAX = 10.0
SWAP_MAX_GIB = 4.0
MIN_GAP_S = 240  # two samples at least 4 minutes apart


def sample() -> dict:
    load1 = os.getloadavg()[0]
    swap_total = 0
    swap_free = 0
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("SwapTotal:"):
            swap_total = int(line.split()[1])
        elif line.startswith("SwapFree:"):
            swap_free = int(line.split()[1])
    swap_used_gib = (swap_total - swap_free) / (1024 * 1024)
    return {"ts": int(time.time()), "load1": round(load1, 2),
            "swap_gib": round(swap_used_gib, 2),
            "pass": load1 < LOAD_MAX and swap_used_gib < SWAP_MAX_GIB}


def main() -> int:
    now = sample()
    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except Exception:
            state = {}
    prev = state.get("prev")
    fired = state.get("fired", False)

    if not now["pass"]:
        # Gate failing: reset streak, re-arm, stay silent.
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"prev": None, "fired": False, "last": now}))
        return 0

    window_open = (
        prev is not None
        and prev.get("pass")
        and now["ts"] - prev.get("ts", 0) >= MIN_GAP_S
    )

    if window_open and not fired:
        ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        msg = (
            f"CAPACITY WINDOW OPEN for binder one-seat canary (t_4a7979bb): "
            f"two consecutive samples passed — prev load1={prev['load1']} "
            f"swap={prev['swap_gib']}GiB, now load1={now['load1']} "
            f"swap={now['swap_gib']}GiB (gate: load1<{LOAD_MAX}, "
            f"swap<{SWAP_MAX_GIB}GiB, gap>={MIN_GAP_S}s) at {ts}. "
            f"Remaining activation prerequisites are unchanged: ESTOP present, "
            f"supervised Mac caller, provider-compliant canary, rollback packet."
        )
        try:
            subprocess.run(
                [HERMES, "kanban", "--board", BOARD, "comment",
                 "--author", "binder-capacity-watchdog", CARD, msg],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "HERMES_HOME": "/home/frank/.hermes"},
            )
        except Exception:
            pass  # alert still delivers via stdout
        print(msg)
        STATE.write_text(json.dumps({"prev": now, "fired": True, "last": now}))
        return 0

    # Passing but streak not yet complete, or already fired: silent.
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"prev": now, "fired": fired, "last": now}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
