#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Elon kanban watchdog — governor's BUSY/IDLE/READY gatekeeper.

Hardened on behalf of kanban task t_feb25dad:
    * bounded retry/backoff for transient subprocess failures, including
      transient [Errno 11] EAGAIN / 'Resource temporarily unavailable'.
    * deterministic WATCHDOG_UNAVAILABLE marker when resilience is exhausted,
      so the governor cron can detect skipped-with-evidence cycles instead
      of suprising silent drops.
"""
import json
import os
import random
import subprocess
import sys
import time
import traceback as _traceback
from pathlib import Path

WATCHDOG_SCRIPT_PATH = Path(__file__)
WATCHDOG_DIR = WATCHDOG_SCRIPT_PATH.resolve().parent
# Testability hook: allow the marker location to be overridden (e.g. a throwaway
# tmp dir in the fault-injection test). Default behaviour is unchanged — the
# marker is written next to this script. No effect on decision logic.
_UNAVAILABLE_PATH_ENV = os.environ.get("WATCHDOG_UNAVAILABLE_PATH")
UNAVAILABLE_MARKER = (
    Path(_UNAVAILABLE_PATH_ENV)
    if _UNAVAILABLE_PATH_ENV
    else WATCHDOG_DIR / "WATCHDOG_UNAVAILABLE"
)
UNAVAILABLE_MARKER_MAX_AGE_S = 900
_CMD_ENV = {**os.environ, "HOME": os.path.expanduser("~")}
_RUNNER = subprocess.run
TRANSIENT_RC = {11, 28, 35}
_EAGAIN = getattr(__import__("errno"), "EAGAIN", 11)
_TRANSIENT_ERRNO = {
    _EAGAIN,
    getattr(__import__("errno"), "EWOULDBLOCK", _EAGAIN),
    getattr(__import__("errno"), "ENOMEM", 12),
}
STATE_SIGNAL = "STATE-SIGNAL-DEGRADED: watchdog unavailable after retries"
FALLBACK_SIGNAL = "WATCHDOG_UNAVAILABLE"
# Distinguishable exit code the governor cron / fleet job store detects: a
# WATCHDOG_UNAVAILABLE cycle must NOT surface as last_status=ok (silent drop).
# The durable WATCHDOG_UNAVAILABLE marker + STATE-SIGNAL-DEGRADED stdout line
# are still emitted first, so the governor (context_from) and any human reading
# the run see the deterministic degraded signal; 42 makes the job store itself
# record a non-ok outcome instead of masking the black hole.
WATCHDOG_UNAVAILABLE_RC = 42
SUBCOMMAND = ["hermes", "kanban", "--board", "jarvis-os", "list", "--assignee", "elon", "--json"]
CMD_ENV = {**os.environ, "HOME": os.path.expanduser("~")}
MAX_ATTEMPTS = 3


def _truncate(value: str | None, limit: int = 200) -> str:
    value = (value or "").strip()
    if len(value) > limit:
        return value[:limit - 1] + "…"
    return value


def _appears_transient(returncode: int | None, stderr: str, stdout: str, exc: BaseException | None) -> bool:
    if returncode is not None and returncode in TRANSIENT_RC:
        return True
    text = f"{stderr}\n{stdout}"
    if "Resource temporarily unavailable" in text:
        return True
    if "[Errno 11]" in stderr:
        return True
    if isinstance(exc, BlockingIOError | OSError) and getattr(exc, "errno", None) in _TRANSIENT_ERRNO:
        return True
    return False


def _transient_label(returncode: int | None, stderr: str, exc: BaseException | None) -> str:
    if exc is not None:
        return str(exc)
    if "[Errno 11]" in stderr:
        return "[Errno 11] Resource temporarily unavailable"
    return f"transient subprocess failure rc={returncode}"


def _write_unavailable(reason: str, elapsed_s: float, suppressed: bool = False) -> None:
    try:
        UNAVAILABLE_MARKER.write_text(
            json.dumps({
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "reason": _truncate(reason, 240),
                "watched_command": SUBCOMMAND,
                "elapsed_s": round(elapsed_s, 3),
                "suppressed_exception": suppressed,
                "state_signal": STATE_SIGNAL,
                "fallback_marker": FALLBACK_SIGNAL,
            }),
            encoding="utf-8",
        )
    except OSError:
        pass


def _run_one() -> dict:
    try:
        proc = _RUNNER(
            SUBCOMMAND,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=_CMD_ENV,
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "exc": None,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else _truncate(str(exc.stdout)),
            "stderr": exc.stderr if isinstance(exc.stderr, str) else _truncate(str(exc.stderr)),
            "exc": None,
            "timed_out": True,
        }
    except BaseException as exc:
        return {
            "returncode": None,
            "stdout": "",
            "stderr": _truncate(_traceback.format_exc()),
            "exc": exc,
            "timed_out": False,
        }


def _classify_tasks(tasks: list[dict]) -> int:
    todo_or_ready = [t for t in tasks if t.get("status") in ("todo", "ready")]
    running = [t for t in tasks if t.get("status") == "running"]
    blocked = [t for t in tasks if t.get("status") == "blocked"]

    if running:
        print(f"ELON BUSY: {len(running)} task(s) running — skip")
        return 0

    for task in todo_or_ready[:3]:
        print(f"READY: {task['id']} | {task.get('title', '')[:80]} | priority={task.get('priority', 0)}")

    if blocked:
        print(f"BLOCKED: {len(blocked)} task(s)")

    if not todo_or_ready and not running:
        print("ELON IDLE: no tasks, self-improvement window open")
    return 0


def main() -> int:
    start = time.monotonic()
    last_run: dict = {}
    last_label = ""
    attempt = 0
    delay = 1.0

    try:
        while attempt < MAX_ATTEMPTS:
            if attempt:
                jitter_sleep = round(random.uniform(0.0, 0.5), 3)
                time.sleep(delay + jitter_sleep)
            attempt += 1
            last_run = _run_one()
            stdout = last_run.get("stdout", "") or ""
            stderr = last_run.get("stderr", "") or ""
            exc = last_run.get("exc")
            rc = last_run.get("returncode")

            if rc == 0 and not stderr and not exc:
                tasks = json.loads(stdout) if stdout else []
                return _classify_tasks(tasks)

            transient = _appears_transient(rc, stderr, stdout, exc)
            if not transient:
                print(
                    "STATE-SIGNAL-DEGRADED: watchdog command errored "
                    f"(rc={rc}, stderr={_truncate(stderr, 200)})"
                )
                return 0

            last_label = _transient_label(rc, stderr, exc)
            delay = min(2 * (2 ** max(attempt - 1, 0)), 8.0)

        _write_unavailable(last_label or FALLBACK_SIGNAL, time.monotonic() - start, suppressed=last_run.get("exc") is not None)
        print(STATE_SIGNAL)
        print(f"{FALLBACK_SIGNAL} marker after {attempt} attempts: {_truncate(last_label, 240)}")
        return WATCHDOG_UNAVAILABLE_RC
    except BaseException as exc:
        _write_unavailable(str(exc), time.monotonic() - start, suppressed=True)
        print(STATE_SIGNAL)
        print(f"{FALLBACK_SIGNAL} marker after exception: {exc}")
        return WATCHDOG_UNAVAILABLE_RC


if __name__ == "__main__":
    sys.exit(main())
