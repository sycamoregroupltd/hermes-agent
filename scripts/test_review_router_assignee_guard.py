#!/usr/bin/env python3
"""Regression coverage for sycode-trading/t_jarvis_revrouter_20260728.

The guard must block the *creation* of review/verification cards (title leads
with REVIEW/REWORK/VERDICT/Post-state review/terminal review, or body carries
REVIEW_VERDICT/REWORK_REQUIRED/post-state review/terminal review) unless the
assignee is a terminal-capable reviewer profile (os-reviewer, guardian,
platform-reviewer, devops, trading-risk-reviewer). Implementation cards that
merely mention review after the leading verb must still be allowed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_review_card_blocked_on_non_terminal_assignee() -> None:
    guard = load_module("kanban_dedupe_guard", ROOT / "scripts" / "kanban_dedupe_guard.py")

    reason = guard.review_assignee_block_reason(
        "REVIEW PR #452 — persist + hydrate stablecoin flow (t_21af0)",
        "Independent review of the branch.",
        "integration-builder",
    )
    assert reason is not None
    assert "terminal-capable reviewer" in reason
    assert "integration-builder" in reason


def test_review_card_allowed_on_terminal_capable_reviewer() -> None:
    guard = load_module("kanban_dedupe_guard", ROOT / "scripts" / "kanban_dedupe_guard.py")

    for assignee in ("os-reviewer", "guardian", "platform-reviewer", "devops", "trading-risk-reviewer"):
        reason = guard.review_assignee_block_reason(
            "REVIEW PR #452 — persist + hydrate stablecoin flow",
            "Independent review of the branch.",
            assignee,
        )
        assert reason is None, f"{assignee} should be allowed: {reason}"


def test_rework_title_blocked_on_pm() -> None:
    guard = load_module("kanban_dedupe_guard", ROOT / "scripts" / "kanban_dedupe_guard.py")

    reason = guard.review_assignee_block_reason(
        "REWORK PR #686: RouteOrder fee fields must wire Rust",
        "Reviewer requested changes.",
        "sycode-trading-pm",
    )
    assert reason is not None
    assert "sycode-trading-pm" in reason


def test_verdict_body_blocked_on_jarvis() -> None:
    guard = load_module("kanban_dedupe_guard", ROOT / "scripts" / "kanban_dedupe_guard.py")

    reason = guard.review_assignee_block_reason(
        "Some card title",
        "REVIEW_VERDICT=CHANGES_REQUESTED — fee fields wrong. Target: t_x",
        "jarvis",
    )
    assert reason is not None
    assert "jarvis" in reason


def test_implement_after_review_is_not_a_review_card() -> None:
    guard = load_module("kanban_dedupe_guard", ROOT / "scripts" / "kanban_dedupe_guard.py")

    # Leading verb is "IMPLEMENT", not a review term -> not caught by RULE 6,
    # so a non-reviewer assignee is allowed.
    reason = guard.review_assignee_block_reason(
        "IMPLEMENT AFTER REVIEW: Grok-ARM Arm C daily shadow wrapper",
        "Build the wrapper after the review lands.",
        "trading-devops",
    )
    assert reason is None


def test_unassigned_review_card_blocked() -> None:
    guard = load_module("kanban_dedupe_guard", ROOT / "scripts" / "kanban_dedupe_guard.py")

    reason = guard.review_assignee_block_reason(
        "REVIEW: t_0cf4dd5b DSR purge comparison remediation",
        "Review the remediation.",
        "",
    )
    assert reason is not None
    assert "unassigned" in reason.lower() or "(unassigned)" in reason


def test_plain_work_card_not_flagged() -> None:
    guard = load_module("kanban_dedupe_guard", ROOT / "scripts" / "kanban_dedupe_guard.py")

    reason = guard.review_assignee_block_reason(
        "FIX: Add COINGECKO_NEWS_PRO_ENABLED to env schema",
        "Update .env.example and validate.",
        "integration-builder",
    )
    assert reason is None
