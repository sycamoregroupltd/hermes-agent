"""Crash handler for kanban workers: emit ``kanban_block`` on non-graceful exit.

A dispatcher-spawned worker must end with a terminal board signal
(``kanban_complete`` / ``kanban_block`` / ``kanban_request_review`` /
``kanban_request_changes``). When a worker instead exits non-gracefully —
a clean ``rc=0`` with no terminal call, a caught signal, or an unhandled
exception — the task would otherwise sit in ``running`` until the dispatcher's
next ``detect_crashed_workers`` tick reclaims it (up to ~60s later), and for
protocol violations it cycles a bounded retry streak that can age the
ready-backlog for days under provider starvation.

This module arms an ``atexit`` + signal handler that, on such an exit where no
terminal kanban signal was sent, synchronously transitions the task to
``blocked`` (kind ``transient``) with a traceable comment and reason
``worker_crash_or_protocol_violation`` — so the card reaches a terminal state
with evidence instead of lingering as residue.

Boundaries (fail-closed, best-effort, never raises):

- Only acts when ``HERMES_KANBAN_TASK`` is set (a dispatcher-spawned worker),
  and can be disabled with ``HERMES_KANBAN_CRASH_HANDLER=0``.
- Only blocks a task that is still ``running``/``ready`` — ``block_task`` is
  idempotent, so a task already completed / blocked / moved to review is a
  no-op (we never clobber a legitimate terminal transition).
- Skips the rate-limit sentinel exit (``KANBAN_RATE_LIMIT_EXIT_CODE``): the
  dispatcher deliberately requeues quota-walled workers without counting a
  failure, so the crash handler must not convert that into a block.
- Every call is wrapped so a broken handler can never change the process exit
  code or wedge interpreter shutdown.
- SIGKILL and ``os._exit()`` bypass ``atexit`` and signal handlers entirely;
  those uncatchable deaths remain the dispatcher's ``detect_crashed_workers``
  responsibility (pid-not-alive reclaim → auto-block), which already emits a
  ``gave_up`` event. This module covers the catchable non-graceful exits.
"""

from __future__ import annotations

import atexit
import os
import sys
import traceback
from typing import Optional

_KANBAN_CRASH_HANDLER_DISABLE = ("0", "false", "no", "off")


def _handler_disabled() -> bool:
    env = os.environ.get("HERMES_KANBAN_CRASH_HANDLER")
    return env is not None and env.strip().lower() in _KANBAN_CRASH_HANDLER_DISABLE


def kanban_crash_handler_enabled() -> bool:
    """True when this process is a kanban worker and the handler is not disabled."""
    if _handler_disabled():
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)


# Set by the cli rate-limit branch before ``sys.exit(KANBAN_RATE_LIMIT_EXIT_CODE)``
# so the atexit hook does not convert a legitimate quota-wall requeue into a block.
_rate_limit_exit = False


def mark_rate_limit_exit() -> None:
    """Record that this worker is exiting on the provider-quota sentinel.

    The dispatcher maps the rate-limit exit code to a clean requeue (no failure
    counted), so the crash handler must skip blocking. Call once before
    ``sys.exit(KANBAN_RATE_LIMIT_EXIT_CODE)``.
    """
    global _rate_limit_exit
    _rate_limit_exit = True


_armed = False


def arm_kanban_crash_handler() -> None:
    """Register the atexit hook (idempotent). No-op unless this is a worker.

    Also eagerly imports the board-DB module and probe-connects NOW, while the
    interpreter is alive. ``kanban_db.connect()`` transitively imports
    ``concurrent.futures`` / ``threading``; deferring that import to the atexit
    hook can raise ``can't register atexit after shutdown`` when the interpreter
    is mid-teardown (the exact hazard the parallel t_498c8b86 implementation
    identified). Eager import keeps the atexit hook dependency-free.
    """
    global _armed
    if _armed:
        return
    if not kanban_crash_handler_enabled():
        return
    try:
        # Warm the import chain while the interpreter is fully alive so the
        # atexit hook never has to import anything at teardown time.
        import hermes_cli.kanban_db as _kb  # noqa: F401
        _conn = _kb.connect()
        try:
            _conn.close()
        except Exception:
            pass
    except Exception:
        # Best-effort warm-up; the hook still tries to import defensively.
        pass
    atexit.register(_on_atexit)
    _armed = True


def _current_run_id() -> Optional[int]:
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _last_error_note() -> str:
    """Capture the active exception (unhandled crash) or a generic note."""
    exc_type, exc, _tb = sys.exc_info()
    if exc_type is None:
        return "worker exited without a terminal kanban signal (worker_crash_or_protocol_violation)"
    if exc_type is SystemExit:
        code = exc.code if exc is not None else None
        return (
            f"worker exited (code={code}) without a terminal kanban signal "
            f"(worker_crash_or_protocol_violation)"
        )
    try:
        body = "".join(traceback.format_exception(exc_type, exc, _tb))
    except Exception:
        body = str(exc)
    return (
        f"worker crashed with {exc_type.__name__} before a terminal kanban "
        f"signal (worker_crash_or_protocol_violation): {body[:600]}"
    )


def _emit_crash_block(note: Optional[str] = None) -> bool:
    """Best-effort synchronous block of the worker's own task. Never raises.

    Returns True if the task was transitioned to a terminal blocked state,
    False when it was already terminal (or not blockable).
    """
    if not kanban_crash_handler_enabled():
        return False
    if _rate_limit_exit:
        return False
    tid = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not tid:
        return False
    reason = "worker_crash_or_protocol_violation"
    if not note:
        note = _last_error_note()
    try:
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
        try:
            ok = kb.block_task(
                conn, tid,
                reason=reason,
                kind="transient",
                expected_run_id=_current_run_id(),
            )
            if not ok:
                # Already terminal (completed / blocked / review / not blockable).
                return False
            author = (os.environ.get("HERMES_PROFILE") or "dispatcher").strip()
            body = (
                f"[crash-handler] {reason} — {note}\n\n"
                f"pid={os.getpid()} — worker exited non-gracefully without a "
                f"terminal kanban signal. Auto-blocked with evidence by the "
                f"crash handler; a recovery/review card may pick this up."
            )
            try:
                kb.add_comment(conn, tid, author or "dispatcher", body)
            except Exception:
                # Comment is best-effort; the block transition is the durable part.
                pass
            return True
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return False


def emit_kanban_crash_block(note: Optional[str] = None) -> bool:
    """Public entry for the cli signal handler: block before process death."""
    return _emit_crash_block(note=note)


def _on_atexit() -> None:
    """atexit hook: fire the crash block for catchable non-graceful crashes.

    Deliberately fires on an *unhandled exception* (a real crash), NOT on a
    clean interpreter shutdown. A clean ``rc=0``-with-no-terminal exit is a
    protocol violation that the dispatcher handles with its own bounded retry
    (~96% of such tasks complete on a later run), so the crash handler must not
    pre-empt that retry budget by blocking on the first clean exit.
    """
    try:
        exc_type, exc, _tb = sys.exc_info()
        # SystemExit(0) is a clean exit; SystemExit(nonzero) is a deliberate
        # code path (e.g. usage error) the dispatcher classifies itself.
        if exc_type is None or (exc_type is SystemExit):
            return
        _emit_crash_block()
    except Exception:
        pass


__all__ = [
    "arm_kanban_crash_handler",
    "emit_kanban_crash_block",
    "kanban_crash_handler_enabled",
    "mark_rate_limit_exit",
]
