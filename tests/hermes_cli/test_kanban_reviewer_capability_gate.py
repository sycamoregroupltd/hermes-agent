"""Verification tests for the review-card terminal-capability gate (t_680e72d2).

Covers rule (2) from the card acceptance: review-type cards only route to
terminal-capable profiles, and each rejection is durably logged.

Design notes (match fleet/live, NOT the alternate t_fddfa577 design):
  - The gate lives in ``_dispatch_once_locked`` on the ``status='review'``
    loop (kanban_db.py ~12030). It calls ``profiles.profile_has_terminal``
    and, on a non-terminal assignee, writes a ``reviewer_capability`` event
    to ``task_events`` and records the id in
    ``DispatchResult.skipped_reviewer_incapable``.
  - We monkeypatch ``profile_exists`` and ``profile_has_terminal`` rather
    than hardcoding live profile names (toolsets drift), matching the
    existing ``all_assignees_spawnable`` conftest convention but overriding
    per-assignee capability for the negative case.
  - We do NOT use ``all_assignees_spawnable`` for the negative case (it
    pretends every assignee is terminal-capable); instead we patch the
    terminal check explicitly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _profiles():
    """Fetch the CURRENT hermes_cli.profiles module.

    Other kanban test files (e.g. test_kanban_default_assignee.py,
    test_kanban_per_profile_cap.py) force-reimport ``hermes_cli.*`` by
    deleting entries from sys.modules, which replaces the module object
    that ``dispatch_once`` resolves at call time. A module-level
    ``from hermes_cli import profiles`` would pin a stale object and make
    our monkeypatches ineffective after those files run. Importing fresh
    inside the helper (mirroring how the review-lifecycle tests do
    ``import hermes_cli.profiles as profmod``) keeps our patches on the
    same object the dispatcher reads.
    """
    import hermes_cli.profiles as profmod  # noqa: PLC0415
    return profmod


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB (mirrors test_kanban_db)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _set_status(conn, task_id: str, status: str) -> None:
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def _capability_events(conn, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'reviewer_capability'",
        (task_id,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]) if r["payload"] else {})
        except (json.JSONDecodeError, TypeError):
            out.append({})
    return out


# ---------------------------------------------------------------------------
# unit: profile_has_terminal (the capability primitive)
# ---------------------------------------------------------------------------


def test_profile_has_terminal_true_when_toolsets_include_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profmod = _profiles()
    root = tmp_path / "profiles"
    pdir = root / "synth-terminal"
    pdir.mkdir(parents=True)
    (pdir / "config.yaml").write_text(
        "toolsets:\n  - hermes-cli\n  - terminal\n", encoding="utf-8"
    )
    monkeypatch.setattr(profmod, "_get_profiles_root", lambda: root)
    assert profmod.profile_has_terminal("synth-terminal", _cache={}) is True


def test_profile_has_terminal_false_when_terminal_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profmod = _profiles()
    root = tmp_path / "profiles"
    pdir = root / "synth-noterm"
    pdir.mkdir(parents=True)
    (pdir / "config.yaml").write_text(
        "toolsets:\n  - hermes-cli\n  - kanban\n", encoding="utf-8"
    )
    monkeypatch.setattr(profmod, "_get_profiles_root", lambda: root)
    assert profmod.profile_has_terminal("synth-noterm", _cache={}) is False


def test_profile_has_terminal_fail_closed_on_missing_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profmod = _profiles()
    root = tmp_path / "profiles"
    root.mkdir()
    monkeypatch.setattr(profmod, "_get_profiles_root", lambda: root)
    assert profmod.profile_has_terminal("does-not-exist", _cache={}) is False


# ---------------------------------------------------------------------------
# integration: dispatch_once review loop
# ---------------------------------------------------------------------------


def test_review_card_non_terminal_not_spawned_and_audited(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(2a)+(2b) core acceptance for the dispatcher gate."""
    profmod = _profiles()
    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(
        profmod, "profile_has_terminal", lambda name: name == "cap-reviewer"
    )

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="REVIEW: capability gate", assignee="no-term")
        _set_status(conn, tid, "review")
        spy_calls: list[str] = []

        def spy(task, workspace, board=None):
            spy_calls.append(getattr(task, "id", str(task)))
            return 424242

        result = kb.dispatch_once(conn, spawn_fn=spy)

    assert tid not in [s[0] for s in result.spawned]
    assert tid not in spy_calls
    assert tid in result.skipped_reviewer_incapable
    # not mis-bucketed
    assert tid not in result.skipped_nonspawnable

    with kb.connect() as conn:
        events = _capability_events(conn, tid)
    assert len(events) >= 1
    ev = events[0]
    assert ev.get("assignee") == "no-term"
    assert "terminal" in (ev.get("reason") or "").lower()

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "review"
    assert task.claim_lock is None


def test_review_card_terminal_capable_is_spawned(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(2c) positive control — gate must not over-block capable reviewers."""
    profmod = _profiles()
    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(profmod, "profile_has_terminal", lambda name: True)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="REVIEW: capable", assignee="cap-reviewer")
        _set_status(conn, tid, "review")
        spy_calls: list[str] = []

        def spy(task, workspace, board=None):
            spy_calls.append(getattr(task, "id", str(task)))
            return 424242

        result = kb.dispatch_once(conn, spawn_fn=spy)

    assert tid in spy_calls
    assert any(s[0] == tid for s in result.spawned)
    assert tid not in result.skipped_reviewer_incapable
    with kb.connect() as conn:
        assert not _capability_events(conn, tid)


def test_ready_non_review_ignores_terminal_gate(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(2d) ready-column work is not subject to the review capability gate.

    Documented behavior: the gate is only in the ``status='review'`` loop
    (kanban_db.py). A non-terminal assignee on ordinary ready work must still
    spawn.
    """
    profmod = _profiles()
    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(profmod, "profile_has_terminal", lambda name: False)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="FIX: ordinary ready work", assignee="builder")
        assert kb.get_task(conn, tid).status == "ready"
        spy_calls: list[str] = []

        def spy(task, workspace, board=None):
            spy_calls.append(getattr(task, "id", str(task)))
            return 424242

        result = kb.dispatch_once(conn, spawn_fn=spy)

    assert tid in spy_calls
    assert any(s[0] == tid for s in result.spawned)
    assert tid not in result.skipped_reviewer_incapable


def test_review_capability_gate_dry_run_still_buckets(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dry_run must not write events but should still classify."""
    profmod = _profiles()
    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(profmod, "profile_has_terminal", lambda name: False)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="REVIEW dry", assignee="no-term")
        _set_status(conn, tid, "review")
        result = kb.dispatch_once(conn, dry_run=True)

    assert tid in result.skipped_reviewer_incapable
    with kb.connect() as conn:
        assert not _capability_events(conn, tid)


def test_review_skips_nonexistent_profile_before_capability(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """profile_exists False -> skipped_nonspawnable, not reviewer_incapable."""
    profmod = _profiles()
    monkeypatch.setattr(profmod, "profile_exists", lambda name: False)
    monkeypatch.setattr(profmod, "profile_has_terminal", lambda name: True)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="REVIEW phantom", assignee="orion-cc")
        _set_status(conn, tid, "review")
        result = kb.dispatch_once(conn, dry_run=True)

    assert tid in result.skipped_nonspawnable
    assert tid not in result.skipped_reviewer_incapable


def test_review_capability_gate_audit_event_fields(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejection audit entry carries assignee + reason (acceptance 2b durable)."""
    profmod = _profiles()
    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(
        profmod, "profile_has_terminal", lambda name: name == "cap-reviewer"
    )

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="REVIEW: audit fields", assignee="no-term")
        _set_status(conn, tid, "review")
        kb.dispatch_once(conn)

    with kb.connect() as conn:
        events = _capability_events(conn, tid)
    assert len(events) == 1
    ev = events[0]
    assert ev.get("assignee") == "no-term"
    assert "non-terminal" in (ev.get("reason") or "").lower()
