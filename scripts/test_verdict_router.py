#!/usr/bin/env python3
"""Regression fixtures for verdict_router.py safety gates."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))

from scripts import verdict_router as vr


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
                created_at INTEGER NOT NULL,
                block_kind TEXT,
                result TEXT
            );
            CREATE TABLE task_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                author TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                run_id INTEGER,
                kind TEXT NOT NULL,
                payload TEXT,
                created_at INTEGER
            );
            """
        )
        con.commit()
        con.close()
        return tmp, vr.Board("fixture", db)

    def insert_task(self, board: vr.Board, task_id: str, body: str = "review-required source patch") -> None:
        con = sqlite3.connect(board.db)
        con.execute(
            "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at,block_kind) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (task_id, "review-required source code patch", body, "devops", "blocked", 10, int(time.time()), None),
        )
        con.commit()
        con.close()

    def add_blocked_event(self, board: vr.Board, task_id: str, reason: str, block_kind: str = "needs_input") -> None:
        """Record a `blocked` task_event with the documented reason (C2, t_c3bbc27b).

        Mirrors what the ``kanban_block`` tool persists: the reason lives in the
        event payload JSON. The operator-gate detector must gate on this reason
        (surface 2) and the structured block_kind (surface 1) — never on reviewer
        comment prose.
        """
        import json as _json

        con = sqlite3.connect(board.db)
        con.execute(
            "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
            (task_id, "blocked", _json.dumps({"reason": reason, "kind": block_kind}), int(time.time())),
        )
        # Surface the structured block_kind on the task row too (first-class field).
        con.execute(
            "UPDATE tasks SET block_kind=? WHERE id=?",
            (block_kind, task_id),
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
                    created_at INTEGER NOT NULL,
                    block_kind TEXT
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
                    "Step 2: confirm via sqlite3/PRAGMA integrity_check on .../kanban.db. "
                    "review-required: source/docs only, no live DB operation.",
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
                    "Scope includes schema migration, live data write, production deploy, and gateway restart. review-required: operator-gated scope.",
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

    def test_c1_reviewer_gatedenial_prose_does_not_strand_approved_card(self) -> None:
        """C1 (t_8874b97b / t_9a0af491): an APPROVED card whose reviewer comment
        *denies* operator gates ('A3 gates intact; no credential, prod, or DB
        change; REVIEW_VERDICT=APPROVED') on a SOURCE/TEST-only card MUST complete
        through the router. The forbidden nouns live only in reviewer prose, not in
        the task's own title/body, so they must not gate the card.

        NOTE: the task title/body deliberately avoid frontend/app trigger words
        (middleware/component/page.tsx/...) so this fixture isolates the operator-
        gate detector (C2) and is not entangled with the unrelated frontend-app
        VERIFY_PASS heuristic.
        """
        task_id = "t_c1a0b1e2"
        tmp, board = self.make_board()
        with tmp:
            con = sqlite3.connect(board.db)
            con.execute(
                "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    task_id,
                    "Add unit tests for tenant id injection",
                    "Source/test-only change. Tenant coverage. review-required: source/test scope.",
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
                "A3 gates intact; no credential, prod, or DB change; "
                "Target: t_c1a0b1e2. Source/test-only, approvable.",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            decision = vr.decide(candidates[0], dry_run=True)
            self.assertEqual(decision.verdict, "APPROVED")
            self.assertNotEqual(
                decision.scope_class,
                "operator_gated",
                "reviewer gate-denial prose must not operator-gate the card",
            )
            self.assertEqual(decision.action, "complete")
            self.assertEqual(decision.scope_class, "source_docs_spec_test_only")

    def test_c2_database_token_in_proper_noun_title_not_gated(self) -> None:
        """C2 regression for t_9b29dfe8: the bare 'database' token must not match a
        proper noun in the title (`@sycode/database-tenant`) nor gate-denial prose.
        Only a genuine database-migration/write/schema phrase may gate.

        This fixture asserts the C2 guarantee: the operator-gate detector no longer
        fires on the proper-noun `database` in the title. The card's REAL title also
        contains the word 'middleware', which (separately, OUT OF SCOPE for this C2
        fix) trips the frontend-app VERIFY_PASS heuristic and routes to needs_pm.
        That residual is a distinct detector and is intentionally NOT weakened here;
        it is tracked separately so the card's remaining block is not mistaken for
        the operator-gate defect this task fixes.
        """
        task_id = "t_9b29dfe8"
        tmp, board = self.make_board()
        with tmp:
            con = sqlite3.connect(board.db)
            con.execute(
                "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    task_id,
                    "FIX: add tenantId-injection middleware to @sycode/database-tenant (unblocks founding-member test)",
                    "Source change for tenant injection. Adds test coverage. review-required: source scope.",
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
                "No credential, prod, or DB change needed; tenant change is source-only. "
                "Target: t_9b29dfe8.",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            decision = vr.decide(candidates[0], dry_run=True)
            self.assertEqual(decision.verdict, "APPROVED")
            self.assertNotEqual(
                decision.scope_class,
                "operator_gated",
                "proper-noun 'database' in title + gate-denial prose must not gate",
            )
            self.assertNotEqual(
                decision.action,
                "needs_operator",
                "the C2 operator-gate detector must no longer strand this card",
            )

    def test_c3_genuine_operator_gate_still_needs_operator(self) -> None:
        """C3 (t_8874b97b / t_9a0af491): fail-closed preserved. A card whose
        title/body genuinely indicates deploy/DB/credential/A3/operator scope MUST
        still emit needs_operator and NOT auto-complete — even if the reviewer
        comment also says 'deploy approved'.
        """
        task_id = "t_c3operator1"
        tmp, board = self.make_board()
        with tmp:
            con = sqlite3.connect(board.db)
            con.execute(
                "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    task_id,
                    "Run production DB migration and live runtime deploy",
                    "Scope includes schema migration, live data write, production deploy, and gateway restart. review-required: operator-gated scope.",
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
                "Target: t_c3operator1\n"
                "Migration plan reviewed; deploy/live/DB scope remains operator gated, do not auto-complete.",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            decision = vr.decide(candidates[0], dry_run=True)
            self.assertEqual(decision.scope_class, "operator_gated")
            self.assertEqual(decision.action, "needs_operator")
            self.assertNotEqual(decision.action, "complete")


class VerdictRouterC4NegatedVerdictTests(VerdictRouterRegressionTests):
    """C4 (t_c996e275): negation-aware verdict detection.

    A negated / no-verdict REVIEW_VERDICT token must NOT enter the routing
    pipeline (fail closed) and must NOT be parsed as an affirmative verdict.
    Incident: sycode-trading/t_19901020 (comment_id=14406, shadow-log
    2026-07-19T20:35:25Z) where "No REVIEW_VERDICT=APPROVED/CHANGES_REQUESTED"
    and "NO REVIEW_VERDICT issued" were wrongly parsed as APPROVED.
    """

    def _blocked_task_with_comment(self, task_id: str, comment_body: str) -> tuple[tempfile.TemporaryDirectory[str], vr.Board]:
        tmp, board = self.make_board()
        self.insert_task(board, task_id)
        self.add_comment(board, task_id, "os-reviewer", comment_body)
        return tmp, board

    def test_c4_no_review_verdict_approved_slash_changes_requested_is_not_a_candidate(self) -> None:
        """The exact incident phrase: 'No REVIEW_VERDICT=APPROVED/CHANGES_REQUESTED'
        must not become a routing candidate (fail closed — never enters pipeline).
        """
        task_id = "t_19901020"
        tmp, board = self._blocked_task_with_comment(
            task_id,
            "NO REVIEW_VERDICT issued. No REVIEW_VERDICT=APPROVED/CHANGES_REQUESTED. "
            "This review is not yet complete; leaving the card blocked.",
        )
        with tmp:
            self.assertEqual(vr.candidates_for_board(board), [])
            # And even if forced through, parse_verdict must not yield APPROVED.
            self.assertIsNone(vr.parse_verdict("No REVIEW_VERDICT=APPROVED/CHANGES_REQUESTED"))

    def test_c4_no_review_verdict_issued_is_not_a_candidate(self) -> None:
        """'NO REVIEW_VERDICT issued' (denial subject) must not route."""
        task_id = "t_c4noissued"
        tmp, board = self._blocked_task_with_comment(
            task_id,
            "NO REVIEW_VERDICT issued. Holding for further review before any verdict.",
        )
        with tmp:
            self.assertEqual(vr.candidates_for_board(board), [])
            self.assertIsNone(vr.parse_verdict("NO REVIEW_VERDICT issued"))

    def test_c4_do_not_post_review_verdict_approved_is_not_a_candidate(self) -> None:
        """'do not post REVIEW_VERDICT=APPROVED' (instruction to withhold) must not route."""
        task_id = "t_c4donotpost"
        tmp, board = self._blocked_task_with_comment(
            task_id,
            "do not post REVIEW_VERDICT=APPROVED here; the review is still in progress.",
        )
        with tmp:
            self.assertEqual(vr.candidates_for_board(board), [])
            self.assertIsNone(vr.parse_verdict("do not post REVIEW_VERDICT=APPROVED"))

    def test_c3b_structured_block_kind_capability_gates_even_without_prose_terms(self) -> None:
        """C2 (t_c3bbc27b) surface 1 — structured block_kind gate.

        A task whose recorded block_kind is 'capability' (system-recorded hard
        wall: missing credentials / access / action no agent can perform) MUST
        operator-gate even when its title/body carry NO FORBIDDEN_SCOPE_RE term
        and its reviewer comment prose is an approval. Blocking on a structured
        field is gating on recorded operator scope, never on reviewer prose.
        """
        task_id = "t_c3b0c0de"
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, task_id, body="review-required source patch")
            self.add_blocked_event(
                board,
                task_id,
                reason="needs_input: awaiting dependent card",
                block_kind="capability",
            )
            self.add_comment(
                board,
                task_id,
                "os-reviewer",
                "REVIEW_VERDICT=APPROVED\nTarget: t_c3b0c0de. Source-only, approvable.",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            decision = vr.decide(candidates[0], dry_run=True)
            self.assertEqual(decision.scope_class, "operator_gated")
            self.assertEqual(decision.action, "needs_operator")

    def test_c3c_block_reason_with_gate_term_gates_even_when_title_body_clear(self) -> None:
        """C2 (t_c3bbc27b) surface 2 — documented block-reason gate.

        A task whose title/body contain NO FORBIDDEN_SCOPE_RE term but whose
        documented block reason ('operator decision required: prod deploy') DOES
        carry a gate term MUST operator-gate. This is the capability-preserving
        half of C2: gating on the recorded block reason rather than reviewer
        comment prose.
        """
        task_id = "t_c3c0c0de"
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, task_id, body="review-required source patch")
            self.add_blocked_event(
                board,
                task_id,
                reason="NEEDS-FRANK/operator decision: live prod deploy requires Frank approval",
                block_kind="needs_input",
            )
            self.add_comment(
                board,
                task_id,
                "os-reviewer",
                "REVIEW_VERDICT=APPROVED\nTarget: t_c3c0c0de. Source-only, approvable.",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            decision = vr.decide(candidates[0], dry_run=True)
            self.assertEqual(decision.scope_class, "operator_gated")
            self.assertEqual(decision.action, "needs_operator")

    def test_c3d_block_reason_denial_does_not_gate_clear_card(self) -> None:
        """C2 (t_c3bbc27b) surface 2 — gate-DENIAL in block reason is NOT a gate.

        A block reason that *denies* a gate ('no prod, no credential or DB change;
        safe') with no positive FORBIDDEN_SCOPE_RE assertion must NOT operator-gate
        a title/body-clear card — the same negation-aware redaction that protects
        title/body now protects the block-reason surface. Confirms C2 did not
        weaken fail-closed: genuine positive gate reasons still gate (see c3c).
        """
        task_id = "t_c3d0c0de"
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, task_id, body="review-required source patch")
            self.add_blocked_event(
                board,
                task_id,
                reason="review-required: no prod, no credential or DB change; A3-safe patch",
                block_kind="needs_input",
            )
            self.add_comment(
                board,
                task_id,
                "os-reviewer",
                "REVIEW_VERDICT=APPROVED\nTarget: t_c3d0c0de. Source-only, approvable.",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            decision = vr.decide(candidates[0], dry_run=True)
            self.assertNotEqual(
                decision.scope_class,
                "operator_gated",
                "gate-DENIAL block reason must not operator-gate a clear card",
            )
            self.assertEqual(decision.action, "complete")
            self.assertEqual(decision.scope_class, "source_docs_spec_test_only")

    def test_c4_affirmative_verdict_in_separate_sentence_still_routes(self) -> None:
        """Positive control: a genuine affirmative REVIEW_VERDICT in its own
        sentence must still route, even if the comment contains a denial word
        ('no' / 'not') in a *different* sentence.
        """
        task_id = "t_c4a1b2c3"
        tmp, board = self.make_board()
        with tmp:
            con = sqlite3.connect(board.db)
            con.execute(
                "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    task_id,
                    "Add unit tests for tenant id injection [REVIEW-REQUIRED]",
                    "Source/test-only change. Tenant coverage.",
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
                "This is not a prod change and no DB migration is required.\n"
                "REVIEW_VERDICT=APPROVED\n"
                "Target: t_c4a1b2c3. Source/test-only, approvable.",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            decision = vr.decide(candidates[0], dry_run=True)
            self.assertEqual(decision.verdict, "APPROVED")
            self.assertEqual(decision.action, "complete")
            self.assertEqual(decision.scope_class, "source_docs_spec_test_only")

    def test_c4_negated_and_affirmative_same_comment_routes_affirmative_only(self) -> None:
        """If a comment denies one verdict and affirms another in separate
        sentences, only the affirmative declaration counts (fail closed to one).
        """
        task_id = "t_c4d00d01"
        tmp, board = self._blocked_task_with_comment(
            task_id,
            "No REVIEW_VERDICT=CHANGES_REQUESTED for this card.\n"
            "REVIEW_VERDICT=APPROVED\n"
            "Target: t_c4d00d01. Source patch approved.",
        )
        with tmp:
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            decision = vr.decide(candidates[0], dry_run=True)
            self.assertEqual(decision.verdict, "APPROVED")
            self.assertEqual(decision.action, "complete")

    def test_c4_incident_text_t19901020_does_not_emit_needs_pm(self) -> None:
        """End-to-end guard using the real t_19901020 reviewer phrasing: a card
        whose latest reviewer comment only denies a verdict must produce NO
        candidate and therefore emit no NEEDS-PM marker.
        """
        task_id = "t_19901020"
        tmp, board = self._blocked_task_with_comment(
            task_id,
            "REVIEW_VERDICT gate: NO REVIEW_VERDICT issued. "
            "No REVIEW_VERDICT=APPROVED/CHANGES_REQUESTED. "
            "Manual PM routing required; do not auto-complete.",
        )
        with tmp:
            self.assertEqual(vr.candidates_for_board(board), [])
            self.assertIsNone(vr.parse_verdict(
                "REVIEW_VERDICT gate: NO REVIEW_VERDICT issued. "
                "No REVIEW_VERDICT=APPROVED/CHANGES_REQUESTED."
            ))


class VerdictRouterBoardExclusionTests(unittest.TestCase):
    """Regression: excluded boards are never returned by boards()."""

    def test_orchestrator_sync_board_is_excluded_from_boards_scan(self) -> None:
        """B3 (t_65a0c080): boards() returns only allowlisted task boards and
        excludes coordination boards, backup snapshots, and non-allowlisted dirs.
        """
        with tempfile.TemporaryDirectory(prefix="boards-test-") as tmpdir:
            boards_dir = Path(tmpdir)

            # Create orchestrator-sync/kanban.db (should be excluded)
            sync_db = boards_dir / "orchestrator-sync" / "kanban.db"
            sync_db.parent.mkdir(parents=True)
            con = sqlite3.connect(str(sync_db))
            con.executescript(
                """
                CREATE TABLE tasks (id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT,assignee TEXT,status TEXT NOT NULL,priority INTEGER DEFAULT 0,created_at INTEGER NOT NULL,block_kind TEXT);
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

            # Create a junk/non-allowlisted board (should be excluded by B3 allowlist)
            junk_db = boards_dir / "testproj" / "kanban.db"
            junk_db.parent.mkdir(parents=True)
            con = sqlite3.connect(str(junk_db))
            con.executescript(
                """
                CREATE TABLE tasks (id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT,assignee TEXT,status TEXT NOT NULL,priority INTEGER DEFAULT 0,created_at INTEGER NOT NULL,block_kind TEXT);
                CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,author TEXT NOT NULL,body TEXT NOT NULL,created_at INTEGER NOT NULL);
                """
            )
            con.commit()
            con.close()

            # Create a backup snapshot board (should be excluded)
            bak_db = boards_dir / ".bak_t_c3bd9fec_20260719T102432Z" / "kanban.db"
            bak_db.parent.mkdir(parents=True)
            con = sqlite3.connect(str(bak_db))
            con.executescript(
                """
                CREATE TABLE tasks (id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT,assignee TEXT,status TEXT NOT NULL,priority INTEGER DEFAULT 0,created_at INTEGER NOT NULL,block_kind TEXT);
                CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,author TEXT NOT NULL,body TEXT NOT NULL,created_at INTEGER NOT NULL);
                """
            )
            con.commit()
            con.close()

            # Create an allowlisted board that should still be included (jarvis-os)
            allowed_db = boards_dir / "jarvis-os" / "kanban.db"
            allowed_db.parent.mkdir(parents=True)
            con = sqlite3.connect(str(allowed_db))
            con.executescript(
                """
                CREATE TABLE tasks (id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT,assignee TEXT,status TEXT NOT NULL,priority INTEGER DEFAULT 0,created_at INTEGER NOT NULL,block_kind TEXT);
                CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,author TEXT NOT NULL,body TEXT NOT NULL,created_at INTEGER NOT NULL);
                """
            )
            con.commit()
            con.close()

            with mock.patch.object(vr, "BOARDS_DIR", new=boards_dir), mock.patch.object(vr, "DEFAULT_DB", new=Path(tmpdir) / "nonexistent"):
                result = vr.boards()
                slugs = [b.slug for b in result]
                self.assertNotIn("orchestrator-sync", slugs, "orchestrator-sync board should be excluded from scan")
                self.assertNotIn("testproj", slugs, "non-allowlisted board should be excluded by B3 allowlist")
                self.assertNotIn(".bak_t_c3bd9fec_20260719T102432Z", slugs, "backup snapshot boards should be excluded")
                self.assertIn("jarvis-os", slugs, "allowlisted board should still be included")
                self.assertEqual(len(slugs), 1, "only the allowlisted board should appear in boards() output")


class RiskTierClassifierTests(unittest.TestCase):
    """Kill 6/W7 risk boundary: explicit manifests only, fail closed."""

    def assert_risky(self, paths: object, flags: object, *reasons: str) -> None:
        result = vr.classify_risk(paths, flags)
        self.assertTrue(result.requires_standalone_risk_review)
        self.assertTrue(result.fail_closed is False or "unknown_input" in result.matched_reasons)
        for reason in reasons:
            self.assertIn(reason, result.matched_reasons)

    def test_every_high_risk_class_has_stable_reason_code(self) -> None:
        cases = ((["server/src/billing/fees.ts"], {"paper_only": False}, "money"), (["server/src/orders/submit.ts"], {"paper_only": False}, "live_execution"), (["server/.env.example"], {"paper_only": False}, "access_material"), (["supabase/migrations/0123.sql"], {"paper_only": False}, "ddl_or_irreversible_data"), (["server/src/outcome-labeler.ts"], {"paper_only": False}, "measurement_write_path"))
        for paths, flags, reason in cases:
            with self.subTest(reason=reason):
                self.assert_risky(paths, flags, reason)

    def test_known_paper_only_docs_research_tests_and_refactors_do_not_need_standalone(self) -> None:
        for path, flag in (("docs/review-routing.md", "docs"), ("research/hypothesis.md", "research"), ("tests/router.test.ts", "tests"), ("src/formatter.ts", "refactor")):
            with self.subTest(path=path):
                result = vr.classify_risk([path], {"paper_only": True, flag: True})
                self.assertFalse(result.requires_standalone_risk_review)
                self.assertFalse(result.fail_closed)
                self.assertEqual(result.matched_reasons, ())

    def test_missing_malformed_and_unknown_inputs_fail_closed(self) -> None:
        for paths, flags in (([], {"paper_only": True}), (["src/newthing.bin"], {"paper_only": True}), (["src/a.ts"], None), ([""], {"docs": "yes"}), (["../orders.ts"], {"paper_only": False})):
            with self.subTest(paths=paths, flags=flags):
                result = vr.classify_risk(paths, flags)
                self.assertTrue(result.requires_standalone_risk_review)
                self.assertTrue(result.fail_closed)
                self.assertIn("unknown_input", result.matched_reasons)

    def test_conflicting_paper_only_and_risky_flag_fails_closed(self) -> None:
        result = vr.classify_risk(["docs/summary.md"], {"paper_only": True, "live_execution": True})
        self.assertTrue(result.requires_standalone_risk_review)
        self.assertTrue(result.fail_closed)
        self.assertEqual(result.matched_reasons, ("live_execution", "unknown_input"))

    def test_title_only_negation_is_not_an_input_or_override(self) -> None:
        result = vr.classify_risk(["server/src/orders/submit.ts"], {"paper_only": True})
        self.assertTrue(result.requires_standalone_risk_review)
        self.assertIn("live_execution", result.matched_reasons)
        self.assertIn("unknown_input", result.matched_reasons)

    def test_generated_and_case_variant_paths_match_risk_rules(self) -> None:
        for path, reason in (("GENERATED/Orders/submit.json", "live_execution"), ("Generated/OUTCOME-LABELS.json", "measurement_write_path"), ("generated/SUPABASE/MIGRATIONS/0001.sql", "ddl_or_irreversible_data")):
            with self.subTest(path=path):
                self.assert_risky([path], {"paper_only": False}, reason)

    def test_reason_codes_are_returned_in_canonical_order(self) -> None:
        result = vr.classify_risk(["orders/outcome-labeler.ts"], {"money": True, "live_execution": True, "measurement_write_path": True, "paper_only": False})
        self.assertEqual(result.matched_reasons[:3], ("money", "live_execution", "measurement_write_path"))


class VerdictAttributionTests(unittest.TestCase):
    """Regression tests for verdict_is_attributed fixes (issue #4, #10)."""

    def test_relay_seat_detection_uses_regex_not_substring(self) -> None:
        """Fix issue #4: verdict_is_attributed should use regex fullmatch, not substring check."""
        # "worker" is a known relay seat. An author named "worker-2" should NOT match
        # the relay seat pattern via substring, because "worker" requires word boundaries.
        text = "REVIEW_VERDICT=APPROVED per worker"
        # The verdict at position 0:24 is attributed to "worker"
        self.assertTrue(vr.verdict_is_attributed(text, "worker-2", 0, 24),
                       "verdict attributed to 'worker' should be detected as attribution when author is 'worker-2'")
        # An author actually named "worker" should match the relay seat
        self.assertFalse(vr.verdict_is_attributed(text, "worker", 0, 24),
                        "verdict should not be attributed when author is the named seat 'worker'")

    def test_relay_author_check_even_when_commenter_is_allowlisted_seat(self) -> None:
        """Fix issue #10: still check relay authors when commenter is an allowlisted seat."""
        # "guardian" is a known relay seat. Even when the commenter IS guardian,
        # if they relay another seat's verdict, it should be detected as attribution.
        text = "per trading-risk-reviewer REVIEW_VERDICT=APPROVED"
        # guardian relaying trading-risk-reviewer's verdict
        self.assertTrue(vr.verdict_is_attributed(text, "guardian", 26, 50),
                       "guardian relaying another seat's verdict should be detected as attribution")
        # guardian issuing their own verdict (no relay phrasing)
        text2 = "REVIEW_VERDICT=APPROVED"
        self.assertFalse(vr.verdict_is_attributed(text2, "guardian", 0, 23),
                        "guardian's own verdict should not be attributed")


class MalformedPathTests(unittest.TestCase):
    """Regression tests for malformed path handling (issue #8)."""

    def test_malformed_path_after_lstrip_adds_unknown_input(self) -> None:
        """Fix issue #8: paths that become empty, absolute, or contain .. after lstrip should add unknown_input."""
        # Empty path after stripping
        result = vr.classify_risk(["./"], {"paper_only": True})
        self.assertIn("unknown_input", result.matched_reasons,
                     "path that becomes empty after lstrip should add unknown_input")
        self.assertTrue(result.fail_closed)
        
        # Absolute path after stripping  
        result = vr.classify_risk(["/src/orders.ts"], {"paper_only": True})
        self.assertIn("unknown_input", result.matched_reasons,
                     "absolute path should add unknown_input")
        self.assertTrue(result.fail_closed)
        
        # Traversal path
        result = vr.classify_risk(["src/../../../etc/passwd"], {"paper_only": True})
        self.assertIn("unknown_input", result.matched_reasons,
                     "path with .. traversal should add unknown_input")
        self.assertTrue(result.fail_closed)


class GateDenialCueTests(unittest.TestCase):
    """Regression tests for gate denial cue handling (issue #9)."""

    def test_frank_gated_is_not_denial_cue(self) -> None:
        """Fix issue #9: 'frank-gated' etc. are gate-affirming phrases, not denial cues."""
        # "frank-gated" means the action IS gated, so a prod/credential/DB term should still gate
        text = "prod deploy - frank-gated"
        result = vr.redact_gate_denials(text)
        # The term "prod" should NOT be redacted because "frank-gated" is not a denial cue
        self.assertIn("prod", result,
                     "'frank-gated' should not cause 'prod' to be redacted as a denial")
        
    def test_operator_gated_is_not_denial_cue(self) -> None:
        """'operator-gated' means the action IS gated, so gate terms should not be redacted."""
        text = "database migration - operator-gated"
        result = vr.redact_gate_denials(text)
        self.assertIn("database", result,
                     "'operator-gated' should not cause 'database' to be redacted as a denial")
        
    def test_needs_frank_is_not_denial_cue(self) -> None:
        """'needs frank' means Frank approval is required, so gate terms should not be redacted."""
        text = "credential change - needs frank"
        result = vr.redact_gate_denials(text)
        self.assertIn("credential", result,
                     "'needs frank' should not cause 'credential' to be redacted as a denial")
        
    def test_genuine_denial_still_redacts(self) -> None:
        """Genuine denial cues like 'no', 'not', 'safe' should still redact gate terms."""
        text = "no prod change"
        result = vr.redact_gate_denials(text)
        # "prod" should be redacted when preceded by genuine denial cue "no"
        self.assertNotIn("prod", result.strip(),
                        "'no prod' should redact 'prod' as a denial")


if __name__ == "__main__":
    unittest.main()
