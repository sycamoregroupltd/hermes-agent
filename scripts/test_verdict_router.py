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
                    "Add unit tests for tenant id injection",
                    "review-required: Source/test-only change. Tenant coverage.",
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


class VerdictRouterB1B4RegressionTests(VerdictRouterRegressionTests):
    """B1/B2/B4 (t_65a0c080) regression gates.

    - B1: a verdict *quoted/relayed* from another reviewer seat must not be
      attributed to the comment author. In particular the relay-hint author
      set must be matched as whole tokens, never as substrings, so ``jarvis``
      does not match ``jarvis-os-pm`` / ``jarvis-voice`` and skip the relay
      check (quoted-verdict blindness).
    - B2: a reviewer cannot self-certify its own card — author == assignee
      must fail closed to None (already implemented; regression-proven here).
    - B4: the durable COMPLETE marker must be persisted as a task_comments row
      in the apply path so a reopened+re-blocked card re-runs and sees the
      marker (prior_router_marker) instead of re-completing off a stale
      approval.
    """

    def test_b1_quoted_verdict_by_jarvis_quoting_other_reviewer_fails_closed(self) -> None:
        """B1: `jarvis` (a reviewer seat) RELAYING another seat's verdict by
        writing 'trading-risk-reviewer posts REVIEW_VERDICT=APPROVED' must NOT
        be treated as issuing the verdict. The relay-hint author-set membership
        check must be whole-token, so 'jarvis' is recognized as a reviewer seat
        (it is in the set) AND the window naming a *different* seat is still an
        attribution. The substring bug made 'jarvis' match 'jarvis-os-pm' and
        skip the relay check entirely.
        """
        task_id = "t_b1a1b2c3"
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, task_id)
            self.add_comment(
                board,
                task_id,
                "jarvis",
                "trading-risk-reviewer posts REVIEW_VERDICT=APPROVED\n"
                "Target: t_b1a1b2c3. Relaying the reviewer's verdict on their behalf.",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            decision = vr.decide(candidate, dry_run=True)
            # The verdict is quoted/attributed to trading-risk-reviewer, NOT
            # issued by jarvis, so it must fail closed (None -> needs_pm),
            # never auto-complete.
            self.assertEqual(decision.verdict, None)
            self.assertNotEqual(decision.action, "complete")
            self.assertEqual(decision.action, "needs_pm")

    def test_b1_jarvis_own_verdict_still_routes(self) -> None:
        """B1 positive control: `jarvis` ISSUING its own verdict (not relaying)
        must still route. The whole-token fix must not suppress genuine
        reviewer verdicts.
        """
        task_id = "t_b1c0de01"
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, task_id)
            self.add_comment(
                board,
                task_id,
                "jarvis",
                "REVIEW_VERDICT=APPROVED\nTarget: t_b1c0de01. Jarvis's own verdict, source-only scope.",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            decision = vr.decide(candidates[0], dry_run=True)
            self.assertEqual(decision.verdict, "APPROVED")
            self.assertEqual(decision.action, "complete")

    def test_b2_builder_self_approve_fails_closed_to_none(self) -> None:
        """B2: a card's OWN assignee (builder) cannot approve its own card.
        author == assignee must fail closed -> parse_verdict returns None ->
        never auto-complete.
        """
        task_id = "t_b2self01"
        tmp, board = self.make_board()
        with tmp:
            # assignee == author == builder (self-certification)
            con = sqlite3.connect(board.db)
            con.execute(
                "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at,block_kind) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (task_id, "review-required source code patch", "review-required source patch",
                 "builder", "blocked", 10, int(time.time()), None),
            )
            con.commit()
            con.close()
            self.add_comment(
                board,
                task_id,
                "builder",
                "REVIEW_VERDICT=APPROVED\nTarget: t_b2self01. Self-approval attempt.",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            # Direct parse: author==assignee==builder -> fail closed.
            self.assertIsNone(vr.parse_verdict(
                candidate.latest_comment_body,
                author=candidate.latest_comment_author,
                assignee=candidate.assignee,
            ))
            decision = vr.decide(candidate, dry_run=True)
            self.assertEqual(decision.verdict, None)
            self.assertNotEqual(decision.action, "complete")

    def test_b4_apply_complete_writes_durable_marker_comment_row(self) -> None:
        """B4: in the apply path the COMPLETE marker must be persisted as a
        task_comments row (via --comment) so a reopened+re-blocked card re-runs
        and prior_router_marker() sees it. This is the regression that the
        apply-era completes (marker only in summary/metadata) missed.
        """
        task_id = "t_b4a1b2c3"
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, task_id)
            self.add_comment(
                board,
                task_id,
                "os-reviewer",
                "REVIEW_VERDICT=APPROVED\nTarget: t_b4a1b2c3. Source/test-only, approvable.",
            )
            candidates = vr.candidates_for_board(board)
            self.assertEqual(len(candidates), 1)
            decision = vr.decide(candidates[0], dry_run=False)
            self.assertEqual(decision.action, "complete")

            # The apply path must emit a --comment carrying the durable marker
            # (so perform() writes it as a task_comments row). Capture the CLI
            # args and prove the marker is present as a comment payload.
            captured: dict[str, list[str]] = {}

            def fake_run_cli(args: list[str]):
                captured["args"] = args
                # Faithfully simulate the apply path: `hermes kanban complete
                # --comment <marker>` writes the marker as a task_comments row.
                if "complete" in args and "--comment" in args:
                    ci = args.index("--comment")
                    marker_body = args[ci + 1]
                    self.add_comment(board, task_id, vr.AUTHOR, marker_body)
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with mock.patch.object(vr, "run_cli", side_effect=fake_run_cli):
                vr.perform(decision, candidates[0])

            self.assertIn("--comment", captured["args"])
            comment_idx = captured["args"].index("--comment")
            marker_comment = captured["args"][comment_idx + 1]
            self.assertIn(
                f"verdict-router COMPLETE marker={decision.idempotency_key}",
                marker_comment,
                "apply-path complete must persist the COMPLETE marker as a comment row",
            )

            # Durability: simulate the completed card being reopened/re-blocked
            # (the marker now exists as a task_comments row). A fresh scan must
            # not re-route the stale approval: prior_router_marker returns True.
            con = vr.open_db_ro(board.db)
            try:
                self.assertTrue(
                    vr.prior_router_marker(con, task_id, decision.idempotency_key),
                    "durable COMPLETE marker must be visible to prior_router_marker",
                )
            finally:
                con.close()


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
                created_at INTEGER NOT NULL,
                claim_lock TEXT,
                claim_expires INTEGER,
                worker_pid INTEGER,
                max_runtime_seconds INTEGER,
                last_heartbeat_at INTEGER,
                started_at INTEGER,
                current_run_id INTEGER
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
                created_at INTEGER NOT NULL,
                claim_lock TEXT,
                claim_expires INTEGER,
                worker_pid INTEGER,
                max_runtime_seconds INTEGER,
                last_heartbeat_at INTEGER,
                started_at INTEGER,
                current_run_id INTEGER
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
