"""Unblock is not repair evidence; owners can contain non-running cards."""
import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect_closing() as c:
        yield c


def parked(conn, status="blocked", parents=None):
    tid = kb.create_task(conn, title="crash containment", assignee="worker", parents=parents or ())
    conn.execute(
        "UPDATE tasks SET status=?, consecutive_failures=5, "
        "last_failure_error='worker died', block_kind='capability', "
        "block_recurrences=2 WHERE id=?", (status, tid),
    )
    conn.commit()
    return tid


@pytest.mark.parametrize("status", ["blocked", "scheduled"])
def test_plain_unblock_preserves_crash_evidence(conn, status):
    tid = parked(conn, status)
    assert kb.unblock_task(conn, tid)
    task = kb.get_task(conn, tid)
    assert task.status == "ready"
    assert (task.consecutive_failures, task.last_failure_error) == (5, "worker died")
    assert (task.block_kind, task.block_recurrences) == ("capability", 2)
    # Another failed attempt immediately re-trips, rather than buying a new budget.
    assert kb.claim_task(conn, tid)
    kb._record_task_failure(conn, tid, error="still broken", outcome="crashed",
                            release_claim=True, end_run=True, failure_limit=2)
    task = kb.get_task(conn, tid)
    assert (task.status, task.consecutive_failures) == ("blocked", 6)


def test_reason_alone_is_not_a_repair_assertion(conn):
    tid = parked(conn)
    assert kb.unblock_task(conn, tid, actor="owner", reason="retry later")
    assert kb.get_task(conn, tid).consecutive_failures == 5


@pytest.mark.parametrize("actor,reason", [(None, "fixed"), ("owner", None),
                                          (" ", "fixed"), ("owner", " \t")])
def test_repair_requires_nonblank_actor_and_reason_without_mutation(conn, actor, reason):
    tid = parked(conn)
    before = kb.get_task(conn, tid)
    events = kb.list_events(conn, tid)
    with pytest.raises(ValueError, match="actor.*reason"):
        kb.unblock_task(conn, tid, cause_fixed=True, actor=actor, reason=reason)
    assert kb.get_task(conn, tid) == before
    assert kb.list_events(conn, tid) == events


@pytest.mark.parametrize("status", ["blocked", "scheduled"])
def test_explicit_repair_is_atomic_and_audited_but_parent_gated(conn, status):
    parent = kb.create_task(conn, title="unfinished parent", assignee="worker")
    tid = parked(conn, status, parents=[parent])
    assert kb.unblock_task(conn, tid, actor="owner", reason="fixed startup import; test passed", cause_fixed=True)
    task = kb.get_task(conn, tid)
    assert task.status == "todo"
    assert (task.consecutive_failures, task.last_failure_error) == (0, None)
    assert (task.block_kind, task.block_recurrences) == ("capability", 2)
    event = [e for e in kb.list_events(conn, tid) if e.kind == "unblocked"][-1]
    payload = event.payload
    assert payload["actor"] == "owner"
    assert payload["reason"] == "fixed startup import; test passed"
    assert payload["cause_fixed"] is True
    assert kb.claim_task(conn, tid) is None


@pytest.mark.parametrize("status", ["todo", "scheduled", "triage"])
@pytest.mark.parametrize("kind", [None, "capability", "needs_input", "transient"])
def test_reasoned_owner_containment_preserves_breakers(conn, status, kind):
    tid = parked(conn, status)
    assert kb.block_task(conn, tid, reason="owner: root cause still unresolved", kind=kind)
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"  # containment must not bounce back to triage
    assert (task.consecutive_failures, task.last_failure_error) == (5, "worker died")
    assert task.block_recurrences == 2
    assert task.current_run_id is None
    assert kb.claim_task(conn, tid) is None
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == "blocked"
    event = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"][-1]
    assert event.payload["reason"] == "owner: root cause still unresolved"


@pytest.mark.parametrize("status", ["todo", "scheduled", "triage"])
@pytest.mark.parametrize("reason", [None, "", " \t"])
def test_owner_containment_requires_reason(conn, status, reason):
    tid = parked(conn, status)
    before = kb.get_task(conn, tid)
    assert not kb.block_task(conn, tid, reason=reason, kind="capability")
    assert kb.get_task(conn, tid) == before


@pytest.mark.parametrize("status", ["todo", "scheduled", "triage", "done", "archived", "review"])
def test_containment_does_not_authorize_stale_worker_or_dependency_release(conn, status):
    tid = parked(conn, status)
    before = kb.get_task(conn, tid)
    assert not kb.block_task(conn, tid, reason="old worker", expected_run_id=123)
    assert not kb.block_task(conn, tid, reason="wait", kind="dependency")
    assert kb.get_task(conn, tid) == before


def test_cli_explicit_repair_and_plain_reason(conn, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_profile_author", lambda: "owner")
    tid = parked(conn)
    args = argparse.Namespace(task_ids=[tid], reason="retry later", cause_fixed=False)
    assert cli._cmd_unblock(args) == 0
    assert kb.get_task(conn, tid).consecutive_failures == 5
    conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
    conn.commit()
    args.reason = "fixed missing module; regression passed"
    args.cause_fixed = True
    assert cli._cmd_unblock(args) == 0
    assert kb.get_task(conn, tid).consecutive_failures == 0
    assert "Unblocked" in capsys.readouterr().out


def test_cli_repair_without_reason_fails_without_mutation(conn, capsys):
    tid = parked(conn)
    args = argparse.Namespace(task_ids=[tid], reason=" ", cause_fixed=True)
    assert cli._cmd_unblock(args) == 1
    assert kb.get_task(conn, tid).status == "blocked"
    assert kb.get_task(conn, tid).consecutive_failures == 5
    assert "reason" in capsys.readouterr().err
