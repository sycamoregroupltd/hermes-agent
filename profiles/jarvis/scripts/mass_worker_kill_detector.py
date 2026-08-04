#!/usr/bin/env python3
"""mass_worker_kill_detector.py — catch simultaneous multi-worker deaths in the act.

WHY (2026-08-04, t_4caa915b): 11 workers across 8 different profiles died at the
SAME wall-clock instant (21:21:42Z) at an identical 332s elapsed. They were doing
real work — bunx tsc, eslint, git staging, plan progress 3/5 then 4/5 — and died
mid-operation. Recorded only as "pid <N> not alive" with empty summaries.

That single event took the board from 11 running back to 3, i.e. it is the
throughput ceiling, and the cards then retry, burning provider calls during a 503
storm and re-entering the blocked pile as crash casualties.

RULED OUT from cold data (this is why the detector exists — none of it stuck):
  - parent-timeout kill: workers spawn with start_new_session=True, so they
    survive `timeout 300` killing the dispatch call that created them
  - OOM: 85GB of 121GB free, nothing in dmesg
  - gateway restart: uptime 1d08h, unbroken across the event
  - container recreates: supabase edge at 20:55Z, server at 21:27Z — neither
    matches 21:21:42Z
  - a per-worker runtime cap: max_runtime_seconds was NULL on every victim and no
    config value near 300s governs worker lifetime
  - periodicity: every other crash in a 3h window is a singleton; this was one event

Post-mortem data is too thin to settle it, so this captures state AT the moment.
It samples recent crashes and, when several land inside a short window, snapshots
what a cold log cannot reconstruct: load, memory, the dispatcher's own uptime,
container restart times, and the most recent kernel messages.

FAIL-CLOSED: probe errors exit non-zero. Healthy = empty stdout, exit 0.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BOARDS_DIR = Path("/home/frank/.hermes/kanban/boards")
BOARDS = ["sycode-trading", "jarvis-os", "upero", "ai-restaurant"]
EVIDENCE = Path("/home/frank/.hermes/var/mass-worker-kill")
LOOKBACK_S = int(1800)      # scan crashes in the last 30 min
CLUSTER_S = int(90)         # deaths within this window count as simultaneous
CLUSTER_MIN = int(4)        # this many or more = a mass kill, not bad luck


class ProbeError(RuntimeError):
    pass


def recent_crashes() -> list[tuple[int, str, str, str]]:
    out: list[tuple[int, str, str, str]] = []
    cutoff = int(time.time()) - LOOKBACK_S
    for board in BOARDS:
        db = BOARDS_DIR / board / "kanban.db"
        if not db.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        except sqlite3.Error as e:
            raise ProbeError(f"cannot open {db}: {e}")
        try:
            for r in conn.execute(
                "SELECT ended_at, task_id, profile, COALESCE(ended_at-started_at,-1) "
                "FROM task_runs WHERE status='crashed' AND ended_at > ?", (cutoff,)
            ):
                out.append((int(r[0]), board, f"{r[1]}@{r[2]}", str(r[3])))
        finally:
            conn.close()
    return sorted(out)


def cluster(crashes) -> list[list]:
    groups, cur = [], []
    for c in crashes:
        if cur and c[0] - cur[0][0] <= CLUSTER_S:
            cur.append(c)
        else:
            if len(cur) >= CLUSTER_MIN:
                groups.append(cur)
            cur = [c]
    if len(cur) >= CLUSTER_MIN:
        groups.append(cur)
    return groups


def snapshot() -> dict:
    """State a cold log cannot reconstruct. Best-effort; never raises."""
    def run(cmd: list[str]) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()[:2000]
        except (subprocess.SubprocessError, OSError):
            return "(unavailable)"
    snap = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "loadavg": Path("/proc/loadavg").read_text().strip() if Path("/proc/loadavg").exists() else "?",
        "meminfo_available_kb": "",
        "gateway": run(["pgrep", "-af", "gateway run"]),
        "dmesg_tail": run(["dmesg", "-T"])[-1200:],
    }
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable"):
                snap["meminfo_available_kb"] = line.split()[1]
                break
    except OSError:
        pass
    if shutil.which("docker"):
        snap["containers"] = run(["docker", "ps", "--format", "{{.Names}} {{.Status}}"])
    return snap


def main() -> int:
    groups = cluster(recent_crashes())
    if not groups:
        return 0  # healthy -> silent

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    snap = snapshot()
    printed = False
    for g in groups:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(g[0][0]))
        path = EVIDENCE / f"masskill-{stamp}.json"
        if path.exists():
            continue  # already captured this event — don't re-alert every 5 min
        elapsed = {c[3] for c in g}
        path.write_text(json.dumps({
            "died_at_epoch": g[0][0], "count": len(g),
            "victims": [c[2] for c in g], "boards": sorted({c[1] for c in g}),
            "elapsed_seconds": sorted(elapsed), "snapshot": snap,
        }, indent=2))
        printed = True
        print(f"MASS WORKER KILL — {len(g)} workers died within {CLUSTER_S}s")
        print(f"  at:       {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(g[0][0]))}")
        print(f"  boards:   {', '.join(sorted({c[1] for c in g}))}")
        varied = len(elapsed) > 2
        print(f"  elapsed:  {sorted(elapsed)}")
        print("  VARIED elapsed => external mass-kill, NOT a per-worker runtime cap"
              if varied else "  uniform elapsed => suspect a shared timeout/cap")
        print(f"  victims:  {', '.join(c[2] for c in g[:6])}{' …' if len(g) > 6 else ''}")
        print(f"  evidence: {path}")
        print(f"  load={snap['loadavg']}  mem_avail_kb={snap['meminfo_available_kb']}")
        print("  See kanban t_4caa915b for what has already been ruled out.")
    return 0 if printed or groups else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProbeError as e:
        print(f"mass_worker_kill_detector: PROBE FAILED (not 'healthy'): {e}", file=sys.stderr)
        sys.exit(1)
