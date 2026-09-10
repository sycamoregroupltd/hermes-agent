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
                    title: str = "review-required source patch", body: str = "review-required source",
                    block_kind: str | None = None) -> None:
        _, db = board
        con = sqlite3.connect(db)
        con.execute(
            "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at,block_kind) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (task_id, title, body, "devops", status, 10, int(time.time()), block_kind),
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

    # --- Negation-scope regression fixtures (t_225705fd) -------------------
    # These three cases were CHANGES_REQUESTED across 9 review cycles because
    # the detector inherited the router's negation-aware matcher, which
    # suppressed a genuine out-of-contract verdict token whenever ANY negation
    # cue appeared later in the SAME SENTENCE, even in an unrelated clause.
    # All three MUST be flagged.

    def test_negation_scope_approve_with_notes_but_could_not_be_performed(self) -> None:
        """Negation cue ('could not') in a later, unrelated clause must not suppress detection."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_neg_notes", "blocked")
            self.add_comment(
                board, "t_neg_notes", "os-reviewer",
                "REVIEW_VERDICT=APPROVE_WITH_NOTES — implementation passed, but live apply could not be performed.",
            )
            findings = self.scan(board)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].verdict_value, "APPROVE_WITH_NOTES")

    def test_negation_scope_technical_request_changes_not_a_runner_flake(self) -> None:
        """Negation cue ('not a runner flake') must not suppress detection.

        Note: VERDICT_RE captures [A-Z0-9_]+ (no hyphen), matching the
        router's own tokenizer, so the hyphenated verdict is captured up to
        its first hyphen ("TECHNICAL"). That's still correctly flagged as
        malformed (not in VALID_VERDICTS) — this test's job is only to prove
        the negation cue does not suppress the flag entirely.
        """
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_neg_technical", "review")
            self.add_comment(
                board, "t_neg_technical", "os-reviewer",
                "REVIEW_VERDICT=TECHNICAL-REQUEST-CHANGES @ target; this is not a runner flake.",
            )
            findings = self.scan(board)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].verdict_value, "TECHNICAL")

    def test_negation_scope_restore_scoped_backup_no_broad_cleanup(self) -> None:
        """Negation cue ('no broad cleanup') must not suppress detection."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_neg_restore", "blocked")
            self.add_comment(
                board, "t_neg_restore", "os-reviewer",
                "REVIEW_VERDICT=CHANGES_REQUIRED_RESTORE_SCOPED_BACKUP — no broad cleanup.",
            )
            findings = self.scan(board)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].verdict_value, "CHANGES_REQUIRED_RESTORE_SCOPED_BACKUP")

    def test_negation_scope_valid_verdict_not_flagged(self) -> None:
        """Control: a genuinely valid verdict alongside negation prose is still not flagged."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_neg_valid", "blocked")
            self.add_comment(
                board, "t_neg_valid", "os-reviewer",
                "REVIEW_VERDICT=APPROVED — valid review result, no further action needed.",
            )
            self.assertEqual(self.scan(board), [])

    # --- Citation-aware exclusion (t_78137279) ------------------------------
    # t_225705fd removed negation-scope suppression, but that regressed a
    # different case: remediation/triage comments that CITE a historical
    # non-contract verdict to explain/dismiss it (not declare a new one) were
    # re-flagged every cycle — a self-perpetuating noise loop. These fixtures
    # are the exact false-positive comments from the report (t_20f0ef3d
    # comment 48401, t_eda793bd comment 60573, t_e728509f comment 60572) plus
    # the task's own citation example, and must NOT be flagged. The three
    # negation-scope MUST-FLAG fixtures above prove no regression: a citation
    # cue is a narrower, additive condition than a bare negation word and
    # those fixtures contain no citation cue phrase.

    def test_citation_backtick_wrapped_historical_value_not_flagged(self) -> None:
        """t_20f0ef3d comment 48401: backtick-wrapped historical citation, not a declaration."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_cite_blocked", "blocked")
            self.add_comment(
                board, "t_cite_blocked", "fleet-engineer",
                "Fleet-engineer verdict-blackhole remediation (2026-09-10): this card's legacy "
                "comment-only `REVIEW_VERDICT: BLOCKED` values are historical artifacts of "
                "os-reviewer using a non-contract verdict string. The card ALREADY carries a "
                "proper native kanban_block(kind=needs_input) from the 2026-09-03 re-verification, "
                "which is the authoritative state. The BLOCKED verdict values are false positives "
                "from the detector's negation-scope bug. That bug is tracked as jarvis-os/t_225705fd. "
                "No status/assignee/block_kind mutation from this comment.",
            )
            self.assertEqual(self.scan(board), [])

    def test_citation_one_character_off_contract_not_flagged_but_reissue_is_kept(self) -> None:
        """t_eda793bd comment 60573 / t_e728509f comment 60572 pattern.

        The comment cites the bad historical CHANGES_REQUIRED token (excluded as a
        citation) AND reissues the valid CHANGES_REQUESTED verdict (kept, in-contract
        so never subject to the citation check) — net result: not flagged.
        """
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_cite_reissue", "blocked")
            self.add_comment(
                board, "t_cite_reissue", "fleet-engineer",
                "Fleet-engineer verdict-blackhole remediation (2026-09-10): the trading-risk-reviewer "
                "issued REVIEW_VERDICT: CHANGES_REQUIRED which is one character off the router contract "
                "value CHANGES_REQUESTED. Reissuing in contract terms: REVIEW_VERDICT: CHANGES_REQUESTED. "
                "Card remains blocked; no status/assignee/block_kind mutation.",
            )
            self.assertEqual(self.scan(board), [])

    def test_citation_task_description_example_not_flagged(self) -> None:
        """The task body's own MUST-NOT-flag example (backtick citation + cue words)."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_cite_taskbody", "blocked")
            self.add_comment(
                board, "t_cite_taskbody", "fleet-engineer",
                "Fleet-engineer remediation: the legacy `REVIEW_VERDICT: BLOCKED` comment is a "
                "historical non-contract value; card is authoritatively kanban_block(needs_input).",
            )
            self.assertEqual(self.scan(board), [])

    def test_citation_exclusion_does_not_suppress_fresh_declaration(self) -> None:
        """Control: an out-of-contract token with NO citation framing is still flagged."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_fresh_bad", "blocked")
            self.add_comment(
                board, "t_fresh_bad", "os-reviewer",
                "REVIEW_VERDICT=APPROVE_WITH_NOTES\nLooks good, minor notes.",
            )
            findings = self.scan(board)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].verdict_value, "APPROVE_WITH_NOTES")

    # --- Native-disposition suppression (t_d59135ab) ------------------------
    # A card that already carries an intentional, evidence-based native
    # disposition (blocked+block_kind set, or a terminal status) is not a
    # router-invisible black hole. A stale/superseded REVIEW_VERDICT comment
    # string on such a card must stop being re-flagged forever. status=blocked
    # with block_kind NULL is a control: it must still be flagged, proving
    # this is additive suppression, not a blanket "ignore blocked cards" bug.

    def test_blocked_with_block_kind_set_suppresses_stale_verdict(self) -> None:
        """status=blocked + block_kind=needs_input suppresses a stale non-contract verdict."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_native_blocked", "blocked", block_kind="needs_input")
            self.add_comment(
                board, "t_native_blocked", "os-reviewer",
                "REVIEW_VERDICT: BLOCKED-needs-Frank",
            )
            self.assertEqual(self.scan(board), [])

    def test_blocked_with_block_kind_null_still_flagged(self) -> None:
        """Regression guard: status=blocked + block_kind=NULL, no later disposition, STILL flags."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_native_blocked_no_kind", "blocked", block_kind=None)
            self.add_comment(
                board, "t_native_blocked_no_kind", "os-reviewer",
                "REVIEW_VERDICT: BLOCKED-needs-Frank",
            )
            findings = self.scan(board)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].task_id, "t_native_blocked_no_kind")

    def test_done_status_never_in_scope(self) -> None:
        """status=done is excluded structurally by the in-scope SQL filter (not just block_kind)."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_native_done", "done", block_kind=None)
            self.add_comment(
                board, "t_native_done", "os-reviewer",
                "REVIEW_VERDICT: BLOCKED-needs-Frank",
            )
            self.assertEqual(self.scan(board), [])

    def test_live_replay_three_target_cards_drop_out(self) -> None:
        """Live-replay acceptance: t_20f0ef3d / t_8a2cecff / t_9530dd4f-style cards
        (status=blocked, block_kind set, stale non-contract REVIEW_VERDICT comment,
        no later plain-English disposition token) all drop out of findings."""
        tmp, board = self.make_board()
        with tmp:
            self.insert_task(board, "t_20f0ef3d", "blocked", block_kind="needs_input")
            self.add_comment(board, "t_20f0ef3d", "some-reviewer", "REVIEW_VERDICT: BLOCKED-needs-Frank")
            self.insert_task(board, "t_8a2cecff", "blocked", block_kind="needs_input")
            self.add_comment(board, "t_8a2cecff", "platform-reviewer", "REVIEW_VERDICT=BLOCKED_CAPABILITY")
            self.add_comment(board, "t_8a2cecff", "platform-reviewer",
                             "REVIEW_VERDICT=NON_BINDING_APPROVE_ON_ARCHITECTURE")
            self.insert_task(board, "t_9530dd4f", "blocked", block_kind="needs_input")
            self.add_comment(board, "t_9530dd4f", "fable-reviewer", "REVIEW_VERDICT=TECHNICAL-REQUEST-CHANGES")
            self.assertEqual(self.scan(board), [])


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
