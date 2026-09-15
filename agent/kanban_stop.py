"""Turn-end guard for kanban workers, which must end with a terminal board call.

Some models narrate the next step and stop with no tool calls; Hermes treats that as a
clean exit → ``rc=0`` → dispatcher ``protocol_violation``. Policy-only: return a bounded
synthetic nudge so the loop continues instead of exiting.

The nudge is only ever emitted while the BOARD still shows this worker's run in flight
(:func:`_run_still_open`). Message history cannot answer that question — a refused terminal
call looks identical to a successful one, and a review-lane worker that already handed the
card back with ``kanban_request_changes`` has no reachable terminal left. The dispatcher
books ``protocol_violation`` only for a task that is still ``running`` with a live worker
pid, so anything else must not be nudged to terminate again.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional


_TERMINAL_KANBAN_TOOLS = frozenset({"kanban_complete", "kanban_block"})

_DEFAULT_MAX_ATTEMPTS = 2


def kanban_stop_nudge_enabled() -> bool:
    """On when ``HERMES_KANBAN_TASK`` is set, unless ``HERMES_KANBAN_STOP_NUDGE`` disables it."""
    if (os.environ.get("HERMES_KANBAN_STOP_NUDGE") or "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool((os.environ.get("HERMES_KANBAN_TASK") or "").strip())


def _tool_call_name(tc: Any) -> str:
    """Tool name from a dict or object tool call (``function.name`` first, then ``name``)."""
    if isinstance(tc, dict):
        fn = tc.get("function")
        return str((fn.get("name") if isinstance(fn, dict) else tc.get("name")) or "")
    fn = getattr(tc, "function", None)
    return str((getattr(fn, "name", "") if fn is not None else getattr(tc, "name", "")) or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    for msg in filter(lambda m: isinstance(m, dict), messages or ()):
        role = msg.get("role")
        if role == "assistant" and any(
            _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS for tc in msg.get("tool_calls") or []
        ):
            return True
        if role == "tool" and str(msg.get("name") or "") in _TERMINAL_KANBAN_TOOLS:
            return True
    return False


def _own_run_id() -> Optional[int]:
    """This worker's dispatcher run id (``HERMES_KANBAN_RUN_ID``), when it has one."""
    raw = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _run_still_open(task_id: str) -> bool:
    """True when the board's own rows still show this worker's run in flight.

    Liveness source is the board (the same rows the dispatcher reads to decide a
    protocol violation — ``kanban_db_dispatch._reclaim_dead_workers`` reclaims
    ``status = 'running' AND worker_pid IS NOT NULL``), never the message history.
    ``False`` when the task is no longer ``running``, or the run this worker owns
    already has ``ended_at`` — i.e. its lifecycle is complete through *any*
    run-closing path, ``kanban_request_changes`` / ``kanban_request_review``
    included, and re-terminating is impossible (both native tools refuse).

    Fail-open: any read problem reports ``True`` so the guard behaves exactly as
    before rather than letting a worker that genuinely owes a terminal call exit
    silently.
    """
    try:
        from hermes_cli.kanban_db_connect import connect_closing

        with connect_closing() as conn:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            if row is None:
                # No positive evidence the lifecycle moved: the nudge stays. The board
                # this process resolved may not even be the worker's own.
                return True
            if str(row["status"] or "") != "running":
                return False  # already transitioned (review/ready/done/blocked/triage)
            run_id = _own_run_id()
            if run_id is None:
                return True  # locally-driven worker: the task row is the only truth
            run = conn.execute(
                "SELECT ended_at FROM task_runs WHERE id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            return run is None or run["ended_at"] is None
    except Exception:
        return True


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Synthetic follow-up when a kanban worker exits without a terminal tool; ``None`` when
    the guard should not fire (not a kanban worker, already completed/blocked, the run is
    already closed, budget exhausted)."""
    if (
        not kanban_stop_nudge_enabled()
        or attempts >= max_attempts
        or session_called_kanban_terminal(messages)
    ):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if tid and not _run_still_open(tid):
        return None

    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid or 'this task'}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no "
        "`kanban_complete` / `kanban_block`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, OR `kanban_block(reason=...)` if you are blocked.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = ["build_kanban_stop_nudge", "kanban_stop_nudge_enabled", "session_called_kanban_terminal"]
