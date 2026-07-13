#!/usr/bin/env python3
"""Watchdog for Hermes/Nous gateway processes — silent when healthy.

On each run it checks that every expected gateway profile process is alive.
If a process is missing, it logs the event and attempts one restart via
``hermes gateway run --replace <profile>`` in the background, then reports
the crash+restart attempt as an alert.

Expected gateways are discovered from the running session at first run and
memorised in a state file, so new gateways added between crons are picked up.
If no gateways are running at baseline, the watchdog waits (first-run grace).

Designed for no_agent cron delivery: stdout is delivered verbatim.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

# --- config ----------------------------------------------------------------
STATE_DIR = Path(os.environ.get("NOUS_WATCH_STATE_DIR", "/home/frank/.hermes/cron/state"))
STATE_FILE = STATE_DIR / "nous-crash-watch.json"
HERMES = os.environ.get("HERMES_BIN", "/home/frank/.hermes/hermes-agent/venv/bin/hermes")
LOG_DIR = Path(os.environ.get("NOUS_WATCH_LOG_DIR", "/home/frank/.hermes/scripts/logs"))
LOG_FILE = LOG_DIR / "nous-crash-watch.log"
HEARTBEAT_FILE = STATE_DIR / "nous-crash-watch.heartbeat"

# --- helpers ---------------------------------------------------------------


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"baseline_gateways": [], "known_dead_gateways": {}}


def write_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def get_running_gateway_processes() -> list[dict]:
    """Parse ``hermes gateway list`` output for running gateways with PIDs."""
    cmd = [HERMES, "gateway", "list"]
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log(f"hermes gateway list unavailable: {exc}")
        return []

    gateways: list[dict] = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Profile") or line.startswith("---"):
            continue
        parts = line.split()
        if len(parts) >= 4:
            # Expected format: <profile> <pid> <status> <mode> ...
            profile = parts[0]
            pid_str = parts[1]
            status = parts[2]
            pid = int(pid_str) if pid_str.isdigit() else None
            if status.lower() == "running" and pid:
                alive = False
                try:
                    os.kill(pid, 0)  # zero-signal liveness check
                    alive = True
                except (OSError, ProcessLookupError):
                    pass
                gateways.append({"profile": profile, "pid": pid, "alive": alive})
    return gateways


def get_running_pids_from_ps() -> dict[str, int]:
    """Fallback: scan ``ps`` for ``hermes ... gateway run`` processes."""
    cmd = ["pgrep", "-f", "hermes.*gateway run"]
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    pids: dict[str, int] = {}
    for pid_str in p.stdout.strip().splitlines():
        pid_str = pid_str.strip()
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        # get profile from /proc/<pid>/cmdline
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_text().replace("\0", " ").strip()
        except (FileNotFoundError, PermissionError):
            continue
        if "--profile" in cmdline:
            parts = cmdline.split()
            try:
                idx = parts.index("--profile")
                profile = parts[idx + 1]
                pids[profile] = pid
            except (ValueError, IndexError):
                pass
    return pids


def attempt_restart(profile: str) -> bool:
    """Launch ``hermes gateway run --replace <profile>`` in background."""
    cmd = [HERMES, "gateway", "run", "--replace", profile]
    log(f"Attempting restart of gateway {profile}: {' '.join(cmd)}")
    try:
        subprocess.Popen(
            cmd,
            stdout=open(LOG_DIR / f"restart-{profile}.log", "a"),
            stderr=subprocess.STDOUT,
        )
        return True
    except Exception as exc:
        log(f"Restart command failed for {profile}: {exc}")
        return False


# --- main ------------------------------------------------------------------


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = read_state()
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    emitted_alerts: list[str] = []

    # Touch heartbeat so cron dispatcher knows this script runs
    HEARTBEAT_FILE.write_text(str(int(time.time())))

    # 1. Discover current gateways
    running = get_running_gateway_processes()
    if not running:
        # Fallback to ps-based scan if gateway list returns nothing
        ps_pids = get_running_pids_from_ps()
        running = [
            {"profile": p, "pid": pid, "alive": True}
            for p, pid in ps_pids.items()
        ]

    now_profiles = {g["profile"] for g in running if g["alive"]}
    now_alive_pids = {g["profile"]: g["pid"] for g in running if g["alive"]}

    # 2. First run: record baseline and exit quietly
    if not state["baseline_gateways"]:
        if now_profiles:
            state["baseline_gateways"] = sorted(now_profiles)
            write_state(state)
            log(f"First run — baseline gateways recorded: {sorted(now_profiles)}")
        # else: no gateways running yet — wait for next run
        return

    # 3. Check dead gateways from previous runs that may have been restarted
    for profile in list(state.get("known_dead_gateways", {})):
        if profile in now_alive_pids:
            log(f"Gateway {profile} has recovered (PID {now_alive_pids[profile]})")
            del state["known_dead_gateways"][profile]

    # 4. Compare baseline against current — report missing gateways
    baseline = set(state.get("baseline_gateways", []))
    alive = set(now_profiles)

    missing = baseline - alive
    if missing:
        for profile in sorted(missing):
            prev = state.get("known_dead_gateways", {}).get(profile, {})
            prev_count = prev.get("count", 0)
            counts = prev_count + 1

            state.setdefault("known_dead_gateways", {})[profile] = {
                "first_seen_down": prev.get("first_seen_down", now_ts),
                "count": counts,
                "last_seen_down": now_ts,
            }

            alert = (
                f"CRASH: gateway profile={profile} "
                f"down for {counts} consecutive checks "
                f"(first_seen={prev.get('first_seen_down', now_ts)})"
            )
            emitted_alerts.append(alert)
            log(alert)

            # Attempt recovery every 3rd check (avoid restart storm on flapping)
            if counts == 1 or (counts % 3 == 0):
                ok = attempt_restart(profile)
                emitted_alerts.append(
                    f"  -> restart {'OK' if ok else 'FAILED'} for {profile}"
                )

    # 5. Detect new gateways not in baseline and add them
    new_profiles = alive - baseline
    if new_profiles:
        state["baseline_gateways"] = sorted(baseline | new_profiles)
        log(f"New gateway(s) detected and added to baseline: {sorted(new_profiles)}")

    write_state(state)

    # 6. Output — silent when healthy, structured when alerting
    if emitted_alerts:
        print(f"=== NOUS CRASH WATCH — {now_ts} ===")
        for line in emitted_alerts:
            print(line)
        print(f"Alive now: {sorted(alive)} | Baseline: {sorted(baseline)}")
    else:
        # Silent when healthy — no_agent watchdog convention
        pass


if __name__ == "__main__":
    main()
