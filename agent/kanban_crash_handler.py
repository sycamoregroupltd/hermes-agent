"""In-process crash handler for kanban workers.

A dispatched worker that dies without a terminal board tool (``kanban_complete``
/ ``kanban_block`` / ``kanban_request_review`` / ``kanban_request_changes``)
leaves its card stranded in ``running`` / ``ready`` until the dispatcher's
liveness watchdog or a protocol-violation sweep reclaims it — and even then the
card may just cycle back to ``ready`` with no durable evidence (the stale
ready-backlog aging the fleet unified-health check WARNs on). This module
installs a best-effort ``atexit`` + signal hook inside the worker process itself
so a non-graceful / no-terminal exit emits ``kanban_block(reason=
worker_crash_or_protocol_violation, note=<last_error>)`` before the process
dies, converting stale residue into a traceable, reviewable block.

Design bounds and invariants
----------------------------
* **Idempotent** — emits at most one block per process (module ``_block_emitted``
  guard). A worker that legitimately terminated (terminal tool succeeded) never
  triggers a block.
* **Kanban-scoped** — no-ops unless ``HERMES_KANBAN_TASK`` is set (a dispatcher
  owned worker). Ordinary ``-q`` runs and interactive sessions are untouched.
* **Rate-limit safe** — the dispatcher owns the provider-quota retry path
  (``EX_TEMPFAIL`` sentinel exit, ``KANBAN_RATE_LIMIT_EXIT_CODE``). We never
  block on that exit code, otherwise a 5-hour quota window would permanently
  block cards instead of letting them re-dispatch on recovery.
* **Never raises** — an exception in a signal/``atexit`` handler must not crash
  the process or corrupt its exit status. Everything is best-effort.
* **SIGKILL / ``os._exit`` caveat** — those cannot be intercepted in-process;
  they remain the dispatcher liveness watchdog's job (unchanged, and already
  verified for dead-pid reclaims).
"""

from __future__ import annotations

import os
import signal
from typing import Optional

# Author tag stamped on the traceability comment. In a dispatched worker the
# profile env is set to the assignee; fall back to a stable tag so the comment
# is attributable even when the profile env is absent.
_CRASH_AUTHOR = "kanban-crash-handler"

# Module state. ``_terminal_sent`` is flipped by the kanban terminal tool
# handlers on success; the atexit/signal hooks read it to decide whether the
# worker actually terminated before the process left.
_terminal_sent: bool = False
_block_emitted: bool = False
_atexit_registered: bool = False
_exit_code: Optional[int] = None
_last_error: Optional[str] = None
# Cached reference to the kanban DB module. Imported eagerly in ``install()``
# (while the interpreter is alive) because the atexit hook runs during
# interpreter shutdown, when a lazy ``from hermes_cli import kanban_db`` can
# fail silently — leaving the card un-blocked with no evidence.
_kb = None


def _get_kb():
    """Return the cached kanban_db module, importing it if not yet cached."""
    global _kb
    if _kb is None:
        from hermes_cli import kanban_db as _m
        _kb = _m
    return _kb


def _active() -> bool:
    """True when this process is a dispatcher-owned kanban worker."""
    return bool((os.environ.get("HERMES_KANBAN_TASK") or "").strip())


def _worker_run_id() -> Optional[int]:
    raw = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    if raw.isdigit():
        return int(raw)
    return None


def mark_terminal_sent() -> None:
    """Record that a terminal kanban tool succeeded in this process.

    Called by ``kanban_complete`` / ``kanban_block`` / ``kanban_request_review``
    / ``kanban_request_changes`` on their success paths. Once set, the
    crash handler treats any subsequent process exit as graceful.
    """
    global _terminal_sent
    _terminal_sent = True


def record_worker_exit(code: Optional[int], error: Optional[str] = None) -> None:
    """Capture the worker's intended exit code / last error for the atexit hook.

    ``code == KANBAN_RATE_LIMIT_EXIT_CODE`` (the ``EX_TEMPFAIL`` provider-quota
    sentinel) is the one exit the crash handler must NOT block — the dispatcher
    owns that retry path. Everything else with no terminal signal gets blocked.
    """
    global _exit_code, _last_error
    if code is not None:
        _exit_code = int(code)
    if error:
        _last_error = str(error)


def _emit_block(note: str) -> None:
    """Best-effort, once-only ``kanban_block`` + traceability comment."""
    global _block_emitted
    if _block_emitted or not _active():
        return
    _block_emitted = True  # set before attempting — never emit twice
    tid = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not tid:
        return
    try:
        kb = _get_kb()

        conn = kb.connect()
        try:
            reason = f"worker_crash_or_protocol_violation: {note}"
            ok = kb.block_task(
                conn,
                tid,
                reason=reason,
                kind="capability",
                expected_run_id=_worker_run_id(),
            )
            if not ok:
                # Task already terminal (blocked/done/reviewed) or not in a
                # blockable state — nothing to do, and no comment to write.
                return
            try:
                exit_desc = (
                    f"exit_code={_exit_code}" if _exit_code is not None else "exit_code=unknown"
                )
                body = (
                    "crash-handler: worker exited without a terminal kanban signal.\n"
                    f"reason=worker_crash_or_protocol_violation\n"
                    f"{exit_desc}\n"
                    f"last_error={_last_error or 'none'}\n"
                    f"note={note}"
                )
                kb.add_comment(conn, tid, author=_CRASH_AUTHOR, body=body)
            except Exception:
                pass  # traceability comment is best-effort; the block already landed
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        # Never let a crash-handler failure corrupt the process exit.
        pass


def atexit_crash_block() -> None:
    """``atexit`` hook: block when a kanban worker exits without a terminal signal.

    Skips when a terminal tool succeeded (``_terminal_sent``) and when the exit
    was the provider-quota ``EX_TEMPFAIL`` sentinel (dispatcher owns that retry).
    """
    if not _active():
        return
    if _terminal_sent:
        return
    try:
        kb = _get_kb()
        if _exit_code == kb.KANBAN_RATE_LIMIT_EXIT_CODE:
            return  # dispatcher-owned retry path; never block
    except Exception:
        pass
    _emit_block("atexit: worker exited (rc=%s) without a terminal kanban tool" % (
        _exit_code if _exit_code is not None else "?"
    ))


def emit_block_on_signal(signum: int) -> None:
    """Signal hook: block a kanban worker that is being killed mid-run.

    Called from the CLI signal handler BEFORE it unwinds / ``os._exit``s, so the
    terminal signal lands on the board even though ``atexit`` is skipped on the
    ``os._exit`` path. Best-effort; the process still dies with the signal.
    """
    if not _active():
        return
    if _terminal_sent:
        return
    name = signal.Signals(signum).name if signum in signal.valid_signals() else str(signum)
    _emit_block(f"received signal {name} ({signum}) before a terminal kanban tool")


def install() -> None:
    """Idempotently register the ``atexit`` hook. No-ops for non-kanban processes."""
    global _atexit_registered
    if _atexit_registered:
        return
    _atexit_registered = True
    import atexit

    # Force-load the full board-write import chain NOW, while the interpreter is
    # alive. ``kb.connect()`` transitively imports ``hermes_state`` →
    # ``agent.memory_manager`` → ``concurrent.futures``/``threading``, whose
    # module import calls ``threading._register_atexit`` — that raises
    # ``RuntimeError: can't register atexit after shutdown`` if the import
    # happens for the first time from inside the atexit hook. By probing a
    # connect here, every module in that chain is already in ``sys.modules`` by
    # the time the hook runs, so the hook's ``kb.connect()`` reuses them without
    # re-importing. Gated on ``_active()`` so ordinary ``-q`` runs pay nothing.
    # Best-effort; a failure here only means the atexit block may not land for
    # this process (the dispatcher liveness watchdog still covers dead-pid
    # reclaims).
    if _active():
        try:
            kb = _get_kb()
            _probe = kb.connect()
            try:
                _probe.close()
            except Exception:
                pass
        except Exception:
            pass
    atexit.register(atexit_crash_block)


__all__ = [
    "install",
    "mark_terminal_sent",
    "record_worker_exit",
    "atexit_crash_block",
    "emit_block_on_signal",
]
