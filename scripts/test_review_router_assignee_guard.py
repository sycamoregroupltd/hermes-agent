#!/usr/bin/env python3
"""Regression coverage for RULE 6 reviewer-routing terminal-capability gate
(jarvis-os t_2a069cba).

A review-class card (title LEADS with REVIEW/REWORK/VERDICT/Pre-review/
Post-state review/Terminal review, OR body carries REVIEW_VERDICT /
REWORK_REQUIRED / post-state review / terminal review / risk-verdict) must
only be assigned to a profile that carries the `terminal` toolset in its
config.yaml `toolsets:`. The capability is verified at runtime by
`profile_has_terminal()` — never a hard-coded name allowlist — so the gate
stays correct as profiles change.

IMPORTANT (2026-07-31 reality check): several profiles that lacked `terminal`
at the time of the 2026-07-28 decision note have since gained it. Verified
directly from config.yaml:
  terminal-capable (allowed):  os-reviewer, guardian, platform-reviewer,
    builder, capability-builder, integration-builder, sycode-trading-pm,
    jarvis, trading-devops, trading-risk-reviewer
  NOT terminal-capable (blocked): devops ([hermes-cli, kanban])

The canonical non-terminal reviewer example is therefore `devops`, which is
also the profile the original task body wrongly listed as terminal-capable.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Verified terminal-capable reviewer profiles (from config.yaml `toolsets:`).
TERMINAL_CAPABLE = (
    "os-reviewer",
    "guardian",
    "platform-reviewer",
    "builder",
    "capability-builder",
    "integration-builder",
    "sycode-trading-pm",
    "jarvis",
    "trading-devops",
    "trading-risk-reviewer",
)
# Genuinely non-terminal-capable reviewer profile(s) — the canonical reject case.
NON_TERMINAL = "devops"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _guard():
    return load_module(
        "kanban_dedupe_guard", ROOT / "scripts" / "kanban_dedupe_guard.py"
    )


# --- capability detector (the source of truth for the gate) ----------------


def test_profile_has_terminal_detects_devops_as_non_terminal() -> None:
    guard = _guard()
    assert guard.profile_has_terminal(NON_TERMINAL) is False


def test_profile_has_terminal_detects_terminal_capable_profiles() -> None:
    guard = _guard()
    for p in TERMINAL_CAPABLE:
        assert guard.profile_has_terminal(p) is True, f"{p} should be terminal"


def test_profile_has_terminal_fails_closed_on_unknown_profile() -> None:
    guard = _guard()
    assert guard.profile_has_terminal("no-such-profile-xyz") is False


# --- RULE 6 gate behavior ---------------------------------------------------


def test_review_card_blocked_on_non_terminal_devops() -> None:
    guard = _guard()
    reason = guard.review_assignee_block_reason(
        "REVIEW PR #452 — persist + hydrate stablecoin flow (t_21af0)",
        "Independent review of the branch.",
        NON_TERMINAL,
    )
    assert reason is not None
    assert "terminal-capable" in reason
    assert NON_TERMINAL in reason


def test_rework_title_blocked_on_devops() -> None:
    guard = _guard()
    reason = guard.review_assignee_block_reason(
        "REWORK PR #686: RouteOrder fee fields must wire Rust",
        "Reviewer requested changes.",
        NON_TERMINAL,
    )
    assert reason is not None
    assert NON_TERMINAL in reason


def test_verdict_body_blocked_on_devops() -> None:
    guard = _guard()
    reason = guard.review_assignee_block_reason(
        "Some card title",
        "REVIEW_VERDICT=CHANGES_REQUESTED — fee fields wrong. Target: t_x",
        NON_TERMINAL,
    )
    assert reason is not None
    assert NON_TERMINAL in reason


def test_review_card_allowed_on_every_terminal_capable_reviewer() -> None:
    guard = _guard()
    for assignee in TERMINAL_CAPABLE:
        reason = guard.review_assignee_block_reason(
            "REVIEW PR #452 — persist + hydrate stablecoin flow",
            "Independent review of the branch.",
            assignee,
        )
        assert reason is None, f"{assignee} is terminal-capable; should be allowed: {reason}"


def test_unassigned_review_card_blocked() -> None:
    guard = _guard()
    # Title LEADS with REVIEW -> review-class; with no assignee it must be blocked.
    reason = guard.review_assignee_block_reason(
        "REVIEW t_0cf4dd5b DSR purge comparison remediation",
        "Independent review of the remediation.",
        "",
    )
    assert reason is not None
    assert "unassigned" in reason.lower() or "UNASSIGNED" in reason


def test_unassigned_plain_body_mention_not_flagged() -> None:
    guard = _guard()
    # A body-only mention of "review" on a non-review-title card is NOT
    # review-class (avoids blocking work cards that merely discuss a review).
    reason = guard.review_assignee_block_reason(
        "FIX: DSR purge comparison",
        "Review the remediation with the analyst.",
        "",
    )
    assert reason is None


def test_implement_after_review_is_not_a_review_card() -> None:
    guard = _guard()
    # Leading verb is "IMPLEMENT", not a review term -> not caught by RULE 6,
    # so a non-reviewer assignee is allowed.
    reason = guard.review_assignee_block_reason(
        "IMPLEMENT AFTER REVIEW: Grok-ARM Arm C daily shadow wrapper",
        "Build the wrapper after the review lands.",
        NON_TERMINAL,
    )
    assert reason is None


def test_plain_work_card_not_flagged() -> None:
    guard = _guard()
    reason = guard.review_assignee_block_reason(
        "FIX: Add COINGECKO_NEWS_PRO_ENABLED to env schema",
        "Update .env.example and validate.",
        NON_TERMINAL,
    )
    assert reason is None
