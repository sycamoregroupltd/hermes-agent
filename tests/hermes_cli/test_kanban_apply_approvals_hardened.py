"""Regression tests for apply_approvals() hardening (t_552cc9e1, t_jarvis_autopromote_20260728).

Verifies that the auto-clear hook cannot false-unblock intentionally-blocked
coordination cards when a peer report merely quotes the verdict token in prose.

Three hardening gates exercised here:

1. Peer DONE report quoting ``<VERDICT>=APPROVED`` in prose MUST NOT unblock a
   blocked card.
2. Cards blocked with ``needs_input`` / ``capability`` block_kind are protected
   from auto-clear by any approval comment.
3. Self-approval by the task assignee (non-reviewer) does NOT clear; reviewer
   roles on the allowlist DO clear their own blocks (separation of duties).

All tests use isolated temp kanban DBs — no live state is touched.
"""

from __future__ import annotations

import os
import tempfile
import shutil
from contextlib import contextmanager

import pytest

from pathlib import Path

from hermes_cli import kanban_db as kb


@contextmanager
def _isolated_board():
    """Yield a single-board DB Path inside a temporary HERMES_KANBAN_HOME."""
    tmpdir = tempfile.mkdtemp()
    old_home = os.environ.get("HERMES_KANBAN_HOME")
    try:
        os.environ["HERMES_KANBAN_HOME"] = tmpdir
        kb.create_board(slug="testboard")
        db_path = Path(tmpdir) / "kanban" / "testboard.db"
        yield db_path
    finally:
        if old_home is None:
            os.environ.pop("HERMES_KANBAN_HOME", None)
        else:
            os.environ["HERMES_KANBAN_HOME"] = old_home
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_applv_approved_clears_normal_block():
    """Normal review-required block with reviewer APPROVED clears to todo."""
    with _isolated_board() as db_path:
        conn = kb.connect(db_path=db_path)
        try:
            tid = kb.create_task(conn, title="FIX: typo", assignee="devops")
            kb.block_task(conn, tid, reason="needs-review")
            kb.add_comment(conn, tid, author="reviewer", body="REVIEW_VERDICT=APPROVED")
            cleared = kb.apply_approvals(conn)
            assert tid in cleared, f"Expected {tid} to be cleared"
            row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
            assert row["status"] == "todo"
        finally:
            conn.close()


def test_peer_done_report_quoting_verdict_does_not_clear():
    """[REGRESSION] Peer DONE batch report quoting '<VERDICT>=APPROVED' in prose
    must NOT unblock an intentionally-blocked coordination bus card."""
    with _isolated_board() as db_path:
        conn = kb.connect(db_path=db_path)
        try:
            bus_tid = kb.create_task(
                conn,
                title="ORCH-LIVE peer orchestrator coordination bus",
                body="coordination bus only; dispatcher-free board; never dispatch this card",
                assignee="jarvis-os-pm",
            )
            kb.block_task(conn, bus_tid, reason="coordination bus manual gate")

            prose_comment = (
                "DONE batch report: two <VERDICT>=APPROVED tokens on comments\n"
                "t_aaaaaaaa (fix-typos): approved by reviewer.\n"
                "t_bbbbbbbb (update-readme): approved by guardian.\n"
                "All items merged."
            )
            kb.add_comment(conn, bus_tid, author="jarvis-cli", body=prose_comment)

            cleared = kb.apply_approvals(conn)
            assert bus_tid not in cleared, (
                f"Bus card {bus_tid} was auto-cleared by a peer DONE report "
                f"quoting the verdict token in prose."
            )
            row = conn.execute("SELECT status FROM tasks WHERE id=?", (bus_tid,)).fetchone()
            assert row["status"] == "blocked"
        finally:
            conn.close()


def test_needs_input_block_not_cleared_by_prose_token():
    """Cards blocked with needs_input kind are NEVER auto-cleared by any
    approval comment, even if properly anchored and authored by a reviewer."""
    with _isolated_board() as db_path:
        conn = kb.connect(db_path=db_path)
        try:
            tid = kb.create_task(conn, title="Needs human decision", assignee="devops")
            kb.block_task(conn, tid, reason="needs input", kind="needs_input")
            kb.add_comment(conn, tid, author="reviewer", body="REVIEW_VERDICT=APPROVED")
            cleared = kb.apply_approvals(conn)
            assert tid not in cleared, (
                f"needs_input card {tid} should NOT be auto-cleared"
            )
        finally:
            conn.close()


def test_capability_block_not_cleared_by_prose_token():
    """Cards blocked with capability kind are NEVER auto-cleared by any
    approval comment."""
    with _isolated_board() as db_path:
        conn = kb.connect(db_path=db_path)
        try:
            tid = kb.create_task(conn, title="Missing credential", assignee="devops")
            kb.block_task(conn, tid, reason="missing auth", kind="capability")
            kb.add_comment(conn, tid, author="guardian", body="REVIEW_VERDICT=APPROVED")
            cleared = kb.apply_approvals(conn)
            assert tid not in cleared, (
                f"capability card {tid} should NOT be auto-cleared"
            )
        finally:
            conn.close()


def test_self_approval_does_not_clear_non_reviewer():
    """The task's own assignee (non-reviewer role) cannot approve its OWN block."""
    with _isolated_board() as db_path:
        conn = kb.connect(db_path=db_path)
        try:
            tid = kb.create_task(conn, title="My own work", assignee="devops")
            kb.block_task(conn, tid, reason="needs-review")
            kb.add_comment(conn, tid, author="devops", body="REVIEW_VERDICT=APPROVED")
            cleared = kb.apply_approvals(conn)
            assert tid not in cleared, (
                f"Self-approved card {tid} should NOT clear"
            )
        finally:
            conn.close()


def test_reviewer_self_approval_does_clear():
    """A reviewer-role profile CAN approve their own block — they're on the
    separation-of-duties allowlist."""
    with _isolated_board() as db_path:
        conn = kb.connect(db_path=db_path)
        try:
            tid = kb.create_task(conn, title="Review code", assignee="reviewer")
            kb.block_task(conn, tid, reason="review")
            kb.add_comment(conn, tid, author="reviewer", body="REVIEW_VERDICT=APPROVED")
            cleared = kb.apply_approvals(conn)
            assert tid in cleared, (
                f"Reviewer self-approval should clear {tid}"
            )
        finally:
            conn.close()


def test_marker_in_code_fence_does_not_clear():
    """An APPROVED marker appearing ONLY inside a fenced code block is ignored.
    This prevents quoted examples from accidentally unblocking cards."""
    with _isolated_board() as db_path:
        conn = kb.connect(db_path=db_path)
        try:
            tid = kb.create_task(conn, title="Fenced quote", assignee="devops")
            kb.block_task(conn, tid, reason="review")
            fenced = (
                "See example below:\n"
                "```\n"
                "REVIEW_VERDICT=APPROVED\n"
                "```\n"
                "That was just a quote."
            )
            kb.add_comment(conn, tid, author="reviewer", body=fenced)
            cleared = kb.apply_approvals(conn)
            assert tid not in cleared, (
                f"Fenced-code verdict should NOT clear {tid}"
            )
        finally:
            conn.close()


def test_idempotent_same_approval_doesnt_refire():
    """Once an approval comment has already auto-cleared a card, re-blocking
    the card for a DIFFERENT reason must NOT be cleared by the same stale
    approval — idempotence per comment_id."""
    with _isolated_board() as db_path:
        conn = kb.connect(db_path=db_path)
        try:
            tid = kb.create_task(conn, title="Idempotent", assignee="devops")
            kb.block_task(conn, tid, reason="review")
            kb.add_comment(conn, tid, author="reviewer", body="REVIEW_VERDICT=APPROVED")

            # First clearance succeeds.
            cleared1 = kb.apply_approvals(conn)
            assert tid in cleared1, "First clearance should succeed"

            # Re-block for a completely different reason.
            kb.block_task(conn, tid, reason="new issue found")

            # Same approval comment must NOT fire again.
            cleared2 = kb.apply_approvals(conn)
            assert tid not in cleared2, (
                f"Approval should not re-fire for {tid} after re-block"
            )
        finally:
            conn.close()
