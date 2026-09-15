"""Terminal-transition idempotency: a second terminal attempt on a run that has
already ended must come back NAMED, not as the generic
``unknown id or not in running/ready``.

The failure this pins down: a review-lane worker that already handed the card
back with ``kanban_request_changes`` ends its turn with a plain-text reply, gets
the "task is still running" nudge, and calls ``kanban_block`` — which then fails
with a message that reads as "your task id is wrong" even though the card is
``ready`` and the worker's run is closed (``ended_at`` set). The worker is left
with no reachable terminal and the nudge's stated remedy (``kanban_complete``)
would falsely approve a card that was sent back for changes.

``request_changes`` / ``block_task`` behaviour itself is unchanged: these tests
pin the *reason* each refusal reports, plus the bool API contract existing
callers rely on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _to_reviewer_run(conn, tid: str) -> int:
    """implementer run -> review -> reviewer run; returns the reviewer run id."""
    kb.claim_task(conn, tid)
    implementer_run = kb.get_task(conn, tid).current_run_id
    assert kb.request_review(
        conn, tid, summary="implemented", expected_run_id=implementer_run,
    ) is True
    claimed = kb.claim_review_task(conn, tid)
    assert claimed is not None and claimed.current_run_id is not None
    return int(claimed.current_run_id)


def test_second_request_changes_reports_already_closed(kanban_home: Path) -> None:
    """The reviewer's second hand-back names the already-closed card instead of
    the old catch-all "not in an active review run" (which cannot distinguish
    'already transitioned' from 'wrong lane')."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="hand back twice", assignee="implementer")
        reviewer_run = _to_reviewer_run(conn, tid)

        ok, implementer = kb.request_changes(
            conn, tid, reason="fix X", expected_run_id=reviewer_run,
        )
        assert ok is True and implementer == "implementer"

        ok, reason = kb.request_changes(
            conn, tid, reason="fix X again", expected_run_id=reviewer_run,
        )
        assert ok is False
        assert reason is not None
        assert "already" in reason and "'ready'" in reason
        assert reason != "task is not in an active review run"
        # The card is untouched: still the implementer's to rework.
        row = conn.execute(
            "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert (row["status"], row["assignee"], row["current_run_id"]) == (
            "ready", "implementer", None,
        )


def test_block_after_review_handoff_names_state_not_unknown_id(kanban_home: Path) -> None:
    """The ``kanban_block`` refusal a nudged worker actually hits: the card is
    ``ready`` and the run is closed, so the reason must say so — never
    ``task not found`` (which the old message read as)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="block a ready card", assignee="implementer")
        reviewer_run = _to_reviewer_run(conn, tid)
        assert kb.request_changes(
            conn, tid, reason="fix Y", expected_run_id=reviewer_run,
        )[0] is True

        ok, reason = kb.block_task(
            conn, tid, reason="needs input", kind="needs_input",
            expected_run_id=reviewer_run, with_reason=True,
        )
        assert ok is False
        assert reason is not None
        assert "already 'ready'" in reason
        assert "not found" not in reason.lower()
        # No block landed: the card stays with the implementer.
        assert kb.get_task(conn, tid).status == "ready"


def test_block_refusal_reasons_distinguish_causes(kanban_home: Path) -> None:
    """Three distinct causes, three distinct reasons."""
    with kbc.connect() as conn:
        # 1) Unknown id.
        ok, reason = kb.block_task(conn, "t_000000000000", with_reason=True)
        assert ok is False and reason == "task not found"

        # 2) Superseded run: the task is running, but under a different run.
        tid = kb.create_task(conn, title="superseded", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        ok, reason = kb.block_task(
            conn, tid, reason="late", expected_run_id=int(claimed.current_run_id) + 999,
            with_reason=True,
        )
        assert ok is False
        assert reason is not None and "superseded" in reason
        # Untouched: the live run keeps the card.
        assert kb.get_task(conn, tid).status == "running"


def test_block_task_bool_contract_unchanged(kanban_home: Path) -> None:
    """Default callers (dashboard/CLI/tests) still get a plain bool, and a real
    block still returns True."""
    with kbc.connect() as conn:
        assert kb.block_task(conn, "t_000000000000", reason="nope") is False
        tid = kb.create_task(conn, title="normal block", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="waiting", kind="needs_input") is True
        assert kb.get_task(conn, tid).status == "blocked"
