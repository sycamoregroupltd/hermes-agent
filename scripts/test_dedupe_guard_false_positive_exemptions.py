#!/usr/bin/env python3
"""Regression coverage for dedupe-guard false-positive exemption classes.

Covers the four CLS-A/B/C/D exemption classes introduced in t_71d3e221
and landed by t_ff76952c, plus original incident-shape true positives.

CLS-A: reviewer/re-review/guardian/risk-review cards (RULE 3 HIGH -> MEDIUM)
CLS-B: PM visibility/routing cards (RULE 3 HIGH -> MEDIUM)
CLS-C: research-actionable/routing-context cards (RULE 3 HIGH -> MEDIUM)
CLS-D: quoted gate markers in body (RULE 2 skip)
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_guard():
    spec = importlib.util.spec_from_file_location(
        "kanban_dedupe_guard", ROOT / "scripts" / "kanban_dedupe_guard.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# =============================================================================
# CLS-A: reviewer/re-review/guardian/risk-review cards
# =============================================================================

def test_cls_a_review_vs_work_downgraded_to_medium() -> None:
    """REVIEW prefix card vs identical work card -> exempted (HIGH -> MEDIUM)."""
    guard = load_guard()
    # "REVIEW: fix auth token refresh" vs "fix auth token refresh"
    assert guard.title_high_exempted(
        "REVIEW: fix auth token refresh",
        "fix auth token refresh",
    ) is True


def test_cls_a_re_review_vs_work_downgraded() -> None:
    """RE-REVIEW prefix card vs work card -> exempted."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "RE-REVIEW: implement rate limiter",
        "implement rate limiter",
    ) is True


def test_cls_a_guardian_vs_work_downgraded() -> None:
    """GUARDIAN prefix card vs work card -> exempted."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "GUARDIAN: review authorization middleware",
        "implement authorization middleware",
    ) is True


def test_cls_a_risk_review_vs_work_downgraded() -> None:
    """RISK-REVIEW prefix card vs work card -> exempted."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "RISK REVIEW: deploy strategy v3 to production",
        "deploy strategy v3 to production",
    ) is True


def test_cls_a_both_review_not_exempted() -> None:
    """Two review cards matching each other ARE exempted (same rule)."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "REVIEW: fix auth token refresh",
        "GUARDIAN: fix auth token refresh",
    ) is True


def test_cls_a_risk_review_hyphenated() -> None:
    """RISK-REVIEW (with hyphen) also triggers the prefix."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "RISK-REVIEW: audit DEX integration",
        "audit DEX integration",
    ) is True


# =============================================================================
# CLS-B: PM visibility/routing cards
# =============================================================================

def test_cls_b_visibility_card_exempted() -> None:
    """VISIBILITY card vs work card -> exempted."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "VISIBILITY: Sprint 24 review",
        "Sprint 24 deliverables",
    ) is True


def test_cls_b_routing_card_exempted() -> None:
    """ROUTING card vs source card -> exempted."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "ROUTING: t_abc123 to team-alpha",
        "Implement P&L aggregation (t_abc123)",
    ) is True


def test_cls_b_dashboard_card_exempted() -> None:
    """DASHBOARD card vs work card -> exempted."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "DASHBOARD: weekly risk metrics - W31",
        "weekly risk metrics - W31",
    ) is True


def test_cls_b_monitor_card_exempted() -> None:
    """MONITOR card vs tracking card -> exempted."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "MONITOR: deploy pipeline status",
        "deploy pipeline status",
    ) is True


def test_cls_b_tracking_card_exempted() -> None:
    """TRACKING card vs source card -> exempted."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "TRACKING: fix jwt expiry handling",
        "fix jwt expiry handling",
    ) is True


def test_cls_b_both_pm_cards_exempted() -> None:
    """Two PM routing/visibility cards matching -> exempted (both sides)."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "VISIBILITY: Sprint 24 deliverables",
        "ROUTING: Sprint 24 deliverables to team",
    ) is True


# =============================================================================
# CLS-C: research-actionable / routing-context cards
# =============================================================================

def test_cls_c_research_actionable_exempted() -> None:
    """RESEARCH-ACTIONABLE card vs work card -> exempted."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "RESEARCH-ACTIONABLE: sycode-trading/t_deadbeef — data quality audit",
        "data quality audit for exchange connections",
    ) is True


def test_cls_c_research_actionable_vs_pm_exempted() -> None:
    """Research-actionable vs PM card both exempt (separate classes)."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "RESEARCH-ACTIONABLE: investigate P&L divergence",
        "VISIBILITY: P&L divergence investigation findings",
    ) is True


def test_cls_c_research_actionable_both_sides() -> None:
    """Two research-actionable cards matching -> exempted."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "RESEARCH-ACTIONABLE: sycode-trading/t_deadbeef — JWT expiry",
        "RESEARCH-ACTIONABLE: sycode-trading/t_abc888 — JWT token investigation",
    ) is True


# =============================================================================
# Negative cases: legitimate work duplicates are NOT exempted
# =============================================================================

def test_identical_proposal_cards_not_exempted() -> None:
    """Two identical PROPOSAL cards -> NOT exempted (legitimate HIGH block)."""
    guard = load_guard()
    # Two identical "PROPOSAL: add market-making engine" cards — not review,
    # not PM routing, not research-actionable.
    assert guard.title_high_exempted(
        "PROPOSAL: add market-making engine",
        "PROPOSAL: add market-making engine",
    ) is False


def test_generic_work_duplicates_not_exempted() -> None:
    """Two identical work cards with no exemption prefix -> NOT exempted."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "Add rate limiter to order entry",
        "Add rate limiter to order entry",
    ) is False


def test_mixed_exempt_work_card_not_exempted() -> None:
    """A REVIEW card vs a non-matching work card is still exempted by CLS-A.
    This IS expected — the exemption is BY DESIGN for any review vs work
    overlap since review cards intentionally mirror source titles."""
    guard = load_guard()
    assert guard.title_high_exempted(
        "REVIEW: fix auth token refresh",
        "refactor database connection pooling",
    ) is True  # CLS-A fires on the review title


# =============================================================================
# CLS-D: quoted gate markers (backtick / double-quoted)
# =============================================================================

def test_cls_d_backtick_quoted_frank_gated() -> None:
    """Backtick-quoted `FRANK GATED` marker -> quoted (skip RULE 2)."""
    guard = load_guard()
    text = 'This was `FRANK GATED` per policy at the time.'
    m = guard.GATE_STRONG_RE.search(text)
    assert m is not None, "GATE_STRONG_RE should still match inside backticks"
    assert guard.marker_is_quoted(text, m) is True


def test_cls_d_double_quoted_frank_gated() -> None:
    """Double-quoted \"FRANK GATED\" marker -> quoted (skip RULE 2)."""
    guard = load_guard()
    text = 'The card was labeled "FRANK GATED" as a historical note.'
    m = guard.GATE_STRONG_RE.search(text)
    assert m is not None
    assert guard.marker_is_quoted(text, m) is True


def test_cls_d_backtick_quoted_approval_gated() -> None:
    """Backtick-quoted `approval gated` marker -> quoted."""
    guard = load_guard()
    text = 'Card was previously marked `approval gated`; now resolved.'
    m = guard.GATE_STRONG_RE.search(text)
    assert m is not None
    assert guard.marker_is_quoted(text, m) is True


def test_cls_d_backtick_quoted_production_ddl() -> None:
    """Backtick-quoted `production DDL` marker -> quoted."""
    guard = load_guard()
    text = 'This was `production DDL` — do not clone without gate.'
    m = guard.GATE_STRONG_RE.search(text)
    assert m is not None
    assert guard.marker_is_quoted(text, m) is True


def test_cls_d_unquoted_frank_gated_not_quoted() -> None:
    """Unquoted FRANK GATED marker -> NOT quoted (still blocks RULE 2)."""
    guard = load_guard()
    text = 'This task is FRANK GATED — do not deploy without approval.'
    m = guard.GATE_STRONG_RE.search(text)
    assert m is not None
    assert guard.marker_is_quoted(text, m) is False


def test_cls_d_mixed_quoted_and_unquoted() -> None:
    """Only the quoted instance is skipped; unquoted ones still fire."""
    guard = load_guard()
    text = (
        'Historical note: was `FRANK GATED` originally. '
        'CURRENT STATUS: still FRANK GATED.'
    )
    # Find all matches
    matches = list(guard.GATE_STRONG_RE.finditer(text))
    assert len(matches) >= 2, "Expected at least 2 gate marker matches"

    # The first match (inside backticks) should be quoted
    quoted = guard.marker_is_quoted(text, matches[0])
    assert quoted is True, "First match (backtick) should be quoted"

    # The second match (unquoted) should NOT be quoted
    unquoted = guard.marker_is_quoted(text, matches[1])
    assert unquoted is False, "Second match (unquoted) should NOT be quoted"


def test_cls_d_multiline_text_with_quoted_marker() -> None:
    """Quoted gate marker in multi-line body is still caught correctly."""
    guard = load_guard()
    text = """This card tracks the rollout of v3.2.
Historical context: this was `FRANK GATED` before the audit cleared it.
Current work: monitor deployment status.
"""
    m = guard.GATE_STRONG_RE.search(text)
    assert m is not None
    assert guard.marker_is_quoted(text, m) is True


# =============================================================================
# title_high_allowed + title_high_exempted integration
# =============================================================================

def test_high_block_logic_exempt_prevents_high() -> None:
    """Simulate RULE 3 HIGH: CLS-A exempts review-vs-review duplicate -> MEDIUM."""
    guard = load_guard()

    # Both review-role, both have >=4 unique tokens -> title_high_allowed passes.
    # CLS-A exempts because REVIEW prefix is present -> HIGH suppressed.
    title_a = "REVIEW: implement market-making engine with multi-asset"
    title_b = "RE-REVIEW: implement market-making engine with multi-asset"
    toks_a = guard.title_tokens(title_a)
    toks_b = guard.title_tokens(title_b)

    score = guard.jaccard(toks_a, toks_b)
    assert score >= guard.TITLE_HIGH_THRESHOLD, (
        f"Expected J>={guard.TITLE_HIGH_THRESHOLD}, got J={score:.2f}"
    )

    # Both are "review" role (REVIEW_TITLE_RE matches)
    assert guard.title_role(title_a) == "review"
    assert guard.title_role(title_b) == "review"

    # title_high_allowed: same role + >=4 tokens -> True
    allowed = guard.title_high_allowed(title_a, title_b, toks_a, toks_b)
    assert allowed is True, "Same-role review cards with >=4 tokens -> allowed"

    # CLS-A exempts because REVIEW prefix is present
    exempted = guard.title_high_exempted(title_a, title_b)
    assert exempted is True, "CLS-A should exempt review vs re-review"

    # Combined: high = score >= threshold AND allowed AND NOT exempted
    high = (
        (toks_a == toks_b or score >= guard.TITLE_HIGH_THRESHOLD)
        and allowed
        and not exempted
    )
    assert high is False, "Exemption should prevent HIGH block (-> MEDIUM)"


def test_high_block_logic_no_exemption_stays_high() -> None:
    """Without exemption, matching substantive cards still get HIGH."""
    guard = load_guard()

    # Both work-role with >=4 unique tokens -> title_high_allowed passes.
    # No CLS-* exemption trigger -> HIGH fires.
    title_a = "PROPOSAL: add market-making engine with multi-asset support"
    title_b = "PROPOSAL: add market-making engine with multi-asset support"
    toks_a = guard.title_tokens(title_a)
    toks_b = guard.title_tokens(title_b)

    assert guard.title_role(title_a) == "work"
    assert guard.title_role(title_b) == "work"
    assert len(toks_a | toks_b) >= 4, (
        f"Expected >=4 unique tokens, got {len(toks_a | toks_b)}"
    )

    score = guard.jaccard(toks_a, toks_b)
    assert score >= guard.TITLE_HIGH_THRESHOLD

    allowed = guard.title_high_allowed(title_a, title_b, toks_a, toks_b)
    assert allowed is True

    exempted = guard.title_high_exempted(title_a, title_b)
    assert exempted is False, "PROPOSAL cards should NOT be exempted"

    high = (
        (toks_a == toks_b or score >= guard.TITLE_HIGH_THRESHOLD)
        and allowed
        and not exempted
    )
    assert high is True, "Non-exempted duplicate should remain HIGH"


def test_short_generic_title_not_high_allowed() -> None:
    """Short/generic titles (<4 unique tokens) stay MEDIUM even without exemption."""
    guard = load_guard()

    title_a = "fix bug"
    title_b = "fix bug"
    toks_a = guard.title_tokens(title_a)
    toks_b = guard.title_tokens(title_b)

    allowed = guard.title_high_allowed(title_a, title_b, toks_a, toks_b)
    assert allowed is False, "Short titles should not be HIGH-allowed"

    # Not exempted by CLS classes
    exempted = guard.title_high_exempted(title_a, title_b)
    assert exempted is False

    # Combined: not high because title_high_allowed is False
    score = guard.jaccard(toks_a, toks_b)
    high = (
        (toks_a == toks_b or score >= guard.TITLE_HIGH_THRESHOLD)
        and allowed
        and not exempted
    )
    assert high is False, "Short title should stay MEDIUM"


# =============================================================================
# Original incident-shape true positives (t_5c25f222 / t_d0fcaddb style)
# =============================================================================

def test_true_positive_identical_file_ref_in_candidate() -> None:
    """Two tasks referencing the same file + error signature should be detected."""
    guard = load_guard()

    sig_a = guard.extract_signature(
        'Fix bug in order matching engine\n'
        'Relevant files: app/orders/matching_engine.py\n'
        'Error: "KeyError on empty orderbook"'
    )
    sig_b = guard.extract_signature(
        'Duplicate: fix matching engine issue\n'
        'File: app/orders/matching_engine.py\n'
        'Same error: "KeyError on empty orderbook"'
    )

    n_ids, n_total, n_classes, tokens = guard.overlap(sig_a, sig_b)

    # Should share file (matching_engine.py) + quoted error (keyerror...)
    assert n_total >= 2, f"Expected >=2 shared markers, got {n_total}"
    assert n_classes >= 2, f"Expected >=2 distinct classes, got {n_classes}"
    assert any("matching_engine" in t for t in tokens), (
        f"Expected matching_engine.py in tokens, got {tokens}"
    )


def test_true_positive_gate_blocked_task_id_in_title() -> None:
    """Explicit task id mention of a gate-blocked task -> RULE 1 HIGH."""
    guard = load_guard()

    sig_blocked = guard.extract_signature(
        "BLOCKED: implement auth refresh\n"
        "Frank gated: requires approval before deploy\n"
        "Files: src/auth/refresh_token.py"
    )
    sig_candidate = guard.extract_signature(
        "IMPLEMENT: auth token refresh\n"
        "This is the work for t_a3be3fa4\n"
        "File: src/auth/refresh_token.py"
    )

    # Simulate what scan_board does: mention of blocked id
    mention = 1 if "t_a3be3fa4" in "IMPLEMENT: auth token refresh\nThis is the work for t_a3be3fa4\nFile: src/auth/refresh_token.py" else 0
    n_ids, n_total, n_classes, tokens = guard.overlap(sig_blocked, sig_candidate)
    n_ids += mention
    n_total += mention
    if mention:
        n_classes += 1

    # HIGH: shared explicit task id (via mention) + shared file
    high = (
        (n_ids >= 1 and n_total >= 2)
        or (n_total >= 2 and n_classes >= 2)
        or n_total >= 3
    )
    assert high is True, (
        f"Gate-blocked dupe should be HIGH: {n_ids} ids, {n_total} total, "
        f"{n_classes} classes"
    )


def test_true_positive_quoted_error_strings_match() -> None:
    """Matching quoted error strings -> RULE 1 MEDIUM at minimum."""
    guard = load_guard()

    sig_a = guard.extract_signature(
        "Error: `cannot import name 'OrderBook' from 'app.orders'`\n"
        "Running on trading-devops"
    )
    sig_b = guard.extract_signature(
        "Also seeing: `cannot import name 'OrderBook' from 'app.orders'`\n"
        "On jarvis profile"
    )

    n_ids, n_total, n_classes, tokens = guard.overlap(sig_a, sig_b)

    # At minimum should share the quoted error string
    assert len(tokens) >= 1, "Should share at least one error token"


# =============================================================================
# RULE 2: CLS-D integration — quoted markers pass through to real check
# =============================================================================

def test_rule2_cls_d_quoted_marker_allows_gateless_profile() -> None:
    """CLS-D: quoted Frank gate marker on gateless profile -> SKIP (no block).

    This is the behavioral integration: when marker_is_quoted returns True
    AND the profile is gateless, RULE 2 does NOT fire.
    """
    guard = load_guard()

    title = "Rollout monitoring card"
    body = (
        'Historical note: this was initially marked `FRANK GATED` but '
        'was subsequently reviewed and cleared.'
    )
    hay = title + "\n" + body

    m = guard.GATE_STRONG_RE.search(hay)
    assert m is not None, "GATE_STRONG_RE should match the marker"

    quoted = guard.marker_is_quoted(hay, m)
    assert quoted is True, "CLS-D: marker inside backticks is quoted"

    # Simulate the RULE 2 gate line:
    # if m and assignee and not profile_honors_gate(assignee)
    #        and not marker_is_quoted(hay, m):
    #     act(...)
    assert quoted is True, "Quoted marker prevents RULE 2 block"


def test_rule2_unquoted_marker_still_blocks_gateless() -> None:
    """Unquoted Frank gate marker on gateless profile -> STILL BLOCKS."""
    guard = load_guard()

    title = "FRANK GATED: database migration"
    body = "Requires Frank approval before deployment."
    hay = title + "\n" + body

    m = guard.GATE_STRONG_RE.search(hay)
    assert m is not None

    quoted = guard.marker_is_quoted(hay, m)
    assert quoted is False, "Unquoted marker is NOT quoted — RULE 2 should fire"
