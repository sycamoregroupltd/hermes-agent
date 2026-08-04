#!/usr/bin/env python3
"""
Container Process Reaper — detect & kill runaway processes in production containers.

Detects resource-hogging processes inside target containers, tracks sustained
high-CPU episodes across polls, and issues namespace-aware kills via
`docker exec --user root kill <pid>` so host EPERM constraints don't apply
(host-side kill of a container-namespace process owned by a different uid fails).

PID-namespace correctness: `docker top` reports the HOST PID (read from host
/proc). A kill issued via `docker exec` operates in the CONTAINER's PID
namespace, so the host PID must be mapped to the namespace PID via the NSpid
field of /proc/<hostpid>/status (last value = PID inside the container). This
is what makes the kill effective where host-side `kill` silently returns EPERM
or "No such process". Several production containers ship no ps binary, so we
depend on `docker top` + /proc rather than exec'ing ps into each container.

Fail-open: any failure (bad state file, missing container, API error) logs a
warning and continues — legitimate work is never blocked.

Configuration (all via environment variables, none hardcoded):
    REAPER_CONTAINERS            Comma-separated container names (default:
                                 sycodetrading-server,jarvis-trader,sycodetrading-web,
                                 market-data-gateway,order-router-rs)
    REAPER_CPU_THRESHOLD         Percent (default: 80)
    REAPER_DURATION_SECONDS      Continuous high-CPU before action (default: 60)
    REAPER_STATE_FILE            Persistent state path
                                 (default: ~/.hermes/scripts/container_reaper_state.json)
    REAPER_ALERT_TARGET          Alert destination (default: discord:#critical-alerts)
                                 Uses `hermes send -q -t <target>` — the fleet's
                                 canonical alert path (same as blocked_task_notifier).
    REAPER_DRY_RUN               Set to "1" to log findings but never kill (safe test mode).

Deploy form: no-agent cron (every 2m) via the Hermes cron store, or manual
invocation for debugging. A cron tick is one full scan of all containers.
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("container-reaper")

# ---------------------------------------------------------------------------
# Configuration — all overrideable via env
# ---------------------------------------------------------------------------
CONTAINER_LIST_ENV = os.environ.get("REAPER_CONTAINERS", "")
CONTAINER_NAMES = [
    c.strip() for c in CONTAINER_LIST_ENV.split(",") if c.strip()
] or [
    "sycodetrading-server",
    "jarvis-trader",
    "sycodetrading-web",
    "market-data-gateway",
    "order-router-rs",
]

CPU_THRESHOLD = float(os.environ.get("REAPER_CPU_THRESHOLD", "80"))
DURATION_THRESHOLD = int(os.environ.get("REAPER_DURATION_SECONDS", "60"))
POLL_INTERVAL = int(os.environ.get("REAPER_POLL_INTERVAL_SECONDS", "30"))
STATE_FILE = Path(
    os.environ.get(
        "REAPER_STATE_FILE",
        str(Path.home() / ".hermes" / "scripts" / "container_reaper_state.json"),
    )
)
ALERT_TARGET = os.environ.get("REAPER_ALERT_TARGET", "discord:#critical-alerts")
DRY_RUN = os.environ.get("REAPER_DRY_RUN", "0") == "1"
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")

# Max age for stale entries in state before cleanup
STATE_TTL_SECONDS = 3600


# ---------------------------------------------------------------------------
# State management (JSON file)
# ---------------------------------------------------------------------------
def _read_state() -> dict:
    """Load state from disk. Return empty dict on missing/corrupt file."""
    try:
        with open(STATE_FILE, "r") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, PermissionError, OSError):
        return {}


def _write_state(state: dict) -> None:
    """Atomically write state back to disk."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        tmp.replace(STATE_FILE)
    except OSError as exc:
        log.warning("Could not persist state %s: %s", STATE_FILE, exc)


def _now_epoch() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# PID namespace mapping — host PID -> container namespace PID
# ---------------------------------------------------------------------------
# `docker top` reports the HOST PID (read from host /proc), but a kill issued
# via `docker exec` operates in the CONTAINER's PID namespace. The NSpid field
# of /proc/<hostpid>/status lists the PID as seen by each nested namespace; the
# last value is the PID inside the container's own namespace. We must kill that
# namespace PID, because the host PID does not exist inside the container
# (killing it returns "No such process"). This is the core namespace-awareness
# that host-side `kill` lacks and that container-internal `ps` cannot provide
# (several production containers ship no ps binary).

def host_to_namespace_pid(host_pid: int) -> int | None:
    """Map a host PID to the PID inside the container's PID namespace (last NSpid value)."""
    try:
        with open(f"/proc/{host_pid}/status") as fh:
            for line in fh:
                if line.startswith("NSpid"):
                    vals = line.split()[1:]  # from outer to inner namespace
                    if vals:
                        return int(vals[-1])
    except (FileNotFoundError, PermissionError, ValueError, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------
def _docker_run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess | None:
    """Run a docker command, returning CompletedProcess or None on failure."""
    try:
        log.debug("Running: %s", " ".join(["docker"] + args))
        return subprocess.run(
            ["docker"] + args, capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.warning("docker %s failed: %s", " ".join(args), exc)
        return None


def container_running(name: str) -> bool:
    """True if the named container exists and is running."""
    res = _docker_run(["inspect", "-f", "{{.State.Running}}", name], timeout=10)
    return bool(res and res.returncode == 0 and res.stdout.strip() == "true")


def get_container_processes(container: str) -> list[dict]:
    """
    Get current process snapshot for a container.

    Uses `docker top <c> -o pid,pcpu,pmem,user,args`, which reads host /proc
    and works even in containers that ship no `ps` binary (e.g. bun/node
    production images). The PID reported by docker top is the HOST PID; we map
    it to the container-namespace PID via the NSpid field for use in kills.

    Returns list of dicts: {host_pid, ns_pid, cpu, mem, user, command}
    """
    processes: list[dict] = []

    res = _docker_run(
        ["top", container, "-o", "pid,pcpu,pmem,user,args"],
        timeout=20,
    )
    if res and res.returncode == 0 and res.stdout.strip():
        lines = res.stdout.strip().splitlines()
        for line in lines[1:]:  # skip header (docker top may or may not emit one)
            parts = line.split(None, 4)  # pid pcpu pmem user args
            if len(parts) >= 5:
                try:
                    host_pid = int(parts[0])
                    processes.append(
                        {
                            "host_pid": host_pid,
                            "ns_pid": host_to_namespace_pid(host_pid),
                            "cpu": float(parts[1]),
                            "mem": float(parts[2]),
                            "user": parts[3],
                            "command": parts[4],
                        }
                    )
                except (ValueError, IndexError):
                    continue
        if processes:
            return processes

    return processes


# ---------------------------------------------------------------------------
# Kill logic — namespace-aware via docker exec --user root
# ---------------------------------------------------------------------------
def _ns_pid_state(container: str, ns_pid: int) -> str:
    """
    Return the process state char inside the container: R/S/D/Z/T or 'GONE'.
    Reads /proc/<ns_pid>/stat field 3 inside the container namespace.
    """
    res = _docker_run(
        [
            "exec", "--user", "root", container, "sh", "-c",
            f"awk '{{print $3}}' /proc/{ns_pid}/stat 2>/dev/null || echo GONE",
        ],
        timeout=10,
    )
    if res is None or res.returncode != 0:
        return "GONE"
    out = (res.stdout or "").strip()
    return out if out else "GONE"


def kill_pid_in_container(container: str, ns_pid: int) -> tuple[bool, str]:
    """
    Kill a PID inside the container's PID namespace. Returns (success, detail).

    `ns_pid` is the PID as seen inside the container (from NSpid mapping).
    Sequence: SIGTERM → brief wait → verify state → SIGKILL if still live.
    A process is considered killed when it is GONE or a zombie (Z) — both
    consume zero CPU, which is the reaper's actual goal. R/S/D means still live.

    Operates via `docker exec --user root` because host-side `kill` is
    ineffective for container-namespace processes (EPERM when uid differs,
    and the host PID does not exist inside the namespace).
    """
    # Step 1: graceful SIGTERM
    res = _docker_run(
        [
            "exec", "--user", "root", container, "sh", "-c",
            f"kill -TERM {ns_pid} 2>/dev/null; echo rc=$?",
        ],
        timeout=10,
    )
    if res is None:
        return False, "docker exec unavailable for SIGTERM"
    term_rc = (res.stdout or "").strip().replace("rc=", "") or "?"
    time.sleep(3)

    # Step 2: check state — GONE or Z (zombie) both mean it stopped consuming CPU
    state = _ns_pid_state(container, ns_pid)
    if state in ("GONE", "Z"):
        return True, f"SIGTERM (rc={term_rc}) — state {state} (dead, 0% CPU)"

    # Step 3: force SIGKILL
    res2 = _docker_run(
        [
            "exec", "--user", "root", container, "sh", "-c",
            f"kill -KILL {ns_pid} 2>/dev/null; echo rc=$?",
        ],
        timeout=10,
    )
    if res2 is None:
        return False, "docker exec unavailable for SIGKILL"
    kill_rc = (res2.stdout or "").strip().replace("rc=", "") or "?"

    # Verify it is really gone (or zombie)
    state2 = _ns_pid_state(container, ns_pid)
    dead = state2 in ("GONE", "Z")
    return dead, f"SIGTERM({term_rc}) → SIGKILL({kill_rc}) → state {state2} ({'dead' if dead else 'STILL ALIVE'})"


# ---------------------------------------------------------------------------
# Alert dispatch — uses fleet's canonical `hermes send` path
# ---------------------------------------------------------------------------
def send_alert(message: str, severity: str = "HIGH") -> bool:
    """
    Send an alert to the configured destination via `hermes send`,
    OR log locally when the target is "console" (for tests / dry-run).
    Matches the existing blocked_task_notifier alert path (discord:#critical-alerts).
    Returns True if sent successfully; never raises.
    """
    if ALERT_TARGET.lower() == "console":
        log.warning("REAPER ALERT [%s]:\n%s", severity, message)
        return True
    subject = "⛔ Container Reaper: runaway process killed"
    try:
        result = subprocess.run(
            [HERMES_BIN, "send", "-q", "-t", ALERT_TARGET, "-s", subject, message],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode != 0:
            log.warning("Alert send failed (rc=%s): %s", result.returncode,
                        (result.stderr or result.stdout or "").strip()[:300])
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.warning("Alert send exception: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Main reaper loop
# ---------------------------------------------------------------------------
def main() -> int:
    start_time = _now_epoch()
    mode = "DRY-RUN (no kills)" if DRY_RUN else "live"
    log.info(
        "Container Process Reaper (%s) — poll=%ds, CPU>=%.0f%% for >=%ds",
        mode, POLL_INTERVAL, CPU_THRESHOLD, DURATION_THRESHOLD,
    )
    log.info("Monitoring: %s", ", ".join(CONTAINER_NAMES))

    state = _read_state()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Phase 1: prune stale entries (TTL expiry)
    cutoff = start_time - STATE_TTL_SECONDS
    stale_keys = [k for k, v in state.items() if v.get("last_updated_epoch", 0) < cutoff]
    for k in stale_keys:
        del state[k]

    # Phase 2: scan each container
    seen_keys: set[str] = set()
    findings: list[dict] = []
    alerts_sent = 0

    for container in CONTAINER_NAMES:
        if not container_running(container):
            log.warning("Container '%s' not running — skipping", container)
            continue

        procs = get_container_processes(container)
        if not procs:
            log.debug("No process data for '%s'", container)
            continue

        for proc in procs:
            host_pid = proc.get("host_pid")
            ns_pid = proc.get("ns_pid")
            cpu = proc["cpu"]
            # Key by namespace PID (stable inside the container); fall back to host PID.
            pid = ns_pid if ns_pid is not None else host_pid
            if pid is None:
                continue
            key = f"{container}/{pid}"
            seen_keys.add(key)

            entry = state.get(key, {})

            if cpu >= CPU_THRESHOLD:
                first_seen_epoch = entry.get("first_seen_epoch")
                if first_seen_epoch is None:
                    # New high-CPU observation
                    entry = {
                        "first_seen_at": now_str,
                        "first_seen_epoch": start_time,
                        "container": container,
                        "host_pid": host_pid,
                        "pid": pid,  # namespace PID (kill target)
                        "user": proc.get("user", "?"),
                        "command": proc.get("command", ""),
                        "peak_cpu": cpu,
                        "last_updated_epoch": start_time,
                        "action_taken": None,
                    }
                    state[key] = entry
                    log.info(
                        "%s — PID %d (host %s) new high-CPU observation at %.1f%%",
                        container, pid, host_pid, cpu,
                    )
                    continue

                # Continuing high-CPU — refresh peak & timestamp
                entry["last_updated_epoch"] = start_time
                entry["peak_cpu"] = max(float(entry.get("peak_cpu", cpu)), cpu)
                entry["host_pid"] = host_pid
                state[key] = entry
                elapsed = start_time - first_seen_epoch

                if elapsed >= DURATION_THRESHOLD and entry.get("action_taken") is None:
                    # Time to act!
                    log.warning(
                        "%s — PID %d (host %s) at %.1f%% CPU for %.0fs (>=%ds threshold)",
                        container, pid, host_pid, cpu, elapsed, DURATION_THRESHOLD,
                    )
                    if ns_pid is None:
                        log.warning(
                            "%s — cannot kill PID %d: no namespace PID mapping "
                            "(fail-open, alert only)", container, pid,
                        )
                        success, detail = False, "NO_NS_PID_MAPPING (alert only)"
                    elif DRY_RUN:
                        detail = "DRY-RUN (no kill)"
                        success = True
                    else:
                        success, detail = kill_pid_in_container(container, ns_pid)
                    entry["action_taken"] = detail
                    entry["killed_at"] = now_str
                    entry["killed_epoch"] = start_time

                    alert_msg = (
                        f"**⛔ Container Reaper: {'[DRY-RUN] would kill' if DRY_RUN else 'killed'} PID `{pid}`**\n\n"
                        f"• **Container:** `{container}`\n"
                        f"• **PID:** `{pid}` (host PID: `{host_pid}`) (user: `{proc.get('user', '?')}`)\n"
                        f"• **Command:** `{proc.get('command', '').strip()[:200]}`\n"
                        f"• **CPU:** `{cpu:.1f}%` (peak: `{entry['peak_cpu']:.1f}%`)\n"
                        f"• **Duration:** `{elapsed:.0f}s` (threshold: {DURATION_THRESHOLD}s)\n"
                        f"• **Action:** `{detail}`\n"
                        f"• **Alert channel:** `{ALERT_TARGET}`\n"
                    )
                    if send_alert(alert_msg):
                        alerts_sent += 1
                    findings.append(
                        {
                            "container": container,
                            "pid": pid,
                            "cpu": cpu,
                            "duration_s": round(elapsed, 1),
                            "status": "killed" if success else "alert_only",
                            "detail": detail,
                        }
                    )
            else:
                # Below threshold — reset the continuous-episode clock. A later
                # spike starts a fresh episode (requirement: ">80% CPU for
                # >60s *continuously*"). But keep completed action history
                # (action_taken) until TTL prune so operators can audit kills.
                if key in state and state[key].get("action_taken") is None:
                    del state[key]

    # Phase 3: drop entries whose process is no longer observed (pid gone / container restarted)
    for key in list(state.keys()):
        if key not in seen_keys:
            # Keep completed action history briefly for audit, drop others
            entry = state[key]
            if entry.get("action_taken") or entry.get("first_seen_epoch", 0) < start_time - STATE_TTL_SECONDS:
                del state[key]

    _write_state(state)

    # Summary
    if findings:
        summary_lines = [f"Reaper run complete — {len(findings)} action(s), {alerts_sent} alert(s) sent:"]
        for f in findings:
            status_emoji = "✅" if f["status"] == "killed" else "⚠️"
            summary_lines.append(
                f"  {status_emoji} {f['container']} PID {f['pid']} @ {f['cpu']}% "
                f"(ran {f['duration_s']}s) → {f['status']} [{f['detail']}]"
            )
        log.info("\n%s", "\n".join(summary_lines))
    else:
        log.info("Reaper run complete — all monitored processes within normal parameters")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.info("Interrupted — exiting cleanly")
        sys.exit(0)
    except Exception as exc:
        log.error("FATAL: %s — fail-open: processes left running", exc)
        sys.exit(1)
