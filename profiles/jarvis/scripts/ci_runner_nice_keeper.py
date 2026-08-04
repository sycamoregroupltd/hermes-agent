#!/usr/bin/env python3
"""ci_runner_nice_keeper.py — keep co-located CI runners at low priority so agents live.

WHY (2026-08-04, kanban t_4caa915b): this 20-core box runs BOTH the self-hosted GitHub
Actions farm and the Hermes agent fleet, with no resource isolation. Measured during a
mass worker kill:

    CI runners (9 dirs, 64 procs)   1079% CPU   (~10.8 cores)
    agent fleet                      103% CPU   (~1 core, shared by ELEVEN workers)
    everything else                 1622% CPU
    TOTAL                           2804% on a 2000% box  = 40% oversubscribed
    load average 87 / 75 / 72

Starved agents miss heartbeats and die together. The signature is unmistakable: workers
at wildly different elapsed times (20s .. 2002s) dying at the SAME instant, repeatedly
(18:34, 18:41, 21:21). Each death loses all of that worker's progress and re-enters the
blocked pile as a crash casualty, then retries — burning provider calls during a 503
storm. CI merely running slower costs nothing comparable.

WHAT THIS DOES
Renices CI runner processes to +10. Under contention they yield to the agent fleet; on
an idle box they still run at full speed, so this is not a throughput cap — it is a
priority ordering. Fully reversible (`renice -n 0`), needs no root, touches nothing else.

Runs on a schedule because the runner service spawns fresh processes constantly: a
one-shot renice covers existing children only and decays within minutes.

NOT A FIX. The real answer is cgroup isolation (cpu.max per slice) or moving CI off-box.
This is the reversible mitigation while that decision sits with Frank.

FAIL-CLOSED: probe errors exit non-zero. Healthy = empty stdout, exit 0.
"""
from __future__ import annotations

import os
import subprocess
import sys

TARGET_NICE = int(os.environ.get("CI_NICE", "10"))
PATTERN = os.environ.get("CI_NICE_PATTERN", "actions-runner")


class ProbeError(RuntimeError):
    pass


def main() -> int:
    try:
        cp = subprocess.run(["pgrep", "-f", PATTERN], capture_output=True, text=True, timeout=30)
    except subprocess.SubprocessError as e:
        raise ProbeError(f"pgrep failed: {e}")
    # pgrep exits 1 when nothing matches — that is a legitimate "no runners", not an error.
    if cp.returncode not in (0, 1):
        raise ProbeError(f"pgrep rc={cp.returncode}: {cp.stderr.strip()[:120]}")

    pids = [p for p in cp.stdout.split() if p.isdigit()]
    raised = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as fh:
                # field 19 (1-indexed) is nice; skip the comm field which may contain spaces
                fields = fh.read().rsplit(")", 1)[-1].split()
            if int(fields[16]) >= TARGET_NICE:
                continue  # already low priority
        except (OSError, IndexError, ValueError):
            continue  # process vanished mid-scan, or unparseable — skip quietly
        if subprocess.run(["renice", "-n", str(TARGET_NICE), "-p", pid],
                          capture_output=True, timeout=15).returncode == 0:
            raised += 1

    if not raised:
        return 0  # nothing to do -> silent
    print(f"CI NICE KEEPER: reniced {raised} new CI runner process(es) to +{TARGET_NICE} "
          f"({len(pids)} total matching '{PATTERN}')")
    print("  Why: CI and the agent fleet share 20 cores with no isolation; starved agents "
          "die together. See kanban t_4caa915b.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProbeError as e:
        print(f"ci_runner_nice_keeper: PROBE FAILED (not 'healthy'): {e}", file=sys.stderr)
        sys.exit(1)
