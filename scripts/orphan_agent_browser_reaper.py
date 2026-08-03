#!/usr/bin/env python3
# CANONICAL SOURCE - do not edit profile-local copies.
"""orphan_agent_browser_reaper.py - reap leaked headless agent-browser Chromium roots.

Filed for kanban t_9b49cd19 (DGX swap exhaustion).

Root cause: each Hermes browser-tool session launches a headless Chromium with
--user-data-dir=/tmp/agent-browser-chrome-<uuid>. When the owning agent session
ends without a clean browser close, the Chromium root survives and is reparented
to pid 1 / systemd --user. On 2026-08-02, 43 such orphans (474 processes) held
6.2 GB of swap - the leading cause of the 15Gi/15Gi swap exhaustion that took
CUDA init offline (t_b0c418cd). Each is individually small, so they are invisible
in any top-N-by-process swap listing; only the aggregate is visible.

Reap criterion (conservative, all must hold):
  1. cmdline is a Chromium ROOT (has --user-data-dir=/tmp/agent-browser-chrome-*,
     and no --type= - renderers/zygotes/gpu die with their root).
  2. PPid is 1 or systemd --user - i.e. the launching agent process is GONE.
     A browser still owned by a live agent is never touched.
  3. Process age >= MIN_AGE_MIN minutes (default 30), so an in-flight session
     that transiently reparents is not reaped.

SIGTERM first, SIGKILL only for stragglers. Stale /tmp/agent-browser-chrome-*
dirs with no live owner are removed too.

Quiet on no-op (no-agent cron semantics): prints only when it reaps something.
Exit 0 always unless the reaper itself errors - a reap is routine hygiene, not
an alert condition. Swap alerting lives in local_inference_liveness_probe.py.

Env:
  MIN_AGE_MIN   default 30
  DRY_RUN       set to 1 to report without killing
"""
from __future__ import annotations

import os
import shutil
import signal
import sys
import time
from pathlib import Path

MIN_AGE_MIN = int(os.environ.get("MIN_AGE_MIN", "30"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UDD_PREFIX = "--user-data-dir=/tmp/agent-browser-chrome-"
TMP_GLOB = "agent-browser-chrome-*"


def cmdline(pid: int) -> list[str]:
    """Argv tokens. Chromium rewrites its own cmdline as ONE space-joined blob
    rather than NUL-separated argv, so split on whitespace too - matching on
    NUL-separated tokens alone silently misses every Chromium process."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    tokens: list[str] = []
    for chunk in raw.decode("utf-8", "replace").split("\0"):
        tokens.extend(t for t in chunk.split() if t)
    return tokens


def ppid_of(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return -1


def age_minutes(pid: int) -> float:
    try:
        return (time.time() - Path(f"/proc/{pid}").stat().st_ctime) / 60.0
    except OSError:
        return 0.0


def is_systemd_user(pid: int) -> bool:
    argv = cmdline(pid)
    return bool(argv) and "systemd" in argv[0] and "--user" in argv


def find_orphan_roots() -> list[tuple[int, str]]:
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        argv = cmdline(pid)
        if not argv:
            continue
        udd = next((a for a in argv if a.startswith(UDD_PREFIX)), None)
        if not udd:
            continue
        if any(a.startswith("--type=") for a in argv):
            continue  # child process; dies with its root
        parent = ppid_of(pid)
        if parent != 1 and not is_systemd_user(parent):
            continue  # still owned by a live agent - leave alone
        if age_minutes(pid) < MIN_AGE_MIN:
            continue
        out.append((pid, udd.split("=", 1)[1]))
    return out


def swap_free_mb() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("SwapFree:"):
            return int(line.split()[1]) // 1024
    return -1


def alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def main() -> int:
    orphans = find_orphan_roots()
    if not orphans:
        return 0

    before = swap_free_mb()
    pids = [p for p, _ in orphans]
    dirs = {d for _, d in orphans}

    if DRY_RUN:
        print(f"DRY_RUN: would reap {len(pids)} orphan agent-browser roots: {pids}")
        return 0

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    for _ in range(25):
        if not any(alive(p) for p in pids):
            break
        time.sleep(1)
    stragglers = [p for p in pids if alive(p)]
    for pid in stragglers:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(3)

    removed = 0
    # Keep-set must be EVERY live agent-browser's data dir, not just the
    # remaining orphans - otherwise this deletes the profile dir out from
    # under a healthy browser that is still owned by a live agent.
    live_dirs = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        for arg in cmdline(int(entry.name)):
            if arg.startswith(UDD_PREFIX):
                live_dirs.add(arg.split("=", 1)[1])
    for d in Path("/tmp").glob(TMP_GLOB):
        if str(d) in live_dirs:
            continue
        try:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
        except OSError:
            pass

    after = swap_free_mb()
    print(
        f"agent-browser reaper: reaped {len(pids)} orphan Chromium roots "
        f"({len(stragglers)} needed SIGKILL), removed {removed} stale /tmp profile dirs. "
        f"SwapFree {before} MB -> {after} MB (+{after - before} MB)."
    )
    print("Cause: Hermes browser-tool sessions that exited without closing Chromium.")
    print("Runbook: kanban t_9b49cd19.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
