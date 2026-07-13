#!/usr/bin/env python3
"""Regression fixtures for verdict_router.py safety gates."""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import verdict_router as vr


class VerdictRouterRegressionTests(unittest.TestCase):
    def make_board(self) -> tuple[tempfile.TemporaryDirectory[str], vr.Board]:
        tmp = tempfile.TemporaryDirectory(prefix="verdict-router-test-")
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
                created_at INTEGER NOT NULL
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
        return tmp, vr.Board("fixture", db)

    def insert_task(self, board: vr.Board, task_id: str, body: str = "review-required source patch") -> None:
        con = sqlite3.connect(board.db)
        con.execute(
            "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)",
            (task_id, "review-required source code patch", body, "devops", "blocked", 10, int(time.time())),
        )
        con.commit()
        con.close()

    def add_comment(self, board: vr.Board, task_id: str, author: str, body: str) -> int:
        con = sqlite3.connect(board.db)
        cur = con.execute(
            "INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,?,?,?)",
            (task_id, author, body, int(time.time())),
        )
        con.commit()
        assert cur.lastrowid is not None
        cid = int(cur.lastrowid)
        con.close()
        return cid

    def test_router_authored_missing_target_comment_is_not_reinterpreted_as_approval(self) -> None:
        tmp, board = self.make_board()
        with tmp:
            task_id = "t_aaaaaaaa"
            self.insert_task(board, task_id)
            self.add_comment(board, task_id, "os-reviewer", "REVIEW_VERDICT=APPROVED\nLooks safe but no task target.")
            first = vr.decide(vr.candidates_for_board(board)[0], dry_run=False)
            self.assertEqual(first.action, "needs_pm")

            self.add_comment(
                board,
                task_id,
                vr.AUTHOR,
                "NEEDS-PM: verdict-router left this REVIEW_VERDICT blocked for manual routing.\n"
                f"verdict-router marker={first.idempotency_key}\n"
                "REVIEW_VERDICT=APPROVED; latest_comment_id=1; reason=failed closed",
            )
            self.assertEqual(vr.candidates_for_board(board), [])

    def test_router_authored_other_task_changes_comment_is_not_reinterpreted_as_rework(self) -> None:
        tmp, board = self.make_board()
        with tmp:
            task_id = "t_bbbbbbbb"
            self.insert_task(board, task_id)
            self.add_comment(
                board,
                task_id,
                "os-reviewer",
                "REVIEW_VERDICT=CHANGES_REQUESTED for t_cccccccc\nBlocking finding belongs elsewhere.",
            )
            first = vr.decide(vr.candidates_for_board(board)[0], dry_run=False)
            self.assertEqual(first.action, "needs_pm")

            self.add_comment(
                board,
                task_id,
                vr.AUTHOR,
                "NEEDS-PM: verdict-router left this REVIEW_VERDICT blocked for manual routing.\n"
                f"verdict-router marker={first.idempotency_key}\n"
                "REVIEW_VERDICT=CHANGES_REQUESTED; latest_comment_id=1; reason=failed closed",
            )
            self.assertEqual(vr.candidates_for_board(board), [])

    def test_nonnumeric_comment_created_at_does_not_crash_scan(self) -> None:
        tmp, board = self.make_board()
        with tmp:
            task_id = "t_dddddddd"
            self.insert_task(board, task_id)
            con = sqlite3.connect(board.db)
            con.execute(
                "INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,?,?,?)",
                (task_id, "os-reviewer", "REVIEW_VERDICT=APPROVED\nTarget: t_dddddddd\nSource patch approved.", "%s"),
            )
            con.commit()
            con.close()

            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].latest_comment_created_at, 0)

    def test_nonnumeric_comment_id_is_skipped_without_crashing_scan(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="verdict-router-test-")
        with tmp:
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
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE task_comments (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )
            task_id = "t_eeeeeeee"
            con.execute(
                "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)",
                (task_id, "review-required source code patch", "review-required source patch", "devops", "blocked", 10, int(time.time())),
            )
            con.execute(
                "INSERT INTO task_comments(id,task_id,author,body,created_at) VALUES (?,?,?,?,?)",
                ("%s", task_id, "os-reviewer", "REVIEW_VERDICT=APPROVED\nTarget: t_eeeeeeee\nSource patch approved.", int(time.time())),
            )
            con.commit()
            con.close()

            self.assertEqual(vr.candidates_for_board(vr.Board("fixture", db)), [])

    def test_reject_same_card_leaves_card_blocked(self) -> None:
        """REVIEW_VERDICT=REJECT on the same card returns action='rejected' and does not complete/unblock."""
        tmp, board = self.make_board()
        with tmp:
            task_id = "t_reject01"
            self.insert_task(board, task_id)
            self.add_comment(
                board,
                task_id,
                "os-reviewer",
                "REVIEW_VERDICT=REJECT\nNot suitable for this scope. Finding: irrelevant approach.",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            decision = vr.decide(candidates[0], dry_run=False)
            self.assertEqual(decision.verdict, "REJECT")
            self.assertEqual(decision.action, "rejected")
            self.assertIn("REJECTED by reviewer on standard scope", decision.reason)
            # No task ID in comment → missing-target (CHANGES_REQUESTED special-case
            # does not apply to REJECT)
            self.assertEqual(decision.target_validation, "missing-target")
            self.assertEqual(decision.scope_class, "standard")

    def test_reject_cross_target_returns_rejected(self) -> None:
        """REVIEW_VERDICT=REJECT mentioning a different task id returns action='rejected' (not needs_pm)."""
        tmp, board = self.make_board()
        with tmp:
            task_id = "t_reject02"
            self.insert_task(board, task_id)
            self.add_comment(
                board,
                task_id,
                "os-reviewer",
                "REVIEW_VERDICT=REJECT\nTarget: t_0badc0de\nThis rejection applies to a different card's scope.",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            decision = vr.decide(candidates[0], dry_run=False)
            self.assertEqual(decision.verdict, "REJECT")
            self.assertEqual(decision.action, "rejected")
            self.assertEqual(decision.target_validation, "cross-target")

    def test_reject_a3_gated_returns_rejected_with_operator_scope(self) -> None:
        """REVIEW_VERDICT=REJECT on an A3/operator-gated task returns action='rejected' with operator_gated scope."""
        tmp, board = self.make_board()
        with tmp:
            task_id = "t_reject03"
            con = sqlite3.connect(board.db)
            con.execute(
                "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)",
                (task_id, "deploy config change", "review-required deploy scope change for prod config", "devops", "blocked", 10, int(time.time())),
            )
            con.commit()
            con.close()
            self.add_comment(
                board,
                task_id,
                "os-reviewer",
                "REVIEW_VERDICT=REJECT\nDeploy scope changes should not go through. Blocking finding: prod impact.",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            decision = vr.decide(candidates[0], dry_run=False)
            self.assertEqual(decision.verdict, "REJECT")
            self.assertEqual(decision.action, "rejected")
            self.assertEqual(decision.scope_class, "operator_gated")
            self.assertIn("A3/operator-gated scope", decision.reason)

    def test_parse_verdict_canonicalizes_rejected_to_reject(self) -> None:
        """parse_verdict() normalizes both REJECT and REJECTED to REJECT."""
        self.assertEqual(vr.parse_verdict("REVIEW_VERDICT=REJECT\nsome reason"), "REJECT")
        self.assertEqual(vr.parse_verdict("REVIEW_VERDICT=REJECTED\nsome reason"), "REJECT")

    def test_doc_only_card_with_db_in_prose_auto_completes_not_operator_gated(self) -> None:
        """Regression for t_28219e67: a doc-only additive skill patch whose body/reviewer
        prose mentions DB/corruption (KanbanDbCorruptError, 'kanban DB', 'DB operation',
        'migration performed') must NOT be flagged operator_gated on the bare 'DB' token.

        The card performs no live DB op/recovery/migration/deploy — it is markdown documentation.
        Before the fix, FORBIDDEN_SCOPE_RE contained a bare ``\\bdb\\b`` token that matched
        'DB' inside the guidance prose and forced a false-positive needs_operator block.
        """
        task_id = "t_581ca2e9"
        tmp, board = self.make_board()
        with tmp:
            con = sqlite3.connect(board.db)
            con.execute(
                "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    task_id,
                    "BORIS-PATCH: kanban skill — quarantined corrupt-DB circuit-breaker (stop retry storm)",
                    "When a board op fails with `KanbanDbCorruptError` (refusing to open corrupt kanban DB ...). "
                    "Step 2: confirm via sqlite3/PRAGMA integrity_check on .../kanban.db.",
                    "devops",
                    "blocked",
                    10,
                    int(time.time()),
                ),
            )
            con.commit()
            con.close()
            self.add_comment(
                board,
                task_id,
                "os-reviewer",
                "REVIEW_VERDICT=APPROVED\n"
                "Reviewer: os-reviewer (independent verification).\n"
                "KanbanDbCorruptError guidance is prose; no live DB operation, recovery, or migration performed.\n"
                "skill_view(name='kanban') availability confirmed.\n"
                "Target: t_581ca2e9",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            decision = vr.decide(candidates[0], dry_run=True)
            self.assertEqual(decision.verdict, "APPROVED")
            self.assertNotEqual(
                decision.scope_class,
                "operator_gated",
                "doc-only card with 'DB' in prose must not be operator_gated",
            )
            self.assertEqual(decision.action, "complete")
            self.assertEqual(decision.scope_class, "source_docs_spec_test_only")

    def test_genuine_db_migration_phrase_still_operator_gated(self) -> None:
        """Sanity: a real 'db migration' phrase must still refuse auto-complete."""
        task_id = "t_dboperator01"
        tmp, board = self.make_board()
        with tmp:
            con = sqlite3.connect(board.db)
            con.execute(
                "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    task_id,
                    "Run production DB migration and live runtime deploy",
                    "Scope includes schema migration, live data write, production deploy, and gateway restart.",
                    "devops",
                    "blocked",
                    10,
                    int(time.time()),
                ),
            )
            con.commit()
            con.close()
            self.add_comment(
                board,
                task_id,
                "os-reviewer",
                "REVIEW_VERDICT=APPROVED\nTarget: t_dboperator01\nMigration plan reviewed; deploy/live/DB scope remains operator gated.",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            decision = vr.decide(candidates[0], dry_run=True)
            self.assertEqual(decision.scope_class, "operator_gated")
            self.assertEqual(decision.action, "needs_operator")


class VerdictRouterBoardExclusionTests(unittest.TestCase):
    """Regression: excluded boards are never returned by boards()."""

    def test_orchestrator_sync_board_is_excluded_from_boards_scan(self) -> None:
        """orchestrator-sync board is not returned by boards()."""
        with tempfile.TemporaryDirectory(prefix="boards-test-") as tmpdir:
            boards_dir = Path(tmpdir)

            # Create orchestrator-sync/kanban.db (should be excluded)
            sync_db = boards_dir / "orchestrator-sync" / "kanban.db"
            sync_db.parent.mkdir(parents=True)
            con = sqlite3.connect(str(sync_db))
            con.executescript(
                """
                CREATE TABLE tasks (id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT,assignee TEXT,status TEXT NOT NULL,priority INTEGER DEFAULT 0,created_at INTEGER NOT NULL);
                CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,author TEXT NOT NULL,body TEXT NOT NULL,created_at INTEGER NOT NULL);
                """
            )
            con.execute(
                "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)",
                ("t_058ad294", "coordination bus", "orchestrator sync card", None, "blocked", 0, int(time.time())),
            )
            con.execute(
                "INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,?,?,?)",
                ("t_058ad294", "orchestrator", "REVIEW_VERDICT=REJECT\ncoordination traffic", int(time.time())),
            )
            con.commit()
            con.close()

            # Create a normal board that should still be included
            normal_db = boards_dir / "normal-board" / "kanban.db"
            normal_db.parent.mkdir(parents=True)
            con = sqlite3.connect(str(normal_db))
            con.executescript(
                """
                CREATE TABLE tasks (id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT,assignee TEXT,status TEXT NOT NULL,priority INTEGER DEFAULT 0,created_at INTEGER NOT NULL);
                CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,author TEXT NOT NULL,body TEXT NOT NULL,created_at INTEGER NOT NULL);
                """
            )
            con.commit()
            con.close()

            with mock.patch.object(vr, "BOARDS_DIR", new=boards_dir), mock.patch.object(vr, "DEFAULT_DB", new=Path(tmpdir) / "nonexistent"):
                result = vr.boards()
                slugs = [b.slug for b in result]
                self.assertNotIn("orchestrator-sync", slugs, "orchestrator-sync board should be excluded from scan")
                self.assertIn("normal-board", slugs, "normal board should still be included")
                self.assertEqual(len(slugs), 1, "only normal-board should appear in boards() output")


class ScanCorruptTimestampsTests(unittest.TestCase):
    """Regression: scan_corrupt_comment_timestamps.py detects '%s' in created_at."""

    def setUp(self) -> None:
        import scan_corrupt_comment_timestamps as sct  # type: ignore[import-unimport]
        self.sct = sct
        self.boards_dir = Path(tempfile.mkdtemp(prefix="sct-test-"))

    def make_clean_board(self) -> Path:
        db = self.boards_dir / "testboard" / "kanban.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db))
        con.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT,
                assignee TEXT,
                status TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL
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
        con.execute(
            "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)",
            ("t_clean", "clean task", "body", "devops", "blocked", 10, int(time.time())),
        )
        con.execute(
            "INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,?,?,?)",
            ("t_clean", "reviewer", "REVIEW_VERDICT=APPROVED", int(time.time())),
        )
        con.commit()
        con.close()
        return db

    def make_corrupt_board(self) -> Path:
        db = self.boards_dir / "corrupt-board" / "kanban.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db))
        con.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT,
                assignee TEXT,
                status TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL
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
        con.execute(
            "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)",
            ("t_corrupt", "corrupt task", "body", "elon-governor", "blocked", 10, int(time.time())),
        )
        # Literal '%s' in created_at — the exact corruption pattern
        con.execute(
            "INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,?,?,?)",
            ("t_corrupt", "elon-governor", "GOVERNOR RESPAN corrupt marker", "%s"),
        )
        con.commit()
        con.close()
        return db

    def test_clean_board_returns_zero(self) -> None:
        db = self.make_clean_board()
        results = self.sct.scan_board(db)
        self.assertEqual(len(results), 0)

    def test_corrupt_board_detects_literal_percent_s(self) -> None:
        db = self.make_corrupt_board()
        results = self.sct.scan_board(db)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["created_at"], "%s")
        self.assertEqual(results[0]["author"], "elon-governor")

    def test_fix_replaces_corrupt_timestamps(self) -> None:
        db = self.make_corrupt_board()
        results = self.sct.scan_board(db, fix=True)
        self.assertEqual(len(results), 1)
        # After fix, re-scan should find nothing
        results_after = self.sct.scan_board(db)
        self.assertEqual(len(results_after), 0)
        # Verify the replacement is an integer
        con = sqlite3.connect(str(db))
        row = con.execute("SELECT created_at FROM task_comments WHERE task_id='t_corrupt'").fetchone()
        con.close()
        self.assertIsNotNone(row)
        self.assertIsInstance(row[0], int)
        self.assertGreater(row[0], 1700000000)  # plausible Unix timestamp


if __name__ == "__main__":
    unittest.main()
