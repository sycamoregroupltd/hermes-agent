"""Creation-time human gates remain blocked until an explicit unblock."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


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
            "blocked", "needs_input", 1,
        )
        blocked_events = [
            event for event in kb.list_events(conn, task_id) if event.kind == "blocked"
        ]
        assert len(blocked_events) == 1
        assert blocked_events[0].payload == {
            "reason": "initial_status=blocked",
            "kind": "needs_input",
            "recurrences": 1,
            "source_status": "created",
            "origin": "initial_creation",
            "gate": "human",
        }

        for _ in range(3):
            assert kb.recompute_ready(conn) == 0
            assert kb.get_task(conn, task_id).status == "blocked"

        assert kb.unblock_task(conn, task_id)
        assert kb.get_task(conn, task_id).status == status_after_unblock


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
