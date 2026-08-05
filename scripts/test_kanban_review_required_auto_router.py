#!/usr/bin/env python3
"""Regression tests for kanban_review_required_auto_router.py (kanban t_06f27b2d).

Verifies the structural fix: a review-required handoff card whose TRUE blocker
is a gate/router defect, a maker-still-running dependency, a staged-fix
awaiting-apply, or a Frank A3 hold must be routed to the owner that can
actually clear it -- NOT the board reviewer (os-reviewer on jarvis-os), where
no reviewer action can resolve it.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import kanban_review_required_auto_router as R


class ClassifyRouteKindTests(unittest.TestCase):
    """Pure classifier tests against the scenario family from t_06f27b2d."""

    def _route(self, block_kind, reason):
        return R.classify_route_kind(block_kind, reason, reason)

    def test_gate_misapplication_is_self_improve_regardless_of_block_kind(self):
        # t_177ef664: VERIFY_PASS gate wrongly applied to a read-only bash review.
        # Genuine gate/router defects route to self-improve for ANY block_kind.
        for bk in (None, "needs_input", "capability"):
            self.assertEqual(
                self._route(
                    bk,
                    "Kernel completion gate misapplied the frontend/web VERIFY_PASS "
                    "requirement to this task. It touches no page/route/middleware/UI.",
                ),
                "self_improve",
                msg=f"block_kind={bk!r}",
            )

    def test_router_automation_defect_is_elon(self):
        # t_ee754df8: verdict delivered but child creation blocked by critic
        # read-only gate on kanban_create -> router/automation defect -> owner-operator.
        self.assertEqual(
            self._route(
                "capability",
                "REVIEW_VERDICT=APPROVE delivered but implementation child creation "
                "was blocked by the reviewer critic read-only gate on kanban_create.",
            ),
            "elon",
        )

    def test_router_automation_defect_beats_title_verify_pass_signal(self):
        # Regression for kanban t_be3fc92b: the real t_ee754df8 card has a
        # TITLE containing the generic gate token "VERIFY_PASS" but a BLOCK REASON
        # naming an explicit router/automation defect (child creation blocked by
        # the critic read-only gate on kanban_create). The classifier MUST route
        # to elon, NOT self_improve-engineer, even though the title carries
        # VERIFY_PASS. The old code checked the gate branch first and misrouted.
        title = ("REVIEWED PROPOSAL: let rejected frontend review cards close "
                 "without VERIFY_PASS while approvals stay gated")
        reason = ("REVIEW_VERDICT=APPROVE delivered and persisted, but "
                  "implementation child creation was blocked by the reviewer "
                  "critic read-only gate on kanban_create.")
        self.assertEqual(
            R.classify_route_kind("capability", reason, title + "\n" + reason),
            "elon",
        )

    def test_verdict_router_token_no_longer_hijacks_gate_classification(self):
        # The bare "verdict-router" substring was removed from GATE_DEFECT_RE so
        # it cannot overlap ROUTER_DEFECT_RE. A plain gate misapplication reason
        # with no router-defect signal still routes to self_improve.
        self.assertEqual(
            self._route(
                "capability",
                "Kernel completion gate misapplied the frontend/web VERIFY_PASS "
                "requirement to this task. It touches no page/route/middleware/UI.",
            ),
            "self_improve",
        )

    def test_capability_genuine_gap_is_elon(self):
        self.assertEqual(
            self._route("capability", "No deployable skill known for this new subsystem."),
            "elon",
        )

    def test_needs_input_maker_not_delivered_is_devops(self):
        # t_91cbca3a: maker has not provided the required artifact.
        self.assertEqual(
            self._route(None, "maker has not provided the required exact branch/SHA artifact."),
            "devops_owner",
        )

    def test_needs_input_staged_fix_awaiting_apply_is_devops(self):
        # t_dd485585: reviewer staged a ready-to-apply fix but did NOT apply/run it.
        self.assertEqual(
            self._route(
                "needs_input",
                "os-reviewer confirmed root cause and staged a ready-to-apply 1-run "
                "grace fix but per the reviewer-independence contract did NOT apply/run it.",
            ),
            "devops_owner",
        )

    def test_needs_input_genuine_review_stays_reviewer(self):
        # t_2f52534d: genuine source-change review pending independent reviewer verdict.
        self.assertEqual(
            self._route(
                "needs_input",
                "pushed exact kanban_create critic-gate source candidate; tests green; "
                "needs independent os-reviewer review before any install/rollout.",
            ),
            "reviewer",
        )

    def test_frank_gate_is_pm_frank(self):
        self.assertEqual(self._route("frank_gate", "Frank A3 hold pending"), "pm_frank")

    def test_needs_input_frank_approval_hold_is_pm_frank(self):
        self.assertEqual(
            self._route("needs_input", "Blocked pending Frank approval of the migration packet."),
            "pm_frank",
        )

    def test_dependency_is_devops(self):
        self.assertEqual(
            self._route("dependency", "blocked on a still-running maker task"),
            "devops_owner",
        )

    def test_genuine_review_mentioning_kanban_create_stays_reviewer(self):
        # t_2f52534d: genuine source-change review mentioning kanban_create must
        # stay with the board reviewer (NOT be stolen as a router defect).
        self.assertEqual(
            self._route(
                "needs_input",
                "pushed exact kanban_create critic-gate source candidate; tests green; "
                "needs independent os-reviewer review before any install/rollout.",
            ),
            "reviewer",
        )

    def test_plain_review_required_handoff_is_reviewer(self):
        # Default path preserved: a plain review-required handoff still routes to
        # the board reviewer (os-reviewer on jarvis-os).
        self.assertEqual(
            self._route(None, "review-required: tests pass; needs reviewer verdict on the source patch."),
            "reviewer",
        )

    def test_successful_verify_pass_evidence_does_not_create_gate_defect(self):
        # Regression for t_56873697 / source t_87fffe68: a completion-gate
        # evidence handoff that says both live gates PASS is not itself a gate
        # misapplication. The remaining blocker is reviewer/source-provenance
        # reconciliation, so it must stay on the genuine reviewer route instead
        # of being stolen by the self-improve gate-defect rule.
        title = "Run live completion gates and prepare review block"
        reason = (
            "review-required: both live gates PASS for / and /kanban; evidence "
            "note/comment posted, but reviewer must reconcile dirty/no-remote "
            "runtime checkout and HEAD 976deba vs parent-referenced add5c16 "
            "before final closeout."
        )
        self.assertEqual(
            R.classify_route_kind("needs_input", reason, title + "\n" + reason),
            "reviewer",
        )

    def test_merge_gate_review_not_confused_with_gate_defect(self):
        # Regression for t_3ba2235c / source t_d88c2d1c: a genuine code-review
        # handoff whose block reason mentions "merge-gate t_0ca5c516" and whose
        # comment excerpt mentions "Defect 2 fix applied".  The bare token "gate"
        # in "merge-gate" combined with "defect" in the comment body (referring
        # to the *original* CryptoQuant bug, not a gate-rule defect) must NOT
        # trigger GATE_DEFECT_RE.  Must stay on genuine reviewer route.
        title = "FIX: CryptoQuant endpoint hardcoded to BTC regardless of symbol (t_8b7fdb13)"
        reason = (
            "review-required: fix(ai) CryptoQuant per-asset path pushed at "
            "2050aacea on sycode-trading/t_9226842b — fresh exact-head risk "
            "review needed via merge-gate t_0ca5c516. Push voids prior approval "
            "at eeae333cd. 10/10 tests green, 0 new TS errors."
        )
        comment_excerpt = (
            "## Defect 2 fix applied and pushed Fix applied to "
            "origin/sycode-trading/t_9226842b at 2050aacea Changes "
            "server/src/domains/ai/services/validator/preFilters.ts"
        )
        title_body = "\n".join([title, comment_excerpt])
        # Must route to board reviewer (genuine review), not self_improve.
        self.assertEqual(
            R.classify_route_kind("needs_input", reason, title_body),
            "reviewer",
        )


class DiscoverRoutesToTrueOwnerTests(unittest.TestCase):
    """End-to-end: discover_candidates assigns the routed reviewer, not os-reviewer,
    for gate/maker/Frank-blocked handoffs.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rrr-test-")
        self.board_dir = Path(self.tmp.name)
        self._make_board("jarvis-os")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_board(self, slug: str) -> Path:
        db = self.board_dir / slug
        db.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db / "kanban.db")
        con.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
                status TEXT, priority INTEGER, created_by TEXT, created_at INTEGER,
                workspace_kind TEXT, idempotency_key TEXT, block_kind TEXT
            );
            CREATE TABLE task_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, author TEXT,
                body TEXT, created_at INTEGER
            );
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id TEXT,
                kind TEXT, payload TEXT, created_at INTEGER
            );
            CREATE TABLE task_links (parent_id TEXT, child_id text);
            """
        )
        con.commit()
        con.close()
        return db

    def _seed(self, source_id, title, block_kind, reason, reviewer="os-reviewer"):
        con = sqlite3.connect(self.board_dir / "jarvis-os" / "kanban.db")
        now = int(time.time())
        con.execute(
            "INSERT INTO tasks(id,title,body,assignee,status,priority,created_by,created_at,workspace_kind,idempotency_key,block_kind) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, title, "x", reviewer, "blocked", 10, "builder", now, "scratch", f"src-{source_id}", block_kind),
        )
        con.execute(
            "INSERT INTO task_events(task_id,run_id,kind,payload,created_at) VALUES (?,NULL,'blocked',?,?)",
            (source_id, json.dumps({"reason": "review-required: " + reason}), now),
        )
        con.commit()
        con.close()

    def test_gate_defect_handoff_routed_to_self_improve(self):
        self._seed("t_gate1", "Harden liveness matrix", "capability", "staged fix; verdict-router false-positive on VERIFY_PASS")
        cands = R.discover_candidates(self.board_dir, ["jarvis-os"])
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].reviewer, "self-improve-engineer")
        self.assertEqual(cands[0].route_kind, "self_improve")

    def test_maker_dependency_handoff_routed_to_devops(self):
        self._seed("t_dep1", "Fix critic hook", "dependency", "blocked on a running maker task")
        cands = R.discover_candidates(self.board_dir, ["jarvis-os"])
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].reviewer, "devops")
        self.assertEqual(cands[0].route_kind, "devops_owner")

    def test_frank_hold_handoff_routed_to_pm(self):
        self._seed("t_frank1", "Service-gate task", "frank_gate", "Frank A3 hold")
        cands = R.discover_candidates(self.board_dir, ["jarvis-os"])
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].reviewer, "jarvis-os-pm")
        self.assertEqual(cands[0].route_kind, "pm_frank")

    def test_genuine_review_handoff_stays_board_reviewer(self):
        self._seed("t_rev1", "Add unit tests", None, "tests pass; needs reviewer verdict on the source patch")
        cands = R.discover_candidates(self.board_dir, ["jarvis-os"])
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].reviewer, "os-reviewer")
        self.assertEqual(cands[0].route_kind, "reviewer")

    def test_existing_review_card_suppresses_duplicate(self):
        self._seed("t_dup1", "Add unit tests", None, "tests pass; needs reviewer verdict")
        R.discover_candidates(self.board_dir, ["jarvis-os"])
        con = sqlite3.connect(self.board_dir / "jarvis-os" / "kanban.db")
        con.execute(
            "INSERT INTO tasks(id,title,body,assignee,status,priority,created_by,created_at,workspace_kind,idempotency_key,block_kind) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("t_existing", "REVIEW: existing", "x", "os-reviewer", "ready", 10,
             "review-required-auto-router", int(time.time()), "scratch",
             "review-t_dup1-r1", None),
        )
        con.commit()
        con.close()
        cands = R.discover_candidates(self.board_dir, ["jarvis-os"])
        self.assertEqual(cands, [])

    def test_review_required_unresolved_block_event_is_not_a_new_handoff(self):
        # Regression for t_5316e481: an os-reviewer REVIEW card can block with
        # "review-required unresolved: REVIEW_VERDICT=CHANGES_REQUESTED ..." to
        # explain that the *source* remains blocked. That is not a fresh builder
        # review handoff and must not create a nested REVIEW: REVIEW: card.
        con = sqlite3.connect(self.board_dir / "jarvis-os" / "kanban.db")
        now = int(time.time())
        con.execute(
            "INSERT INTO tasks(id,title,body,assignee,status,priority,created_by,created_at,workspace_kind,idempotency_key,block_kind) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("t_review1", "REVIEW: Fix dashboard display exposure (t_source1)", "x", "os-reviewer", "blocked", 10, "review-required-auto-router", now, "scratch", "src-t_review1", "needs_input"),
        )
        con.execute(
            "INSERT INTO task_events(task_id,run_id,kind,payload,created_at) VALUES (?,NULL,'blocked',?,?)",
            (
                "t_review1",
                json.dumps({
                    "reason": "review-required unresolved: REVIEW_VERDICT=CHANGES_REQUESTED posted on t_source1 "
                    "because running-dashboard VERIFY_PASS/browser proof is missing; "
                    "static bundle review is green but UI gate blocks approval."
                }),
                now,
            ),
        )
        con.commit()
        con.close()
        cands = R.discover_candidates(self.board_dir, ["jarvis-os"])
        self.assertEqual(cands, [])

    def test_review_required_unresolved_comment_is_not_a_new_handoff(self):
        # Same guard for the comment detection path: "review-required unresolved"
        # is review status prose, not a review-required handoff marker.
        con = sqlite3.connect(self.board_dir / "jarvis-os" / "kanban.db")
        now = int(time.time())
        con.execute(
            "INSERT INTO tasks(id,title,body,assignee,status,priority,created_by,created_at,workspace_kind,idempotency_key,block_kind) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("t_review2", "REVIEW: source card", "x", "os-reviewer", "blocked", 10, "review-required-auto-router", now, "scratch", "src-t_review2", "needs_input"),
        )
        con.execute(
            "INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,?,?,?)",
            (
                "t_review2",
                "os-reviewer",
                "review-required unresolved: REVIEW_VERDICT=CHANGES_REQUESTED posted on t_source2; needs builder rework.",
                now,
            ),
        )
        con.commit()
        con.close()

        cands = R.discover_candidates(self.board_dir, ["jarvis-os"])
        self.assertEqual(cands, [])

    def test_successful_live_gate_handoff_stays_board_reviewer(self):
        # End-to-end form of t_56873697 / source t_87fffe68: VERIFY_PASS/PASS
        # evidence in a handoff should not be treated as a Boris gate defect.
        self._seed(
            "t_livegate1",
            "Run live completion gates and prepare review block",
            "needs_input",
            "both live gates PASS for / and /kanban; evidence note/comment "
            "posted, but reviewer must reconcile dirty/no-remote runtime "
            "checkout and HEAD 976deba vs parent-referenced add5c16 before "
            "final closeout.",
        )
        cands = R.discover_candidates(self.board_dir, ["jarvis-os"])
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].reviewer, "os-reviewer")
        self.assertEqual(cands[0].route_kind, "reviewer")


if __name__ == "__main__":
    unittest.main()
