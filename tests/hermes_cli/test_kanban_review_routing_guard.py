"""Tests for the dispatcher reviewer-routing guard (t_fddfa577).

Review cards (REVIEW / REWORK / RISK-VERDICT) may only be dispatched to a
terminal-capable reviewer profile. The dispatcher must reject any review card
whose effective assignee is not in ``TERMINAL_CAPABLE_REVIEWER_PROFILES``
BEFORE claim/spawn, write a ``routing_rejected`` audit entry to
``task_events``, and record the rejection in
``DispatchResult.skipped_nonterminal_review``. Non-review cards are routed
normally, unchanged.

Spec: reviewer-routing-policy-spec-t_16b409d5 (parent t_16b409d5).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

# Non-terminal profiles that must be rejected when assigned a review card.
NON_TERMINAL_PROFILES = [
    "jarvis",
    "integration-builder",
    "research-trading",
    "test-engineer",
]


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Spin up a fresh HERMES_HOME with a clean kanban DB."""
    test_home = tempfile.mkdtemp(prefix="kanban_review_routing_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    # Force-reimport so the fresh HERMES_HOME is picked up.
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db, test_home


@pytest.fixture()
def make_all_profiles_exist(monkeypatch):
    """Make profile_exists return True so the guard's non-guard checks pass.

    The guard-under-test is the terminal-capable allowlist, not filesystem
    profile presence, so we stub ``profile_exists`` to treat every assignee as
    an existing profile. Without this, the isolated HERMES_HOME only knows the
    ``default`` profile and every named profile would be bucketed
    ``skipped_nonspawnable`` before we could observe a successful spawn.
    """
    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    yield


def _fake_spawn(*args, **kwargs):
    """Stand-in for the real worker spawn — returns a fake PID."""
    return 12345


def _create_review_card(kb, conn, *, title, assignee, body=None):
    return kb.create_task(
        conn, title=title, body=body or "", assignee=assignee,
    )


def test_review_card_allowed_terminal_profile_succeeds(
    isolated_kanban_home, make_all_profiles_exist,
):
    """A review card assigned to a terminal-capable profile routes normally."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = _create_review_card(
            kb, conn, title="REVIEW: verify x", assignee="os-reviewer",
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False, board="default",
        )
    assert task_id not in [r[0] for r in res.skipped_nonterminal_review]
    assert any(s[0] == task_id and s[1] == "os-reviewer" for s in res.spawned)


@pytest.mark.parametrize("profile", NON_TERMINAL_PROFILES)
def test_each_disallowed_profile_rejected(
    isolated_kanban_home, make_all_profiles_exist, profile,
):
    """Every non-terminal-capable profile is rejected for a review card."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = _create_review_card(
            kb, conn, title="REVIEW: check audit", assignee=profile,
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False, board="default",
        )
    assert task_id not in [s[0] for s in res.spawned]
    assert (task_id, profile, kb.NON_TERMINAL_REVIEW_REASON) in (
        res.skipped_nonterminal_review
    )


@pytest.mark.parametrize(
    ("title", "body", "expected_type"),
    [
        ("REVIEW: verify routing", "", "REVIEW"),
        ("re-review: second pass", "", "REVIEW"),
        ("Rework: change the loop", "", "REWORK"),
        ("Plain implement card", "REWORK_REQUIRED", "REWORK"),
        ("Verdict: accept", "", "RISK-VERDICT"),
        ("Some other title", "marker REVIEW_VERDICT here", "RISK-VERDICT"),
    ],
)
def test_rejection_writes_required_audit_entry(
    isolated_kanban_home, make_all_profiles_exist,
    title, body, expected_type,
):
    """Each rejection writes a routing_rejected event with required fields."""
    kb, _home = isolated_kanban_home
    profile = "integration-builder"
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = _create_review_card(
            kb, conn, title=title, assignee=profile, body=body,
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False, board="default",
        )
    assert (task_id, profile, kb.NON_TERMINAL_REVIEW_REASON) in (
        res.skipped_nonterminal_review
    )
    with kb.connect_closing() as conn:
        evs = list(conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'routing_rejected'",
            (task_id,),
        ))
    assert len(evs) == 1
    payload = json.loads(evs[0][1])
    assert payload["card_id"] == task_id
    assert payload["target_profile"] == profile
    assert payload["reason"] == kb.NON_TERMINAL_REVIEW_REASON
    assert payload["card_type"] == expected_type
    assert payload["board"] == "default"


def test_unassigned_review_card_rejected(
    isolated_kanban_home, make_all_profiles_exist,
):
    """An unassigned review card is rejected (empty is not terminal-capable).

    It must NOT be auto-assigned to kanban.default_assignee (which may be a
    non-terminal profile) and spawned.
    """
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = _create_review_card(
            kb, conn, title="REVIEW: unassigned", assignee=None,
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="integration-builder", board="default",
        )
    assert task_id not in [s[0] for s in res.spawned]
    assert task_id not in res.auto_assigned_default
    assert (task_id, "integration-builder", kb.NON_TERMINAL_REVIEW_REASON) in (
        res.skipped_nonterminal_review
    )
    # The DB row must not have been mutated with the default assignee.
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
    assert row["assignee"] is None


def test_no_review_card_assigned_to_nonterminal_profile(
    isolated_kanban_home, make_all_profiles_exist,
):
    """Invariant: no REVIEW/REWORK/RISK-VERDICT card spawns to a non-terminal.

    Builds a mixed queue of review and non-review cards; every review card
    must either spawn to a terminal-capable profile or be rejected — never
    spawned to a non-terminal-capable profile.
    """
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        review_bad = _create_review_card(
            kb, conn, title="REVIEW: x", assignee="jarvis",
        )
        review_good = _create_review_card(
            kb, conn, title="Rework: y", assignee="guardian",
        )
        risk_bad = _create_review_card(
            kb, conn, title="Some title", assignee="test-engineer",
            body="REVIEW_VERDICT",
        )
        normal = _create_review_card(
            kb, conn, title="Build the widget", assignee="jarvis",
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False, board="default",
        )
    spawned_ids = {s[0]: s[1] for s in res.spawned}
    # review_bad and risk_bad rejected, never spawned.
    assert review_bad not in spawned_ids
    assert risk_bad not in spawned_ids
    # review_good spawns to a terminal-capable profile.
    assert spawned_ids.get(review_good) == "guardian"
    # The plain work card (non-review) routes normally, unchanged.
    assert spawned_ids.get(normal) == "jarvis"
    # Invariant: the ONLY task spawned to a non-terminal-capable profile is
    # the non-review plain work card. No review card spawns to a non-terminal.
    for sid, assignee in spawned_ids.items():
        if sid == normal:
            # The non-review work card is exempt from the terminal gate.
            continue
        assert assignee in kb.TERMINAL_CAPABLE_REVIEWER_PROFILES


def test_non_review_card_unchanged(
    isolated_kanban_home, make_all_profiles_exist,
):
    """Cards that merely *mention* review are not treated as review cards.

    ``IMPLEMENT AFTER REVIEW ...`` does not lead with a review term and must
    route to its (possibly non-terminal) assignee unchanged.
    """
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = _create_review_card(
            kb, conn, title="IMPLEMENT AFTER REVIEW: add retry",
            assignee="integration-builder",
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False, board="default",
        )
    assert task_id not in [r[0] for r in res.skipped_nonterminal_review]
    assert any(
        s[0] == task_id and s[1] == "integration-builder"
        for s in res.spawned
    )
