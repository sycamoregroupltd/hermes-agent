"""Turn-end guard for kanban workers.

Every dispatched run must end with one successful native lifecycle transition:
``kanban_complete`` / ``kanban_request_review`` / ``kanban_block`` for an
implementer, or ``kanban_complete`` / ``kanban_request_changes`` /
``kanban_block`` for a reviewer. Models sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
successful terminal tool result. Hermes would otherwise treat that as a clean
exit → ``rc=0`` → dispatcher ``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
successful terminal board result, return a bounded synthetic nudge so the
conversation loop continues instead of exiting.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, Optional


_TERMINAL_KANBAN_TOOLS = frozenset({
    "kanban_complete",
    "kanban_request_review",
    "kanban_request_changes",
    "kanban_block",
})

_DEFAULT_MAX_ATTEMPTS = 2

# When the nudge budget is exhausted and the worker STILL exits with a plain
# narration (finish_reason=stop, no terminal board tool), we must not let the
# process return rc=0 and leave the card silently `running` — that is exactly
# the protocol_violation class this module exists to prevent. Instead, build a
# concrete `kanban_block` payload the harness can fire so the card lands in a
# visible, routable `blocked` state with a real reason (never silently
# `running`, never a phantom `complete`). See t_44cfa735.
_BLOCK_REASON_PREFIX = "auto-block: worker exited without a successful native kanban terminal"


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)


def _tool_result_succeeded(content: Any) -> bool:
    """Return whether a terminal tool result confirms a durable transition.

    Merely emitting a terminal tool call is not enough: lifecycle gates can
    reject stale run ids, incomplete handoffs, or blocked artifacts. All native
    kanban terminal handlers return ``{"ok": true, ...}`` on success and a
    structured ``{"error": ...}`` payload on refusal.
    """
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError):
            return False
    return isinstance(content, dict) and content.get("ok") is True and not content.get("error")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation completed a native kanban terminal tool.

    Assistant intent is deliberately insufficient. Only a successful tool
    result proves the board transition landed; a rejected terminal attempt must
    leave the stop guard active so the worker can correct and retry.
    """
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        name = str(msg.get("name") or msg.get("tool_name") or "")
        if name in _TERMINAL_KANBAN_TOOLS and _tool_result_succeeded(msg.get("content")):
            return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire (not a kanban worker,
    already completed/blocked, or nudge budget exhausted).
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts >= max_attempts:
        return None
    if session_called_kanban_terminal(messages):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a successful native "
        "terminal causes a protocol violation (clean exit with no board transition).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call exactly one applicable native terminal: `kanban_complete` when final; "
        "`kanban_request_review` when implementation needs same-card review; "
        "`kanban_request_changes` when reviewing and concrete rework is required; or "
        "`kanban_block` only for a genuine external blocker. Empty/no-op findings still "
        "require a terminal. If a prior call was rejected, correct it and retry.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


def build_kanban_stop_fallback_block(
    *,
    messages: Iterable[dict] | None = None,
    final_response: Optional[str] = None,
    model: Optional[str] = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[dict]:
    """Build a concrete ``kanban_block`` payload when the nudge budget is spent.

    The agent turn-end guard (``build_kanban_stop_nudge``) gives the model up
    to ``max_attempts`` chances to emit one successful native lifecycle
    terminal. If the worker STILL ends the turn with a plain narration and no
    durable board transition, returning a synthetic nudge again would just loop forever —
    and letting the process exit rc=0 leaves the card silently ``running``,
    which the dispatcher records as a ``protocol_violation`` (the dominant
    worker-failure mode tracked in t_44cfa735).

    This returns a ready-to-fire ``kanban_block(reason=...)`` payload so the
    harness can terminate the card in a visible, routable ``blocked`` state
    with a real reason recorded — never silently ``running``, never a phantom
    ``complete``. The reason is prefixed so dashboards/analyzers can attribute
    the block to the protocol-violation auto-guard rather than a real human
    gate.

    Returns ``None`` when the guard should not fire (not a kanban worker, the
    session already called a terminal tool, or the nudge budget is not yet
    exhausted — in which case the caller should still issue one more nudge).
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts < max_attempts:
        # Nudge budget not exhausted: caller should still try to coax a
        # terminal call out of the model before falling back to a hard block.
        return None
    if session_called_kanban_terminal(messages):
        # Defensive: if a terminal transition succeeded at any point this
        # session, do not double-block.
        return None
    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    snippet = (final_response or "").strip().replace("\n", " ")[:280]
    reason_parts = [
        f"{_BLOCK_REASON_PREFIX} after {attempts} nudge attempt(s).",
        f"The worker ended its turn without a successful native terminal board "
        f"transition (model={model or 'unknown'}).",
    ]
    if snippet:
        reason_parts.append(f"Final narration (truncated): \"{snippet}\"")
    reason_parts.append(
        "Route: a human must verify whether the work was actually completed "
        "(check artifacts/comments) and either unblock for retry or complete it."
    )
    return {
        "reason": " ".join(reason_parts),
        # Mark the kind so routing/analytics can separate this auto-guard block
        # from a genuine human/dependency gate. 'capability' is the closest
        # valid kind: the worker could not perform the required terminal
        # transition on its own.
        "kind": "capability",
        "auto_guard": True,
        "task_id": tid,
        "nudge_attempts": attempts,
    }


__all__ = [
    "build_kanban_stop_nudge",
    "build_kanban_stop_fallback_block",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]
