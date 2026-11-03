"""Crash owner gate: a card the dispatcher's breaker auto-blocked (``gave_up``)
is released only by its owner (with a reason) or an operator surface, and no
unblock resets the crash streak. Design: docs/design/kanban-crash-owner-gate.md.

Every test drives the real DB + dispatcher failure path (``_record_task_failure``)
against a temp HERMES_HOME; no mocks of the code under test, no network.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home: Path):
    c = kbc.connect()
    try:
        yield c
    finally:
        c.close()


def _crash(conn, tid: str) -> bool:
    """One claimed attempt that dies (the spawn/crash funnel); True when it tripped."""
    assert kb.claim_task(conn, tid) is not None, kb.get_task(conn, tid).status
    return kbd._record_task_failure(
        conn, tid, "worker exited 1", outcome="crashed", failure_limit=2,
        release_claim=True, end_run=True,
    )


def _crash_until_gated(conn, tid: str) -> None:
    assert _crash(conn, tid) is False
    assert _crash(conn, tid) is True, "2nd crash must trip the breaker"
    assert kb.get_task(conn, tid).status == "blocked"


def _events(conn, tid: str, kind: str) -> list:
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


# --- trip payload / notification routing ------------------------------------

def test_gave_up_names_owner_and_notification_reach(conn) -> None:
    tid = kb.create_task(conn, title="flaky", assignee="builder", created_by="fleet-engineer")
    _crash_until_gated(conn, tid)
    gave_up = _events(conn, tid, "gave_up")[-1].payload
    assert gave_up["owner_gate"] is True
    assert gave_up["owner"] == "fleet-engineer"
    assert gave_up["owner_source"] == "created_by"
    assert gave_up["owner_notify_subs"] == 0
    assert kb.open_crash_gate(conn, tid) is not None


def test_owner_notify_subs_counts_gateway_subscriptions(conn) -> None:
    from hermes_cli import kanban_db_notify as kbn
    tid = kb.create_task(conn, title="flaky", assignee="builder", created_by="pm")
    kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="42")
    _crash_until_gated(conn, tid)
    assert _events(conn, tid, "gave_up")[-1].payload["owner_notify_subs"] == 1


# --- non-owner unblock ------------------------------------------------------

def test_non_owner_unblock_is_refused_and_audited(conn) -> None:
    tid = kb.create_task(conn, title="flaky", assignee="builder", created_by="fleet-engineer")
    _crash_until_gated(conn, tid)
    failures = kb.get_task(conn, tid).consecutive_failures

    with pytest.raises(kb.CrashGateRefused) as exc:
        kb.unblock_task(conn, tid, actor="pm-autopilot", source="tool", reason="looks fine")
    assert "fleet-engineer" in str(exc.value)

    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    assert task.consecutive_failures == failures
    refused = _events(conn, tid, "unblock_refused")[-1].payload
    assert refused["actor"] == "pm-autopilot"
    assert refused["source"] == "tool"
    assert refused["owner"] == "fleet-engineer"
    assert _events(conn, tid, "unblocked") == []
    # The dispatcher must not slip it back in either.
    assert kb.recompute_ready(conn) == 0
    assert kb.get_task(conn, tid).status == "blocked"


def test_unidentified_caller_is_refused(conn) -> None:
    """Out-of-tree callers that pass no actor (old autopilots) fail closed."""
    tid = kb.create_task(conn, title="flaky", assignee="builder", created_by="fleet-engineer")
    _crash_until_gated(conn, tid)
    with pytest.raises(kb.CrashGateRefused):
        kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == "blocked"


def test_reassign_does_not_release_the_gate(conn) -> None:
    """assign_task resets the per-profile streak; it must not reopen the card."""
    tid = kb.create_task(conn, title="flaky", assignee="builder", created_by="fleet-engineer")
    _crash_until_gated(conn, tid)
    assert kb.assign_task(conn, tid, "other-builder")
    assert kb.get_task(conn, tid).consecutive_failures == 0
    assert kb.recompute_ready(conn) == 0
    assert kb.get_task(conn, tid).status == "blocked"


def test_promote_is_not_a_bypass(conn) -> None:
    tid = kb.create_task(conn, title="flaky", assignee="builder", created_by="fleet-engineer")
    _crash_until_gated(conn, tid)
    ok, err = kb.promote_task(conn, tid, actor="pm-autopilot", reason="retry")
    assert ok is False and "fleet-engineer" in err
    ok, err = kb.promote_task(conn, tid, actor="pm-autopilot", reason="retry", dry_run=True)
    assert ok is False
    assert kb.get_task(conn, tid).status == "blocked"
    ok, err = kb.promote_task(conn, tid, actor="fleet-engineer", reason="pinned model")
    assert ok, err
    payload = _events(conn, tid, "promoted_manual")[-1].payload
    assert payload["crash_gate"]["released_by"] == "owner"


# --- owner release ----------------------------------------------------------

def test_owner_needs_a_reason(conn) -> None:
    tid = kb.create_task(conn, title="flaky", assignee="builder", created_by="fleet-engineer")
    _crash_until_gated(conn, tid)
    with pytest.raises(kb.CrashGateRefused, match="reason"):
        kb.unblock_task(conn, tid, actor="fleet-engineer", source="cli")
    assert kb.get_task(conn, tid).status == "blocked"


def test_owner_release_grants_exactly_one_retry(conn) -> None:
    tid = kb.create_task(conn, title="flaky", assignee="builder", created_by="Fleet-Engineer")
    _crash_until_gated(conn, tid)

    assert kb.unblock_task(conn, tid, actor="fleet-engineer", source="tool", reason="bumped timeout")
    task = kb.get_task(conn, tid)
    assert task.status == "ready"
    assert task.consecutive_failures == kbd.DEFAULT_FAILURE_LIMIT - 1
    assert kb.open_crash_gate(conn, tid) is None

    unblocked = _events(conn, tid, "unblocked")[-1].payload
    assert unblocked["actor"] == "fleet-engineer"
    assert unblocked["source"] == "tool"
    assert unblocked["reason"] == "bumped timeout"
    gate = unblocked["crash_gate"]
    assert gate["released_by"] == "owner"
    assert gate["owner"] == "fleet-engineer"
    assert gate["failures_before"] == 2
    assert gate["failures_after"] == 1

    # The very next crash re-blocks and hands the card back to the owner.
    assert _crash(conn, tid) is True
    assert kb.get_task(conn, tid).status == "blocked"
    assert kb.open_crash_gate(conn, tid) is not None


def test_bounded_crashes_without_owner_action(conn) -> None:
    """The probe-6 invariant: autopilot unblock attempts cannot produce a 3rd crash."""
    tid = kb.create_task(conn, title="flaky", assignee="builder", created_by="fleet-engineer")
    _crash_until_gated(conn, tid)
    for _ in range(5):
        with pytest.raises(kb.CrashGateRefused):
            kb.unblock_task(conn, tid, actor="pm-autopilot", source="tool", reason="auto")
        kb.recompute_ready(conn)
    crashes = [e for e in kb.list_events(conn, tid) if e.kind in ("crashed", "gave_up")]
    assert len(crashes) == 2
    assert kb.get_task(conn, tid).status == "blocked"


def test_creator_task_id_resolves_to_that_tasks_assignee(conn) -> None:
    parent = kb.create_task(conn, title="orchestrating card", assignee="planner")
    tid = kb.create_task(conn, title="child", assignee="builder", created_by=parent)
    assert kb.crash_gate_owner(conn, tid) == ("planner", "creator_task")


# --- legacy / ownerless cards ----------------------------------------------

@pytest.mark.parametrize("creator", [None, "", "worker"])
def test_legacy_ownerless_card_needs_operator_surface(conn, creator) -> None:
    tid = kb.create_task(conn, title="legacy", assignee="builder", created_by=creator)
    if creator is None:
        conn.execute("UPDATE tasks SET created_by = NULL WHERE id = ?", (tid,))
        conn.commit()
    _crash_until_gated(conn, tid)
    assert _events(conn, tid, "gave_up")[-1].payload["owner"] is None

    with pytest.raises(kb.CrashGateRefused, match="no recorded owner"):
        kb.unblock_task(conn, tid, actor="some-agent", source="tool", reason="retry")
    with pytest.raises(kb.CrashGateRefused):
        kb.unblock_task(conn, tid, actor="frank", source="cli")  # no reason
    assert kb.unblock_task(conn, tid, actor="frank", source="cli", reason="checked logs")
    gate = _events(conn, tid, "unblocked")[-1].payload["crash_gate"]
    assert gate["released_by"] == "operator" and gate["owner"] is None


def test_pre_existing_gave_up_card_is_gated_without_migration(conn) -> None:
    """A card blocked by an old-format ``gave_up`` (payload without owner fields)
    is held from install: the gate is derived from the event log."""
    tid = kb.create_task(conn, title="old", assignee="builder", created_by="pm")
    conn.execute("UPDATE tasks SET status='blocked', consecutive_failures=2 WHERE id=?", (tid,))
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'gave_up', ?, 0)",
        (tid, json.dumps({"failures": 2, "effective_limit": 2})),
    )
    conn.commit()
    assert kb.recompute_ready(conn) == 0
    with pytest.raises(kb.CrashGateRefused):
        kb.unblock_task(conn, tid, actor="autopilot", source="tool", reason="x")
    assert kb.unblock_task(conn, tid, actor="pm", source="cli", reason="fixed env")


# --- ordinary (non-crash) unblock -------------------------------------------

def test_plain_unblock_records_actor_and_keeps_streak(conn) -> None:
    tid = kb.create_task(conn, title="waits on a human", assignee="builder", created_by="pm")
    assert _crash(conn, tid) is False  # one crash, below the limit
    assert kb.claim_task(conn, tid) is not None
    assert kb.block_task(conn, tid, reason="need creds",
                         expected_run_id=kb.get_task(conn, tid).current_run_id)

    assert kb.unblock_task(conn, tid, actor="someone-else", source="cli", reason="creds added")
    task = kb.get_task(conn, tid)
    assert task.status == "ready"
    assert task.consecutive_failures == 1, "unblock must not reset the crash streak"
    payload = _events(conn, tid, "unblocked")[-1].payload
    assert payload == {"actor": "someone-else", "source": "cli", "reason": "creds added"}
    # So the next crash is the 2nd since the last success and goes to the owner.
    assert _crash(conn, tid) is True
    assert _events(conn, tid, "gave_up")[-1].payload["owner"] == "pm"


def test_complete_task_still_clears_the_streak(conn) -> None:
    tid = kb.create_task(conn, title="t", assignee="builder", created_by="pm")
    _crash_until_gated(conn, tid)
    kb.unblock_task(conn, tid, actor="pm", source="cli", reason="fix")
    kb.claim_task(conn, tid)
    assert kb.complete_task(conn, tid, result="ok", force=True)
    assert kb.get_task(conn, tid).consecutive_failures == 0


# --- surfaces: CLI, agent tool, dashboard -----------------------------------

def _cli(*argv: str) -> int:
    from hermes_cli import kanban as kc
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kc.build_parser(parser.add_subparsers(dest="command"))
    return kc.kanban_command(parser.parse_args(["kanban", *argv]))


def test_cli_unblock_identifies_profile_and_reports_refusal(kanban_home, monkeypatch, capsys) -> None:
    with kbc.connect_closing() as c:
        tid = kb.create_task(c, title="flaky", assignee="builder", created_by="fleet-engineer")
        _crash_until_gated(c, tid)

    monkeypatch.setenv("HERMES_PROFILE", "pm-autopilot")
    assert _cli("unblock", tid, "--reason", "auto-retry") == 1
    assert "only the owner 'fleet-engineer'" in capsys.readouterr().err
    with kbc.connect_closing() as c:
        assert kb.get_task(c, tid).status == "blocked"
        assert not [x for x in kb.list_comments(c, tid) if "UNBLOCK" in (x.body or "")]

    monkeypatch.setenv("HERMES_PROFILE", "fleet-engineer")
    assert _cli("unblock", tid, "--reason", "pinned the model") == 0
    with kbc.connect_closing() as c:
        payload = _events(c, tid, "unblocked")[-1].payload
        assert payload["actor"] == "fleet-engineer" and payload["source"] == "cli"
        assert payload["crash_gate"]["released_by"] == "owner"


def test_tool_unblock_uses_bound_profile_not_args(kanban_home, monkeypatch) -> None:
    from tools import kanban_tools as kt
    with kbc.connect_closing() as c:
        tid = kb.create_task(c, title="flaky", assignee="builder", created_by="fleet-engineer")
        _crash_until_gated(c, tid)

    monkeypatch.setenv("HERMES_PROFILE", "pm-autopilot")
    out = json.loads(kt._handle_unblock({"task_id": tid, "reason": "auto"}))
    assert not out.get("ok"), out
    assert "fleet-engineer" in json.dumps(out)

    monkeypatch.setenv("HERMES_PROFILE", "fleet-engineer")
    out = json.loads(kt._handle_unblock({"task_id": tid, "reason": "root-caused OOM"}))
    assert out["ok"] is True and out["status"] == "ready"
    with kbc.connect_closing() as c:
        payload = _events(c, tid, "unblocked")[-1].payload
        assert payload["actor"] == "fleet-engineer" and payload["source"] == "tool"


def _dashboard_module():
    plugin_file = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_kanban_gate_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("target", ["ready", "todo"])
def test_dashboard_drag_is_an_audited_operator_override(conn, target) -> None:
    mod = _dashboard_module()
    tid = kb.create_task(conn, title="flaky", assignee="builder", created_by="fleet-engineer")
    _crash_until_gated(conn, tid)
    assert mod._drag_to(conn, tid, target)
    assert kb.get_task(conn, tid).status == target  # lands in the drop column
    payload = _events(conn, tid, "unblocked")[-1].payload
    assert payload["source"] == "dashboard"
    assert payload["crash_gate"]["released_by"] == "operator"
    assert kb.get_task(conn, tid).consecutive_failures == kbd.DEFAULT_FAILURE_LIMIT - 1
