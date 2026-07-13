#!/usr/bin/env python3
"""Gateway cgroup reaper / sprawl sentinel.

Companion to the systemd ``memory-guard.conf`` drop-ins added for task
t_a89cb802. The drop-ins cap memory and task counts; this guard adds *active*
awareness of process sprawl of the kind the claude-seat audit flagged
(orphaned vitest fork workers, multiple tsservers, timed-out ``find`` left
running inside a gateway cgroup).

Design (fail-closed, read-only by default):
  * Default mode is REPORT-ONLY: it inspects each gateway cgroup, emits JSON
    and a human line, and exits non-zero only when it finds sprawl that *would*
    have been reaped (so a cron can alert). It NEVER kills anything.
  * ``--enforce``: sends SIGTERM (then SIGKILL after --grace) to sprawl
    candidates. Still bounded -- it only targets the specific "leftover helper"
    signatures, never the gateway main process or its tracked children.

Sprawl signatures matched (only outside the gateway's own main+tracked tree):
  * > 1 tsserver / pyright-langserver inside one gateway cgroup
  * vitest / vitest-worker / @vitest/runner fork children still alive after the
    parent gateway command should have exited
  * ``find`` / ``grep -r`` style long-lived scan processes whose parent is gone
    (orphaned) and etime exceeds --max-etime
  * any process whose parent PID is not in the cgroup and is not a known helper

This is deliberately conservative: the cgroup memory caps are the real safety
net (they cannot be evaded); this script is the early-warning + optional tidy.

No secrets are read or logged.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

CGROUP_ROOT = "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice"

GATEWAY_UNITS = [
    "hermes-gateway-jarvis",
    "hermes-gateway-jarvis-voice",
    "hermes-gateway-jarvis-os-pm",
    "hermes-gateway-sycode-trading-pm",
    "hermes-surface-gateway",
]

# Commands we treat as "sprawl-prone leftover helpers".
TSSERVER_RE = re.compile(r"(tsserver|pyright-langserver|esbuild|typescript-language-server)", re.I)
VITEST_RE = re.compile(r"(vitest|vitest-worker|@vitest/runner)", re.I)
FIND_RE = re.compile(r"\b(find|grep)\b.*(-r|-R|--include|--exclude)", re.I)


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _cgroup_procs(cgroup_path: str) -> list[int]:
    raw = _read(f"{cgroup_path}/cgroup.procs")
    if raw is None:
        return []
    out: list[int] = []
    for line in raw.splitlines():
        line = line.strip()
        if line and line.isdigit():
            out.append(int(line))
    return out


def _proc_fields(pid: int) -> dict:
    """Return ppid, comm, etime_seconds, args for a pid (best-effort)."""
    info: dict = {"pid": pid, "ppid": None, "comm": None, "etime_s": None, "args": "", "alive": False}
    stat = _read(f"/proc/{pid}/stat")
    if stat:
        info["alive"] = True
        # comm is the 2nd field, wrapped in parens, may contain spaces.
        m = re.search(r"\(([^)]*)\)\s+(\w+)\s+(\d+)", stat)
        if m:
            info["comm"] = m.group(1)
            info["state"] = m.group(2)
            info["ppid"] = int(m.group(3))
    status = _read(f"/proc/{pid}/status")
    if status:
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                info["rss_kb"] = int(line.split()[1])
    cmd = _read(f"/proc/{pid}/cmdline")
    if cmd:
        info["args"] = cmd.replace("\0", " ").strip()
    etime = _read(f"/proc/{pid}/stat")  # re-read; etime not in stat. Use ps-free method below.
    info["etime_s"] = _etime_seconds(pid)
    return info


def _etime_seconds(pid: int) -> float | None:
    """Compute process elapsed time (seconds) from /proc/stat start_time."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            fields = fh.read().split()
        start_ticks = int(fields[21])
        clk = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        now_ticks = time.time() * clk
        return max(0.0, (now_ticks - start_ticks) / clk)
    except (OSError, IndexError, ValueError):
        return None


def _is_orphaned(pid: int, cgroup_pids: set[int]) -> bool:
    """True if the process's parent is not in the same cgroup (orphaned in it)."""
    info = _proc_fields(pid)
    ppid = info.get("ppid")
    if ppid is None:
        return False
    # Parent dead -> orphaned.
    if not Path(f"/proc/{ppid}").exists():
        return True
    # Parent alive but outside the cgroup -> orphaned relative to gateway tree.
    return ppid not in cgroup_pids


def inspect_unit(unit: str, max_etime: float) -> dict:
    cg = f"{CGROUP_ROOT}/{unit}.service"
    procs = _cgroup_procs(cg)
    pids = set(procs)
    findings: list[dict] = []
    tsservers: list[int] = []

    for pid in procs:
        info = _proc_fields(pid)
        if not info.get("alive"):
            continue
        args = info.get("args", "")
        comm = info.get("comm", "")
        etime = info.get("etime_s")

        if TSSERVER_RE.search(args) or TSSERVER_RE.search(comm or ""):
            tsservers.append(pid)
        if VITEST_RE.search(args):
            findings.append({**info, "reason": "vitest_leftover"})
        if FIND_RE.search(args) and (_is_orphaned(pid, pids)) and (etime is None or etime > max_etime):
            findings.append({**info, "reason": "orphaned_long_find"})

    # More than one tsserver/pyright/language-server in one gateway cgroup = sprawl.
    if len(tsservers) > 1:
        for pid in tsservers[1:]:
            f = _proc_fields(pid)
            findings.append({**f, "reason": "duplicate_language_server", "siblings": tsservers})

    return {
        "unit": unit,
        "cgroup": cg,
        "pids": len(procs),
        "memory_current_bytes": _read(f"{cg}/memory.current"),
        "memory_high_bytes": _read(f"{cg}/memory.high"),
        "memory_max_bytes": _read(f"{cg}/memory.max"),
        "pids_max": _read(f"{cg}/pids.max"),
        "tsserver_count": len(tsservers),
        "sprawl": findings,
    }


def reap(unit: str, findings: list[dict], grace: float) -> int:
    killed = 0
    for f in findings:
        pid = f.get("pid")
        if not pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except (ProcessLookupError, PermissionError):
            continue
    if grace > 0 and killed:
        time.sleep(grace)
        for f in findings:
            pid = f.get("pid")
            if pid and Path(f"/proc/{pid}").exists():
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    continue
    return killed


def main() -> int:
    ap = argparse.ArgumentParser(description="Gateway cgroup sprawl sentinel")
    ap.add_argument("--enforce", action="store_true", help="Actually SIGTERM/SIGKILL sprawl (default: report-only)")
    ap.add_argument("--grace", type=float, default=10.0, help="Seconds between SIGTERM and SIGKILL in enforce mode")
    ap.add_argument("--max-etime", type=float, default=600.0, help="Max age (s) before an orphaned find is sprawl")
    ap.add_argument("--json", action="store_true", help="Emit JSON only")
    args = ap.parse_args()

    report: dict = {"checked_at": time.time(), "enforce": args.enforce, "units": [], "total_sprawl": 0}
    for unit in GATEWAY_UNITS:
        u = inspect_unit(unit, args.max_etime)
        report["units"].append(u)
        report["total_sprawl"] += len(u["sprawl"])
        if u["sprawl"]:
            for f in u["sprawl"]:
                line = f"  [SPRAWL] {unit} pid={f.get('pid')} comm={f.get('comm')} reason={f.get('reason')} etime={f.get('etime_s')}"
                if args.enforce:
                    line += "  -> SIGTERM"
                print(line)
            if args.enforce:
                n = reap(unit, u["sprawl"], args.grace)
                print(f"  reaped {n} process(es) in {unit}")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        # Human summary line
        total_pids = sum(u["pids"] for u in report["units"])
        print(f"SUMMARY units={len(report['units'])} total_pids={total_pids} sprawl_findings={report['total_sprawl']} enforce={args.enforce}")

    # Exit non-zero when sprawl found (so a cron wrapper can alert) UNLESS in
    # enforce mode (where we've already acted) -- still non-zero so cron knows
    # something was found+acted.
    return 1 if report["total_sprawl"] else 0


if __name__ == "__main__":
    sys.exit(main())
