"""Creation-time human gates remain blocked until an explicit unblock.

The direct-claim and dispatch regression rows are stacked on open PR #99998,
which owns the unresolved-block claim backstop.  They skip on ``main`` until
that dependency lands; this branch deliberately does not duplicate its source
change.  The complete stack is exercised by composing #99998 in a temporary
checkout during verification.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


requires_pr_99998 = pytest.mark.skipif(
    not hasattr(kb, "_has_unresolved_block"),
    reason="depends on open PR #99998's unresolved-block claim backstop",
)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.mark.parametrize(
    ("parent_state", "status_after_unblock"),
    (("none", "ready"), ("unmet", "todo"), ("satisfied", "ready")),
)
def test_initial_blocked_is_a_sticky_human_gate_until_unblocked(
    kanban_home: Path, parent_state: str, status_after_unblock: str,
) -> None:
    with kbc.connect_closing() as conn:
        parents: list[str] = []
        if parent_state != "none":
            parent_id = kb.create_task(conn, title=f"{parent_state} parent")
            if parent_state == "satisfied":
                assert kb.complete_task(conn, parent_id, result="done")
            parents.append(parent_id)

        task_id = kb.create_task(
            conn,
            title=f"{parent_state} initial human gate",
            parents=parents,
            initial_status="blocked",
        )

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert (task.status, task.block_kind, task.block_recurrences) == (
            "blocked", "needs_input", 0,
        )
        blocked_events = [
            event for event in kb.list_events(conn, task_id) if event.kind == "blocked"
        ]
        assert len(blocked_events) == 1
        assert blocked_events[0].payload == {
            "reason": "initial_status=blocked",
            "kind": "needs_input",
            "recurrences": 0,
            "source_status": "created",
            "origin": "initial_creation",
            "gate": "human",
        }

        for _ in range(3):
            assert kb.recompute_ready(conn) == 0
            assert kb.get_task(conn, task_id).status == "blocked"

        assert kb.unblock_task(conn, task_id)
        assert kb.get_task(conn, task_id).status == status_after_unblock


def test_initial_human_gate_does_not_spend_first_recurrence(
    kanban_home: Path,
) -> None:
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="initial gate then first runtime block",
            assignee="worker",
            initial_status="blocked",
        )

        assert kb.unblock_task(conn, task_id)
        claimed = kb.claim_task(conn, task_id, claimer="worker")
        assert claimed is not None
        assert kb.block_task(
            conn,
            task_id,
            reason="the first real input request",
            kind="needs_input",
            expected_run_id=claimed.current_run_id,
        )

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert (task.status, task.block_kind, task.block_recurrences) == (
            "blocked", "needs_input", 1,
        )
        assert not [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "block_loop_detected"
        ]


def _initial_gate_with_parent_state(conn, parent_state: str) -> tuple[str, str | None]:
    parent_id = None
    if parent_state != "none":
        parent_id = kb.create_task(conn, title=f"{parent_state} parent")
        if parent_state == "satisfied":
            assert kb.complete_task(conn, parent_id, result="done")
    task_id = kb.create_task(
        conn,
        title=f"{parent_state} externally flipped initial gate",
        assignee="worker",
        parents=[parent_id] if parent_id else [],
        initial_status="blocked",
    )
    return task_id, parent_id


def _external_ready_flip(conn, task_id: str) -> None:
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    conn.commit()


@requires_pr_99998
@pytest.mark.parametrize("parent_state", ("none", "unmet", "satisfied"))
def test_external_ready_flip_cannot_bypass_initial_gate_via_claim_task(
    kanban_home: Path, parent_state: str,
) -> None:
    with kbc.connect_closing() as conn:
        task_id, _ = _initial_gate_with_parent_state(conn, parent_state)
        _external_ready_flip(conn, task_id)

        assert kb.claim_task(conn, task_id, claimer="worker") is None
        assert kb.list_runs(conn, task_id) == []
        rejected = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "claim_rejected"
        ]
        assert rejected[-1].payload == {"reason": "sticky_blocked"}


@requires_pr_99998
@pytest.mark.parametrize("parent_state", ("none", "unmet", "satisfied"))
def test_external_ready_flip_cannot_bypass_initial_gate_via_dispatch_once(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_state: str,
) -> None:
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    spawned: list[str] = []

    def spawn(task, _workspace, board=None):
        spawned.append(task.id)
        return 4242

    with kbc.connect_closing() as conn:
        task_id, _ = _initial_gate_with_parent_state(conn, parent_state)
        _external_ready_flip(conn, task_id)

        result = kbd.dispatch_once(conn, spawn_fn=spawn)

        assert spawned == []
        assert result.spawned == []
        assert kb.list_runs(conn, task_id) == []


@pytest.mark.parametrize("entrypoint", ("claim", "dispatch"))
@pytest.mark.parametrize("parent_state", ("none", "unmet", "satisfied"))
def test_explicit_unblock_preserves_normal_parent_gating_and_admission(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_state: str,
    entrypoint: str,
) -> None:
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    with kbc.connect_closing() as conn:
        task_id, parent_id = _initial_gate_with_parent_state(conn, parent_state)
        assert kb.unblock_task(conn, task_id)
        if parent_state == "unmet":
            assert kb.get_task(conn, task_id).status == "todo"
            assert parent_id is not None
            assert kb.complete_task(conn, parent_id, result="done")
        assert kb.get_task(conn, task_id).status == "ready"

        if entrypoint == "claim":
            assert kb.claim_task(conn, task_id, claimer="worker") is not None
        else:
            result = kbd.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: 4242)
            assert [task_id] == [item[0] for item in result.spawned]


def test_unknown_parent_rolls_back_initial_gate_without_residue(
    kanban_home: Path,
) -> None:
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match=r"unknown parent task\(s\): t_missing"):
            kb.create_task(
                conn,
                title="invalid initial gate",
                parents=["t_missing"],
                initial_status="blocked",
            )

        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0


def test_dependency_only_todo_still_promotes_when_parent_finishes(
    kanban_home: Path,
) -> None:
    with kbc.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="dependency parent")
        child_id = kb.create_task(
            conn, title="dependency child", parents=[parent_id],
        )

        child = kb.get_task(conn, child_id)
        assert child is not None
        assert (child.status, child.block_kind, child.block_recurrences) == (
            "todo", None, 0,
        )
        assert kb.recompute_ready(conn) == 0

        assert kb.complete_task(conn, parent_id, result="done")
        assert kb.get_task(conn, child_id).status == "ready"
