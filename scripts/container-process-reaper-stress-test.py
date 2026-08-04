#!/usr/bin/env python3
"""
container-process-reaper — synthetic stress-test harness (v2).

Verifies the reaper end-to-end against a throwaway container:

  1. Detection:      container/process listing works and CPU% is read
  2. Episode track:  high-CPU is tracked across polls (first_seen)
  3. Kill (REAL):    the runaway namespace PID is actually dead after reaping
                     (verified via `docker exec kill -0` returning nonzero),
                     NOT merely "state says action_taken" — that was the v1
                     false-positive trap (host PID ≠ namespace PID).
  4. Blast radius:   the container itself stays alive.

Usage: python3 container-process-reaper-stress-test.py
Exit 0 on all checks passed; exit 1 otherwise.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

SCRIPT_DIR = os.path.join(os.path.expanduser("~"), ".hermes", "scripts")
REAPER_SCRIPT = os.path.join(SCRIPT_DIR, "container-process-reaper.py")
TEST_CONTAINER_NAME = "reaper-test-harness"
TEST_STATE_FILE = os.path.join(tempfile.gettempdir(), "reaper_test_state.json")


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run command, return (returncode, stdout+stderr)."""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _run_quiet(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    """Same as _run but swallows noise from expected failures."""
    return _run(cmd, timeout)


def setup() -> None:
    """Create the throwaway test container (alpine, long-lived)."""
    print("[setup] Creating test container...")
    _run_quiet(["docker", "rm", "-f", TEST_CONTAINER_NAME])
    rc, out = _run([
        "docker", "run", "-d",
        "--name", TEST_CONTAINER_NAME,
        "alpine:latest",
        "/bin/sh", "-c", "sleep 300",
    ])
    if rc != 0:
        print(f"[setup] FAILED to create container: {out}")
        sys.exit(1)
    time.sleep(2)
    print("[setup] Test container running.")


def inject_stress_load() -> str | None:
    """Start a CPU-burning dd inside the container. Returns container-namespace PID."""
    print("[inject] Launching dd-based CPU burn inside test container...")
    rc, out = _run([
        "docker", "exec", TEST_CONTAINER_NAME,
        "sh", "-c",
        "nohup dd if=/dev/zero of=/dev/null > /dev/null 2>&1 & echo $!",
    ])
    if rc != 0:
        print(f"[inject] Failed to launch stress process: {out}")
        return None
    pid = out.strip()
    print(f"[inject] Stress process launched as namespace PID {pid}")
    time.sleep(3)  # let it burn and appear in docker top
    return pid


def namespace_pid_alive(pid: str) -> bool:
    """True if the given namespace PID still exists AND is not a zombie in the container."""
    rc, out = _run_quiet([
        "docker", "exec", TEST_CONTAINER_NAME,
        "sh", "-c",
        f"awk '{{print $3}}' /proc/{pid}/stat 2>/dev/null || echo GONE",
    ])
    state = out.strip()
    # GONE or Z (zombie) = dead (0% CPU). R/S/D = alive.
    return state not in ("GONE", "Z")


def run_reaper_once() -> str:
    """Execute one pass of the reaper and capture its output."""
    env = {
        **os.environ,
        "REAPER_POLL_INTERVAL": "5",
        "REAPER_STATE_FILE": TEST_STATE_FILE,
        "REAPER_CONTAINERS": TEST_CONTAINER_NAME,
        "REAPER_DURATION_SECONDS": "20",   # fast trigger for test
        "REAPER_ALERT_TARGET": "console",  # don't spam Discord during tests
    }
    proc = subprocess.run(
        [sys.executable, REAPER_SCRIPT],
        capture_output=True, text=True, timeout=60,
        env=env,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def cleanup() -> None:
    """Tear down test container and temp state."""
    print("[cleanup] Removing test container...")
    _run_quiet(["docker", "rm", "-f", TEST_CONTAINER_NAME])
    try:
        os.remove(TEST_STATE_FILE)
    except FileNotFoundError:
        pass
    print("[cleanup] Done.")


def main():
    passed = 0
    failed = 0

    def check(ok: bool, label: str):
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  [PASS] {label}")
        else:
            failed += 1
            print(f"  [FAIL] {label}")

    try:
        # ─── Step 1: Setup ───
        print("\n=== STEP 1: Setup ===")
        setup()

        # ─── Step 2: Inject load ───
        print("\n=== STEP 2: Inject stress load ===")
        stress_ns_pid = inject_stress_load()
        check(stress_ns_pid is not None, "inject CPU-burning dd process")
        if stress_ns_pid is None:
            cleanup()
            sys.exit(1)

        check(namespace_pid_alive(stress_ns_pid), "stress process alive before reaping")

        # ─── Step 3: Run reaper until it kills (or give up) ───
        print("\n=== STEP 3: Run reaper loop ===")
        killed = False
        killed_detail = ""
        num_rounds = 12
        for i in range(num_rounds):
            print(f"\n--- Reap round {i+1}/{num_rounds} ---")
            output = run_reaper_once()
            tail = output[-900:]
            print(tail)
            if "exited gracefully" in output or "SIGKILL" in output or "state Z" in output or "state GONE" in output:
                killed = True
                for ln in output.splitlines():
                    if "→ killed" in ln:
                        killed_detail = ln.strip()
                break
            time.sleep(5)

        check(killed, f"reaper reported a kill action ({killed_detail or 'n/a'})")

        # ─── Step 4: REAL verification — namespace PID must be gone ───
        print("\n=== STEP 4: Verify namespace PID actually dead ===")
        time.sleep(2)
        still_alive = namespace_pid_alive(stress_ns_pid)
        check(not still_alive, f"namespace PID {stress_ns_pid} is gone from container")
        if still_alive:
            rc, out = _run_quiet(["docker", "exec", TEST_CONTAINER_NAME,
                                  "sh", "-c", "ls /proc | grep -c '^[0-9]' 2>/dev/null || echo 0"])
            print(f"  [info] container proc count check rc={rc} out={out.strip()[:80]}")

        # ─── Step 5: Blast radius — container still alive ───
        print("\n=== STEP 5: Verify container survived ===")
        rc, out = _run(["docker", "inspect", "-f", "{{.State.Running}}", TEST_CONTAINER_NAME])
        check(rc == 0 and out.strip() == "true", "container still running after kill")

    finally:
        cleanup()

    total = passed + failed
    print(f"\n{'='*50}")
    print(f"STRESS TEST RESULTS: {passed}/{total} checks passed")
    if failed == 0:
        print("*** ALL CHECKS PASSED ***")
        sys.exit(0)
    else:
        print("*** SOME CHECKS FAILED ***")
        sys.exit(1)


if __name__ == "__main__":
    main()
