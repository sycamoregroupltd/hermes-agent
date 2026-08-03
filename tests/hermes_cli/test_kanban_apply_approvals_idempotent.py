"""Regression tests: apply_approvals auto-clear rules.

t_jarvis_autopromote_20260728 — the approval-auto-clear loop. A card
carrying a historical REVIEW_VERDICT=APPROVED comment was auto-cleared
(unblocked -> promoted -> claimed) EVERY time it re-blocked for a new
reason (24h soak gate, needs_input park), because apply_approvals kept
re-firing on the same old approval comment_id. An approval verdict
covers what it reviewed; a later block for a different reason is a new
gate the stale verdict must not defeat.

t_552cc9e1 — the approval marker must be ANCHORED (not a quoted/cited
substring), must not clear a human-authority hold (needs_input /
capability), and must not let a worker approve its own block. These
tests pin those corrected rules.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    return tmp_path


def _approved_blocked_card(conn, *, assignee=None, author="reviewer") -> str:
    tid = kb.create_task(conn, title="reviewed card", assignee=assignee)
    # A genuine anchored verdict line from a reviewer clears a review hold.
    kb.add_comment(conn, tid, author, "REVIEW_VERDICT=APPROVED looks good")
    kb.claim_task(conn, tid)
    kb.block_task(
        conn,
        tid,
        reason="first block",
        expected_run_id=kb.get_task(conn, tid).current_run_id,
    )
    return tid


# ---------------------------------------------------------------------------
# Idempotence (unchanged contract)
# ---------------------------------------------------------------------------


def test_apply_approvals_clears_approved_card_once(kanban_home):
    with kb.connect() as conn:
        tid = _approved_blocked_card(conn)
        cleared = kb.apply_approvals(conn)
        assert tid in cleared
        assert kb.get_task(conn, tid).status == "todo"


def test_apply_approvals_does_not_refire_on_same_approval(kanban_home):
    """The core regression: same approval comment_id must not clear twice."""
    with kb.connect() as conn:
        tid = _approved_blocked_card(conn)
        first = kb.apply_approvals(conn)
        assert tid in first

        # Card re-blocks for a NEW reason (e.g. soak gate / needs_input).
        kb.recompute_ready(conn)  # todo -> ready so it can be claimed
        kb.claim_task(conn, tid)
        kb.block_task(
            conn,
            tid,
            reason="24h soak gate (different reason)",
            kind="needs_input",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # Stale approval must NOT clear it again.
        second = kb.apply_approvals(conn)
        assert tid not in second
        assert kb.get_task(conn, tid).status == "blocked"


# ---------------------------------------------------------------------------
# t_552cc9e1 corrections
# ---------------------------------------------------------------------------


def test_apply_approvals_quoted_marker_does_not_clear(kanban_home):
    """A verdict quoted/cited in narrative prose about ANOTHER card must not
    clear this card (the live t_15b7ebc4 trigger)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="reviewed card")
        # The prose CITED the literal token while narrating a different card.
        kb.add_comment(
            conn, tid, "reviewer",
            "t_f31ab4c4 was marked done under a normal REVIEW_VERDICT=APPROVED + "
            "130 PASS/0 FAIL selftest; this card is different.",
        )
        kb.claim_task(conn, tid)
        kb.block_task(
            conn, tid,
            reason="review-required",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        cleared = kb.apply_approvals(conn)
        assert tid not in cleared
        assert kb.get_task(conn, tid).status == "blocked"


def test_apply_approvals_marker_in_code_fence_does_not_clear(kanban_home):
    """A verdict inside a fenced code block is a quotation, not a verdict."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="reviewed card")
        kb.add_comment(
            conn, tid, "reviewer",
            "see the other card:\n```\nREVIEW_VERDICT=APPROVED\n```\n"
            "that was a different card's result.",
        )
        kb.claim_task(conn, tid)
        kb.block_task(
            conn, tid,
            reason="review-required",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        cleared = kb.apply_approvals(conn)
        assert tid not in cleared
        assert kb.get_task(conn, tid).status == "blocked"


def test_apply_approvals_unanchored_marker_does_not_clear(kanban_home):
    """A marker buried mid-line (not at line start) does not count."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="reviewed card")
        kb.add_comment(
            conn, tid, "reviewer",
            "the result was REVIEW_VERDICT=APPROVED which is fine",
        )
        kb.claim_task(conn, tid)
        kb.block_task(
            conn, tid,
            reason="review-required",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        cleared = kb.apply_approvals(conn)
        assert tid not in cleared
        assert kb.get_task(conn, tid).status == "blocked"


def test_apply_approvals_self_approval_does_not_clear(kanban_home):
    """Separation of duties (t_cb5a275a): the blocked card's own assignee
    may not approve its own block, even with a valid anchored verdict."""
    with kb.connect() as conn:
        tid = _approved_blocked_card(conn, assignee="devops", author="devops")
        cleared = kb.apply_approvals(conn)
        assert tid not in cleared
        assert kb.get_task(conn, tid).status == "blocked"


def test_apply_approvals_needs_input_does_not_clear(kanban_home):
    """Human-authority gate (t_552cc9e1 / t_15b7ebc4): a needs_input hold is
    never auto-cleared by an approval comment, even from a real reviewer with
    a valid anchored verdict."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="needs frank decision")
        kb.add_comment(conn, tid, "reviewer", "REVIEW_VERDICT=APPROVED looks good")
        kb.claim_task(conn, tid)
        kb.block_task(
            conn, tid,
            reason="awaiting Frank decision",
            kind="needs_input",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        cleared = kb.apply_approvals(conn)
        assert tid not in cleared
        assert kb.get_task(conn, tid).status == "blocked"


def test_apply_approvals_capability_does_not_clear(kanban_home):
    """capability holds are human-only and must never auto-clear."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="needs credential")
        kb.add_comment(conn, tid, "reviewer", "REVIEW_VERDICT=APPROVED looks good")
        kb.claim_task(conn, tid)
        kb.block_task(
            conn, tid,
            reason="missing credential",
            kind="capability",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        cleared = kb.apply_approvals(conn)
        assert tid not in cleared
        assert kb.get_task(conn, tid).status == "blocked"


def test_apply_approvals_independent_reviewer_still_clears(kanban_home):
    """No regression: a genuine anchored verdict from an independent reviewer
    on a review-class (un-typed) block still clears (the intended feature)."""
    with kb.connect() as conn:
        tid = _approved_blocked_card(conn, assignee="devops", author="os-reviewer")
        cleared = kb.apply_approvals(conn)
        assert tid in cleared
        assert kb.get_task(conn, tid).status == "todo"


def test_apply_approvals_reopen_marker_still_respected(kanban_home):
    """Existing re-open guard must keep working alongside idempotence."""
    with kb.connect() as conn:
        tid = _approved_blocked_card(conn)
        kb.add_comment(conn, tid, "reviewer", "REVIEW_VERDICT=CHANGES_REQUESTED re-opened")
        cleared = kb.apply_approvals(conn)
        assert tid not in cleared
        assert kb.get_task(conn, tid).status == "blocked"
