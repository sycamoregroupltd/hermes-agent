"""Opt-in in-process session-bus heartbeat.

A Hermes process bound to a session-bus session announces itself with
``busctl.py heartbeat`` on start, then every ``HERMES_SESSION_BUS_INTERVAL``
seconds from a daemon thread. No shell loop is involved.

Opt-in only: nothing happens unless BOTH ``HERMES_SESSION_BUS_SESSION`` and
``HERMES_SESSION_BUS_CTL`` are set. The ctl path is never defaulted, and the
bus is never looked up under ``~/.hermes``. A process that is not bound
therefore never touches a bus.

Only the ``heartbeat`` subcommand is ever invoked. Failures are reported on
stderr and never raised into the caller.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

_ENV_SESSION = "HERMES_SESSION_BUS_SESSION"
_ENV_CTL = "HERMES_SESSION_BUS_CTL"
_ENV_CARD = "HERMES_SESSION_BUS_CARD"
_ENV_STATE = "HERMES_SESSION_BUS_STATE"
_ENV_INTERVAL = "HERMES_SESSION_BUS_INTERVAL"

_DEFAULT_STATE = "active"
_DEFAULT_INTERVAL = 60
_SUBPROCESS_TIMEOUT = 20

_lock = threading.Lock()
_started = False


def _log(msg: str) -> None:
    print(f"session-bus-heartbeat: {msg}", file=sys.stderr, flush=True)


def _interval_seconds() -> int:
    raw = os.environ.get(_ENV_INTERVAL, "").strip()
    if not raw:
        return _DEFAULT_INTERVAL
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_INTERVAL
    return max(1, value)


def _build_command(session: str, ctl: str, state: str, card: str) -> list[str]:
    cmd = [
        sys.executable,
        ctl,
        "heartbeat",
        "--session",
        session,
        "--state",
        state,
        "--pid",
        str(os.getpid()),
    ]
    if card:
        cmd += ["--card", card]
    return cmd


def _beat(cmd: list[str]) -> None:
    try:
        subprocess.run(
            cmd,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception as exc:  # noqa: BLE001 - heartbeat must never raise
        _log(f"beat failed: {exc}")


def _loop(cmd: list[str], interval: int) -> None:
    while True:
        time.sleep(interval)
        _beat(cmd)


def maybe_start_session_bus_heartbeat() -> bool:
    """Start the heartbeat if this process is bound to a session bus.

    Returns True if a heartbeat thread was started by this call.
    """
    global _started

    session = os.environ.get(_ENV_SESSION, "").strip()
    ctl = os.environ.get(_ENV_CTL, "").strip()
    if not session or not ctl:
        return False

    with _lock:
        if _started:
            return False
        _started = True

    try:
        state = os.environ.get(_ENV_STATE, "").strip() or _DEFAULT_STATE
        card = os.environ.get(_ENV_CARD, "").strip()
        interval = _interval_seconds()
        cmd = _build_command(session, ctl, state, card)

        _beat(cmd)
        threading.Thread(
            target=_loop,
            args=(cmd, interval),
            name="session-bus-heartbeat",
            daemon=True,
        ).start()
        return True
    except Exception as exc:  # noqa: BLE001 - heartbeat must never raise
        _log(f"start failed: {exc}")
        return False
