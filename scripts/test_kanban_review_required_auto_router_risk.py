from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import kanban_review_required_auto_router as router


class RiskRoutingIntegrationTests(unittest.TestCase):
    def make_board(self, body: str, *, comment: str = "review-required: inspect") -> tuple[tempfile.TemporaryDirectory[str], Path]:
        tmp = tempfile.TemporaryDirectory(prefix="review-risk-router-")
        board = Path(tmp.name) / "sycode-trading"
        board.mkdir()
        db = board / "kanban.db"
        con = sqlite3.connect(db)
        con.executescript(
            """
            CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
                status TEXT, priority INTEGER, created_by TEXT, created_at INTEGER,
                workspace_kind TEXT, idempotency_key TEXT, block_kind TEXT);
            CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                author TEXT, body TEXT, created_at INTEGER);
            CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
            """
        )
        now = int(time.time())
        con.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("t_12345678", "REVIEW-ME", body, "builder", "blocked", 10,
             "builder", now, "scratch", None, "needs_input"),
        )
        con.execute(
            "INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,?,?,?)",
            ("t_12345678", "builder", comment, now),
        )
        con.commit()
        con.close()
        return tmp, db

    def discover(self, body: str):
        tmp, db = self.make_board(body)
        self.addCleanup(tmp.cleanup)
        return router.discover_candidates(Path(tmp.name), ["sycode-trading"])

    def test_high_risk_path_routes_standalone_risk_review(self):
        candidates = self.discover('change_manifest: {"changed_paths":["server/orders/close.ts"],"change_flags":{}}')
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].reviewer, "trading-risk-reviewer")
        self.assertTrue(candidates[0].risk_classification.requires_standalone_risk_review)
        self.assertIn("live_execution", candidates[0].risk_classification.matched_reasons)

    def test_known_paper_only_does_not_mint_standalone_card(self):
        candidates = self.discover('change_manifest: {"changed_paths":["docs/review.md"],"change_flags":{"docs":true,"paper_only":true}}')
        self.assertEqual(candidates, [])

    def test_unknown_and_malformed_inputs_fail_closed(self):
        for body in (
            "No manifest here; live execution is not intended.",
            'change_manifest: {"changed_paths": ["server/app.ts"],',
        ):
            candidates = self.discover(body)
            self.assertEqual(len(candidates), 1)
            classification = candidates[0].risk_classification
            self.assertTrue(classification.fail_closed)
            self.assertIn("unknown_input", classification.matched_reasons)
            self.assertEqual(candidates[0].reviewer, "trading-risk-reviewer")

    def test_title_only_negation_cannot_override_risky_manifest(self):
        tmp, db = self.make_board('change_manifest: {"changed_paths":["server/positions/close.ts"],"change_flags":{"paper_only":true}}')
        self.addCleanup(tmp.cleanup)
        con = sqlite3.connect(db)
        con.execute("UPDATE tasks SET title=?", ("No live execution change",))
        con.commit()
        con.close()
        candidates = router.discover_candidates(Path(tmp.name), ["sycode-trading"])
        self.assertEqual(candidates[0].reviewer, "trading-risk-reviewer")
        self.assertTrue(candidates[0].risk_classification.fail_closed)

    def test_generated_and_case_variant_paths_cannot_bypass_matching(self):
        for path in ("GENERATED/../orders/close.ts", "./GENERATED/positions/close.ts"):
            candidates = self.discover(json.dumps({"change_manifest": {"changed_paths": [path], "change_flags": {}}}))
            self.assertEqual(len(candidates), 1)
            self.assertTrue(candidates[0].risk_classification.fail_closed)
            self.assertIn("unknown_input", candidates[0].risk_classification.matched_reasons)


if __name__ == "__main__":
    unittest.main()
