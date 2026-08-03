#!/usr/bin/env python3
"""Regression tests for verdict_vocabulary_detector.py.

Mirrors test_verdict_router.py: builds a fixture kanban DB, inserts tasks and
comments with malformed/valid verdicts, and asserts detection / non-detection
according to the contract in task t_1d6ed4c0.

Acceptance (from the task body):
- Detects APPROVE_WITH_NOTES / CHANGES_REQUESTED_FOR_DOCS style malformed verdicts.
- Zero false positives on valid verdicts (APPROVED/APPROVE/CHANGES_REQUESTED/REJECT).
- Only flags status in {blocked, review} and {running} cards with ONLY a malformed verdict.
- Only flags verdicts older than 1h with no later valid verdict.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import verdict_vocabulary_detector as vd


class VerdictVocabDetectorTests(unittest.TestCase):
    def make_board(self) -> tuple[tempfile.TemporaryDirectory[str], tuple[str, Path]]:
        tmp = tempfile.TemporaryDirectory(prefix="verdict-vocab-detector-test-")
        db = Path(tmp.name) / "kanban.db"
        con = sqlite3.connect(db)
        con.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT,
                assignee TEXT,
                status TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL,
                block_kind TEXT
            );
            CREATE TABLE task_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                author TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        con.commit()
        con.close()
        return tmp, ("fixture", db)

    def insert_task(self, board: tuple[str, Path], task_id: str, status: str,
                    title: str = "review-required source patch", body: str = "review-required source") -> None:
        _, db = board
        con = sqlite3.connect(db)
        con.execute(
            "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (task_id, title, body, "devops", status, 10, int(time.time())),
        )
        con.commit()
        con.close()

    def add_comment(self, board: tuple[str, Path], task_id: str, author: str, body: str,
                    age_seconds: int = 7200) -> int:
        """Add a comment with created_at = now - age_seconds (default > 1h)."""
        _, db = board
        created = int(time.time()) - age_seconds
        con = sqlite3.connect(db)
        cur = con.execute(
            "INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,?,?,?)",
            (task_id, author, body, created),
        )
        con.commit()
        assert cur.lastrowid is not None
        cid = int(cur.lastrowid)
        con.close()
        return cid

    def scan(self, board: tuple[str, Path]) -> list[vd.Finding]:
        return vd.detect_malformed_verdicts(boards_override=[board])

    # --- Detection cases -------------------------------------------------

    def test_detect_approve_with_notes_on_blocked(self) -> None:
        """t_6c6f3d57-style: APPROVE_WITH_NOTES on a blocked card is flagged."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_malformed01", "blocked")
            self.add_comment(board, "t_malformed01", "os-reviewer",
                             "REVIEW_VERDICT=APPROVE_WITH_NOTES\nLooks good, minor notes.")
            findings = self.scan(board)
            self.assertEqual(len(findings), 1)
            f = findings[0]
            self.assertEqual(f.board, "fixture")
            self.assertEqual(f.task_id, "t_malformed01")
            self.assertEqual(f.verdict_value, "APPROVE_WITH_NOTES")
            self.assertEqual(f.task_status, "blocked")
            self.assertGreater(f.age_seconds, vd.MIN_AGE_SECONDS)

    def test_detect_changes_requested_for_docs_on_review(self) -> None:
        """t_59daece5-style: CHANGES_REQUESTED_FOR_DOCS on a review card is flagged."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_malformed02", "review")
            self.add_comment(board, "t_malformed02", "os-reviewer",
                             "REVIEW_VERDICT=CHANGES_REQUESTED_FOR_DOCS\nPlease expand the docs.")
            findings = self.scan(board)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].verdict_value, "CHANGES_REQUESTED_FOR_DOCS")
            self.assertEqual(findings[0].task_status, "review")

    def test_detect_rejected_alt_spelling_treated_as_valid(self) -> None:
        """REJECTED (router normalizes to REJECT) must NOT be flagged — no false positive."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_rejectalt", "blocked")
            self.add_comment(board, "t_rejectalt", "os-reviewer", "REVIEW_VERDICT=REJECTED\nNot for this scope.")
            self.assertEqual(self.scan(board), [])

    # --- Non-detection cases (valid verdicts) -----------------------------

    def test_no_false_positive_approved(self) -> None:
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_ok_approved", "blocked")
            self.add_comment(board, "t_ok_approved", "os-reviewer", "REVIEW_VERDICT=APPROVED\nTarget: t_ok_approved")
            self.assertEqual(self.scan(board), [])

    def test_no_false_positive_approve(self) -> None:
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_ok_approve", "blocked")
            self.add_comment(board, "t_ok_approve", "os-reviewer", "REVIEW_VERDICT=APPROVE\nTarget: t_ok_approve")
            self.assertEqual(self.scan(board), [])

    def test_no_false_positive_changes_requested(self) -> None:
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_ok_cr", "blocked")
            self.add_comment(board, "t_ok_cr", "os-reviewer", "REVIEW_VERDICT=CHANGES_REQUESTED\nFinding: rework")
            self.assertEqual(self.scan(board), [])

    def test_no_false_positive_reject(self) -> None:
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_ok_reject", "blocked")
            self.add_comment(board, "t_ok_reject", "os-reviewer", "REVIEW_VERDICT=REJECT\nNo.")
            self.assertEqual(self.scan(board), [])

    # --- Status / age / idempotency gates --------------------------------

    def test_running_card_with_valid_and_malformed_not_flagged(self) -> None:
        """Running card that ALSO has a valid verdict is NOT in scope (router can route)."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_run_valid", "running")
            self.add_comment(board, "t_run_valid", "devops", "working on it", age_seconds=9000)
            self.add_comment(board, "t_run_valid", "os-reviewer",
                             "REVIEW_VERDICT=APPROVED\nTarget: t_run_valid", age_seconds=8000)
            self.add_comment(board, "t_run_valid", "os-reviewer", "REVIEW_VERDICT=APPROVE_WITH_NOTES\nnotes",
                             age_seconds=7200)
            self.assertEqual(self.scan(board), [])

    def test_running_card_with_only_malformed_flagged(self) -> None:
        """Running card with ONLY a malformed verdict (no valid) IS flagged (closes stuck-running branch)."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_run_only", "running")
            self.add_comment(board, "t_run_only", "os-reviewer", "REVIEW_VERDICT=APPROVE_WITH_NOTES\nonly notes")
            findings = self.scan(board)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].task_status, "running")
            self.assertEqual(findings[0].verdict_value, "APPROVE_WITH_NOTES")

    def test_transient_under_1h_not_flagged(self) -> None:
        """A malformed verdict younger than 1h is not flagged (transient in-flight review)."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_fresh", "blocked")
            self.add_comment(board, "t_fresh", "os-reviewer", "REVIEW_VERDICT=APPROVE_WITH_NOTES\nfresh",
                             age_seconds=600)
            self.assertEqual(self.scan(board), [])

    def test_later_valid_verdict_suppresses_earlier_malformed(self) -> None:
        """A later valid verdict resolves an earlier malformed one (idempotency / no re-flag)."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_resolved", "blocked")
            self.add_comment(board, "t_resolved", "os-reviewer", "REVIEW_VERDICT=APPROVE_WITH_NOTES\nfirst (bad)",
                             age_seconds=9000)
            self.add_comment(board, "t_resolved", "os-reviewer", "REVIEW_VERDICT=APPROVED\nTarget: t_resolved",
                             age_seconds=3600)
            self.assertEqual(self.scan(board), [])

    def test_malformed_verdict_with_no_later_valid_is_flagged_notwithstanding_earlier_valid(self) -> None:
        """Valid verdict first, malformed later (no newer valid) -> malformed flagged."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_interleaved", "blocked")
            self.add_comment(board, "t_interleaved", "os-reviewer", "REVIEW_VERDICT=APPROVED\nTarget: t_interleaved",
                             age_seconds=9000)
            self.add_comment(board, "t_interleaved", "os-reviewer", "REVIEW_VERDICT=APPROVE_WITH_NOTES\nchanged my mind",
                             age_seconds=7200)
            findings = self.scan(board)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].verdict_value, "APPROVE_WITH_NOTES")


class VerdictVocabDetectorReplayTests(unittest.TestCase):
    """Live-state replay acceptance: a fixture board seeded with the two known
    real instances is detected with board/card/comment id/value/age."""

    def test_replay_real_instances(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="verdict-vocab-replay-")
        db = Path(tmp.name) / "kanban.db"
        con = sqlite3.connect(db)
        con.executescript(
            """
            CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT, priority INTEGER, created_at INTEGER, block_kind TEXT);
            CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, author TEXT, body TEXT, created_at INTEGER);
            """
        )
        now = int(time.time())
        # sycode-ai/t_6c6f3d57 (blocked, APPROVE_WITH_NOTES)
        con.execute("INSERT INTO tasks(id,title,status,created_at) VALUES (?,?,?,?)",
                    ("t_6c6f3d57", "real instance A", "blocked", now - 7200))
        con.execute("INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,?,?,?)",
                    ("t_6c6f3d57", "os-reviewer", "REVIEW_VERDICT=APPROVE_WITH_NOTES\nnotes", now - 7000))
        # sycode-ai/t_59daece5 (blocked, APPROVED_WITH_NOTES)
        con.execute("INSERT INTO tasks(id,title,status,created_at) VALUES (?,?,?,?)",
                    ("t_59daece5", "real instance B", "blocked", now - 7200))
        con.execute("INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,?,?,?)",
                    ("t_59daece5", "os-reviewer", "REVIEW_VERDICT=APPROVED_WITH_NOTES\nnotes", now - 7000))
        con.commit()
        con.close()
        try:
            findings = vd.detect_malformed_verdicts(now=now, boards_override=[("sycode-ai", db)])
            by_id = {f.task_id: f for f in findings}
            self.assertIn("t_6c6f3d57", by_id)
            self.assertIn("t_59daece5", by_id)
            self.assertEqual(by_id["t_6c6f3d57"].verdict_value, "APPROVE_WITH_NOTES")
            self.assertEqual(by_id["t_59daece5"].verdict_value, "APPROVED_WITH_NOTES")
            for f in findings:
                self.assertEqual(f.board, "sycode-ai")
                self.assertGreater(f.comment_id, 0)
                self.assertGreater(f.age_seconds, vd.MIN_AGE_SECONDS)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
