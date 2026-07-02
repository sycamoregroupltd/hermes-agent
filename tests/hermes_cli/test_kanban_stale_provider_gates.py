from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_stale_provider_gates import (
    classify_provider_gate,
    scan_all_boards,
)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _set_task(
    conn,
    task_id: str,
    *,
    status: str,
    created_at: int,
    completed_at: int | None = None,
    last_failure_error: str | None = None,
    consecutive_failures: int = 0,
) -> None:
    conn.execute(
        "UPDATE tasks SET status=?, created_at=?, completed_at=?, "
        "last_failure_error=?, consecutive_failures=? WHERE id=?",
        (
            status,
            created_at,
            completed_at,
            last_failure_error,
            consecutive_failures,
            task_id,
        ),
    )
    conn.commit()


def test_classify_provider_gate_keeps_auth_separate() -> None:
    assert classify_provider_gate("OpenAI Codex 429 rate limit window") == "provider_capacity"
    assert (
        classify_provider_gate("Not logged into Nous Portal; missing access_token")
        == "active_credential_auth_failure"
    )
    # Auth wins when both appear, because credentials must stay operator-gated.
    assert (
        classify_provider_gate("429 while missing access_token")
        == "active_credential_auth_failure"
    )


def test_scan_flags_stale_capacity_after_same_profile_success(kanban_home: Path) -> None:
    now = 2_000_000
    kb.init_db(board="jarvis-os")
    with kb.connect(board="jarvis-os") as conn:
        blocked = kb.create_task(
            conn,
            title="Provider capacity gate",
            body="OpenAI Codex 429 provider-capacity decision packet",
            assignee="jarvis-os-pm",
        )
        _set_task(
            conn,
            blocked,
            status="blocked",
            created_at=now - 100_000,
            last_failure_error="OpenAI Codex 429 rate limit",
        )
        success = kb.create_task(conn, title="same profile recovered", assignee="jarvis-os-pm")
        _set_task(conn, success, status="done", created_at=now - 1_000, completed_at=now - 100)

    findings = scan_all_boards(kanban_home, boards=["jarvis-os"], now=now)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.task_id == blocked
    assert finding.classification == "provider_capacity"
    assert finding.stale is True
    assert finding.same_profile_successes[0].task_id == success


def test_scan_does_not_expire_active_auth_failure(kanban_home: Path) -> None:
    now = 2_000_000
    kb.init_db(board="jarvis-os")
    with kb.connect(board="jarvis-os") as conn:
        blocked = kb.create_task(
            conn,
            title="Provider auth gate",
            body="Not logged into Nous Portal, missing access_token",
            assignee="jarvis-os-pm",
        )
        _set_task(
            conn,
            blocked,
            status="blocked",
            created_at=now - 100_000,
            last_failure_error="missing access_token",
        )
        success = kb.create_task(conn, title="same profile recovered", assignee="jarvis-os-pm")
        _set_task(conn, success, status="done", created_at=now - 1_000, completed_at=now - 100)

    findings = scan_all_boards(kanban_home, boards=["jarvis-os"], now=now)

    assert len(findings) == 1
    assert findings[0].classification == "active_credential_auth_failure"
    assert findings[0].stale is False
    assert "operator-gated" in findings[0].reason


def test_dependent_eligibility_respects_other_open_parents(kanban_home: Path) -> None:
    now = 2_000_000
    kb.init_db(board="jarvis-os")
    kb.init_db(board="sycode-trading")
    with kb.connect(board="jarvis-os") as conn:
        blocked = kb.create_task(
            conn,
            title="Provider capacity packet",
            body="OpenAI Codex 429 provider-capacity decision packet",
            assignee="jarvis-os-pm",
        )
        _set_task(
            conn,
            blocked,
            status="blocked",
            created_at=now - 100_000,
            last_failure_error="OpenAI Codex 429 rate limit",
        )
        success = kb.create_task(conn, title="same profile recovered", assignee="jarvis-os-pm")
        _set_task(conn, success, status="done", created_at=now - 1_000, completed_at=now - 100)

    with kb.connect(board="sycode-trading") as conn:
        parent = kb.create_task(conn, title="time gate still blocked", assignee="sycode-trading-pm")
        _set_task(conn, parent, status="blocked", created_at=now - 90_000)
        dependent = kb.create_task(
            conn,
            title="Read-only follow-up",
            body=f"read-only diagnostic depends on jarvis-os/{blocked}",
            assignee="sycode-trading-pm",
        )
        _set_task(conn, dependent, status="blocked", created_at=now - 80_000)
        kb.link_tasks(conn, parent_id=parent, child_id=dependent)

    findings = scan_all_boards(kanban_home, boards=["jarvis-os", "sycode-trading"], now=now)

    finding = next(f for f in findings if f.task_id == blocked)
    dep = next(d for d in finding.dependents if d.task_id == dependent)
    assert dep.safe_read_only_signal is True
    assert dep.eligible is False
    assert dep.open_parent_ids == [parent]


def test_linked_child_eligible_when_only_open_parent_is_stale_blocker(kanban_home: Path) -> None:
    now = 2_000_000
    kb.init_db(board="jarvis-os")
    with kb.connect(board="jarvis-os") as conn:
        blocked = kb.create_task(
            conn,
            title="Provider capacity packet",
            body="OpenAI Codex 429 provider-capacity decision packet",
            assignee="jarvis-os-pm",
        )
        _set_task(
            conn,
            blocked,
            status="blocked",
            created_at=now - 100_000,
            last_failure_error="OpenAI Codex 429 rate limit",
        )
        success = kb.create_task(conn, title="same profile recovered", assignee="jarvis-os-pm")
        _set_task(conn, success, status="done", created_at=now - 1_000, completed_at=now - 100)
        dependent = kb.create_task(
            conn,
            title="Read-only linked follow-up",
            body="read-only diagnostic child for provider-capacity reset",
            assignee="jarvis-os-pm",
        )
        _set_task(conn, dependent, status="blocked", created_at=now - 80_000)
        kb.link_tasks(conn, parent_id=blocked, child_id=dependent)

    findings = scan_all_boards(kanban_home, boards=["jarvis-os"], now=now)

    finding = next(f for f in findings if f.task_id == blocked)
    dep = next(d for d in finding.dependents if d.task_id == dependent)
    assert dep.mention_source == "link"
    assert dep.safe_read_only_signal is True
    assert dep.eligible is True
    assert dep.open_parent_ids == []


def test_linked_child_stays_ineligible_when_other_open_parent_remains(kanban_home: Path) -> None:
    now = 2_000_000
    kb.init_db(board="jarvis-os")
    with kb.connect(board="jarvis-os") as conn:
        blocked = kb.create_task(
            conn,
            title="Provider capacity packet",
            body="OpenAI Codex 429 provider-capacity decision packet",
            assignee="jarvis-os-pm",
        )
        _set_task(
            conn,
            blocked,
            status="blocked",
            created_at=now - 100_000,
            last_failure_error="OpenAI Codex 429 rate limit",
        )
        other_parent = kb.create_task(conn, title="time gate still blocked", assignee="jarvis-os-pm")
        _set_task(conn, other_parent, status="blocked", created_at=now - 90_000)
        success = kb.create_task(conn, title="same profile recovered", assignee="jarvis-os-pm")
        _set_task(conn, success, status="done", created_at=now - 1_000, completed_at=now - 100)
        dependent = kb.create_task(
            conn,
            title="Read-only linked follow-up",
            body="read-only diagnostic child for provider-capacity reset",
            assignee="jarvis-os-pm",
        )
        _set_task(conn, dependent, status="blocked", created_at=now - 80_000)
        kb.link_tasks(conn, parent_id=blocked, child_id=dependent)
        kb.link_tasks(conn, parent_id=other_parent, child_id=dependent)

    findings = scan_all_boards(kanban_home, boards=["jarvis-os"], now=now)

    finding = next(f for f in findings if f.task_id == blocked)
    dep = next(d for d in finding.dependents if d.task_id == dependent)
    assert dep.mention_source == "link"
    assert dep.safe_read_only_signal is True
    assert dep.eligible is False
    assert dep.open_parent_ids == [other_parent]
