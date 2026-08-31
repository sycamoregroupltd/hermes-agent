#!/usr/bin/env python3
"""Silent Mac exhaustion watchdog; stdout is the cron alert payload."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

SSH = os.environ.get("MAC_HEALTH_SSH", "mac")
STATE = Path(os.environ.get("MAC_HEALTH_STATE", os.path.expanduser("~/.hermes/mac-health-watch.state")))
MIN_FREE_GB = float(os.environ.get("MAC_HEALTH_MIN_FREE_GB", "5"))
MAX_USED_PCT = float(os.environ.get("MAC_HEALTH_MAX_USED_PCT", "99.0"))
MAX_SWAP_GB = float(os.environ.get("MAC_HEALTH_MAX_SWAP_GB", "55"))
MAX_CUA_PROCS = int(os.environ.get("MAC_HEALTH_MAX_CUA_PROCS", "120"))
MAX_CUA_CPU = float(os.environ.get("MAC_HEALTH_MAX_CUA_CPU", "300"))
MAX_CODEX_DESCENDANTS = int(os.environ.get("MAC_HEALTH_MAX_CODEX_DESCENDANTS", "300"))
MAX_CODEX_RSS_GB = float(os.environ.get("MAC_HEALTH_MAX_CODEX_RSS_GB", "12"))


def remote() -> str:
    fixture = os.environ.get("MAC_HEALTH_FIXTURE")
    if fixture:
        return Path(fixture).read_text()
    cmd = (
        "df -kP ~; printf '\n--SWAP--\n'; sysctl -n vm.swapusage; "
        "printf '\n--PROC--\n'; ps -axo pid=,ppid=,rss=,pcpu=,command="
    )
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", SSH, cmd],
        capture_output=True, text=True, timeout=45, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"ssh failed rc={result.returncode}: {result.stderr.strip()}")
    return result.stdout


def parse(raw: str) -> tuple[float, float, float, int, float, int, float]:
    df_line = next((line for line in raw.splitlines() if line.startswith("/dev/")), "")
    fields = df_line.split()
    swap = re.search(r"used = ([0-9.]+)([MG])", raw)
    proc_lines = raw.split("--PROC--", 1)[-1].splitlines()
    proc_rows: list[tuple[int, int, int, float, str]] = []
    cua_values: list[float] = []
    for line in proc_lines:
        parts = line.strip().split(None, 4)
        if len(parts) == 5:
            try:
                row = (int(parts[0]), int(parts[1]), int(parts[2]), float(parts[3]), parts[4])
                proc_rows.append(row)
                if re.search(r"cua_node|node_repl", row[4]):
                    cua_values.append(row[3])
                continue
            except ValueError:
                pass
        # Preserve compatibility with the compact fixtures used by the original
        # monitor, where each process line ended with CPU and omitted pid/ppid/RSS.
        if parts and re.search(r"cua_node|node_repl", line):
            try:
                cua_values.append(float(parts[-1]))
            except ValueError:
                pass
    if len(fields) < 5 or not swap:
        raise ValueError("unexpected Mac health output")
    if swap.group(2) == "M":
        swap_gb = float(swap.group(1)) / 1024
    else:
        swap_gb = float(swap.group(1))

    roots = {
        pid for pid, _ppid, _rss, _cpu, command in proc_rows
        if "ChatGPT.app/Contents/Resources/codex" in command and "app-server" in command
    }
    children: dict[int, list[int]] = {}
    by_pid = {row[0]: row for row in proc_rows}
    for pid, ppid, _rss, _cpu, _command in proc_rows:
        children.setdefault(ppid, []).append(pid)
    codex_pids = set(roots)
    stack = list(roots)
    while stack:
        parent = stack.pop()
        for child in children.get(parent, []):
            if child not in codex_pids:
                codex_pids.add(child)
                stack.append(child)
    codex_rss_gb = sum(by_pid[pid][2] for pid in codex_pids) / 1024 / 1024
    codex_descendants = max(0, len(codex_pids) - len(roots))

    return (
        float(fields[3]) / 1024 / 1024,
        float(fields[4].rstrip("%")),
        swap_gb,
        len(cua_values),
        sum(cua_values),
        codex_descendants,
        codex_rss_gb,
    )


def main() -> int:
    try:
        free_gb, used_pct, swap_gb, cua_procs, cua_cpu, codex_descendants, codex_rss_gb = parse(remote())
        findings = []
        if free_gb < MIN_FREE_GB:
            findings.append(f"APFS free {free_gb:.2f} GiB < {MIN_FREE_GB:g} GiB")
        if used_pct > MAX_USED_PCT:
            findings.append(f"APFS used {used_pct:.1f}% > {MAX_USED_PCT:g}%")
        if swap_gb > MAX_SWAP_GB:
            findings.append(f"VM swap used {swap_gb:.1f} GiB > {MAX_SWAP_GB:g} GiB")
        if cua_procs > MAX_CUA_PROCS:
            findings.append(f"CUA node processes {cua_procs} > {MAX_CUA_PROCS}")
        if cua_cpu > MAX_CUA_CPU:
            findings.append(f"CUA node CPU {cua_cpu:.1f}% > {MAX_CUA_CPU:g}%")
        if codex_descendants > MAX_CODEX_DESCENDANTS:
            findings.append(
                f"Codex app-server descendants {codex_descendants} > {MAX_CODEX_DESCENDANTS}"
            )
        if codex_rss_gb > MAX_CODEX_RSS_GB:
            findings.append(f"Codex process-tree RSS {codex_rss_gb:.1f} GiB > {MAX_CODEX_RSS_GB:g} GiB")
        if not findings:
            STATE.unlink(missing_ok=True)
            return 0
        body = "🔴 MAC HEALTH: " + "; ".join(findings)
        digest = hashlib.sha256(body.encode()).hexdigest()
        if STATE.exists() and STATE.read_text().strip() == digest:
            return 0
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(digest + "\n")
        print(body)
        print("No automatic delete/kill/reboot; Frank approval remains required for remediation.")
    except Exception as exc:
        body = f"🔴 MAC HEALTH WATCH FAILED: {exc}"
        digest = hashlib.sha256(body.encode()).hexdigest()
        if STATE.exists() and STATE.read_text().strip() == digest:
            return 0
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(digest + "\n")
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
