#!/usr/bin/env python3
"""Importable deterministic REVIEW_VERDICT fixtures for router tests.

Each fixture is a ``VerdictFixture`` dataclass with stable, deterministic fields.
All 21 cases from the approved matrix are named as module-level constants.

Usage::

    from agent_hooks.verdict_router_fixtures import (
        APPROVED_SOURCE_CARD_COMPLETES,
        CHANGES_REQUESTED_UNBLOCKS_WITH_QUOTED_FINDING,
        ALL_CASES,
    )

    for case in ALL_CASES:
        print(case.name, case.expect.action)

No live board APIs, no current-time dependencies, no network calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Fixture data types ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Comment:
    """A single deterministic comment on a task, exactly as in the fixture matrix."""

    id: int | str  # '%s' for corrupt-id cases
    author: str
    created_at: int | str  # '%s' for corrupt-timestamp cases
    body: str


@dataclass(frozen=True)
class FixtureTask:
    """A deterministic task (card) for a fixture scenario.

    All timestamps and IDs are stable literals.  No live-board or
    current-time dependency.
    """

    id: str
    status: str
    title: str
    body: str
    block_reason: str
    comments: tuple[Comment, ...] = ()
    existing_idempotency_keys: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    created_at: int | None = None


@dataclass(frozen=True)
class Expect:
    """Expected outcomes for one fixture case.

    Every field maps 1:1 to the approved fixture matrix so router tests
    can assert against these directly.
    """

    verdict_value: str | None
    target_validation: str  # 'same-card' | 'cross-target' | 'missing-target' | 'not-applicable'
    scope_class: str  # 'source_docs_spec_test_only' | 'operator_gated' | 'ambiguous' | 'unknown'
    action: str  # 'complete' | 'needs_operator' | 'needs_pm' | 'unblock_rework' | 'skip'
    result: str  # 'would_complete' | 'would_comment' | 'would_unblock' | 'skipped' | 'skipped_idempotent'
    mutations: tuple[str, ...]  # planned mutation types, e.g. ('complete',) or ('comment',)
    forbid_mutations: tuple[str, ...] = ()  # mutation types that must NOT happen
    comment_prefix: str | None = None  # when set, expected comment body starts with this
    comment_contains: str | None = None  # when set, expected comment body must contain this
    router_log_contains: tuple[str, ...] = ()  # substrings expected in router log


@dataclass(frozen=True)
class VerdictFixture:
    """Complete fixture for one REVIEW_VERDICT router scenario.

    ``task`` is the primary source card.  ``extra_tasks`` are peer cards
    (e.g. for scan-continuity cases).  ``expect`` encodes what the router
    should decide for this case.
    """

    name: str
    description: str
    board: str
    task: FixtureTask
    expect: Expect
    extra_tasks: tuple[FixtureTask, ...] = ()


# ── Helper: convert fixture to dict (JSON-compatible shape) ─────────────────


def as_dict(fixture: VerdictFixture) -> dict[str, Any]:
    """Return a JSON-compatible dict matching the verdict-router.fixtures.json schema.

    This lets ``as_dict`` fixtures feed directly into the existing
    ``verdict-router-harness.py`` external-command flow without manual
    dict construction.
    """
    return {
        "name": fixture.name,
        "description": fixture.description,
        "board": fixture.board,
        "task": {
            "id": fixture.task.id,
            "status": fixture.task.status,
            "title": fixture.task.title,
            "body": fixture.task.body,
            "block_reason": fixture.task.block_reason,
            "changed_files": list(fixture.task.changed_files),
            "existing_idempotency_keys": list(fixture.task.existing_idempotency_keys),
            "comments": [
                {"id": c.id, "author": c.author, "created_at": c.created_at, "body": c.body}
                for c in fixture.task.comments
            ],
        },
        "extra_tasks": [
            {
                "id": t.id,
                "status": t.status,
                "title": t.title,
                "body": t.body,
                "block_reason": t.block_reason,
                "changed_files": list(t.changed_files),
                "existing_idempotency_keys": list(t.existing_idempotency_keys),
                "comments": [
                    {"id": c.id, "author": c.author, "created_at": c.created_at, "body": c.body}
                    for c in t.comments
                ],
            }
            for t in fixture.extra_tasks
        ],
        "expect": {
            "verdict_value": fixture.expect.verdict_value,
            "target_validation": fixture.expect.target_validation,
            "scope_class": fixture.expect.scope_class,
            "action": fixture.expect.action,
            "result": fixture.expect.result,
            "mutations": list(fixture.expect.mutations),
            "forbid_mutations": list(fixture.expect.forbid_mutations),
            "comment_prefix": fixture.expect.comment_prefix,
            "comment_contains": fixture.expect.comment_contains,
            "router_log_contains": list(fixture.expect.router_log_contains),
        },
    }


# ── Helper: list of all tasks in a fixture (source + extra) ─────────────────


def fixture_tasks(fixture: VerdictFixture) -> tuple[FixtureTask, ...]:
    """Return all tasks for a fixture: source card first, then extra tasks."""
    return (fixture.task, *fixture.extra_tasks)


# ── ALL 21 FIXTURE CASES ────────────────────────────────────────────────────

# --- 1. approved-source-card-completes ---
APPROVED_SOURCE_CARD_COMPLETES = VerdictFixture(
    name="approved-source-card-completes",
    description="Eligible source/docs/spec/test-only card with same-card APPROVED verdict plans completion.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000001",
        status="blocked",
        title="Add deterministic source-only regression fixtures",
        body="Bounded source/test-only deliverable with deterministic fixture files and local command output evidence.",
        block_reason="review-required: tests pass; needs reviewer verdict",
        comments=(
            Comment(
                id=101,
                author="os-reviewer",
                created_at=1783110001,
                body="REVIEW_VERDICT=APPROVED\nTarget: jarvis-os/t_a0000001\nFinding: source/test fixture scope only; tests pass.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="APPROVED",
        target_validation="same-card",
        scope_class="source_docs_spec_test_only",
        action="complete",
        result="would_complete",
        mutations=("complete",),
    ),
)

# --- 2. approved-runtime-a3-needs-operator ---
APPROVED_RUNTIME_A3_NEEDS_OPERATOR = VerdictFixture(
    name="approved-runtime-a3-needs-operator",
    description="APPROVED runtime/A3 card is operator-gated and must not complete.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000002",
        status="blocked",
        title="A3: enable verdict-router apply sentinel and runtime cron activation",
        body="Requires A3 approval, runtime activation, cron apply sentinel enablement, and service restart after review.",
        block_reason="review-required: proposed apply activation",
        comments=(
            Comment(
                id=102,
                author="os-reviewer",
                created_at=1783110002,
                body="REVIEW_VERDICT: APPROVED\nTarget t_a0000002\nA3 proposal is internally consistent.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="APPROVED",
        target_validation="same-card",
        scope_class="operator_gated",
        action="needs_operator",
        result="would_comment",
        mutations=("comment",),
        forbid_mutations=("complete", "unblock"),
        comment_prefix="NEEDS-OPERATOR: verdict-router operator-gated",
    ),
)

# --- 3. approved-db-live-scope-needs-operator ---
APPROVED_DB_LIVE_SCOPE_NEEDS_OPERATOR = VerdictFixture(
    name="approved-db-live-scope-needs-operator",
    description="APPROVED verdict mentioning deploy/live/DB scope fails into operator gate, no completion.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000003",
        status="blocked",
        title="Run production DB migration and live runtime deploy",
        body="Scope includes schema migration, live data write, production deploy, and gateway restart.",
        block_reason="review-required: migration plan ready",
        comments=(
            Comment(
                id=103,
                author="os-reviewer",
                created_at=1783110003,
                body="REVIEW_VERDICT=APPROVED\nTarget: t_a0000003\nMigration plan reviewed; deploy/live/DB scope remains operator gated.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="APPROVED",
        target_validation="same-card",
        scope_class="operator_gated",
        action="needs_operator",
        result="would_comment",
        mutations=("comment",),
        forbid_mutations=("complete", "unblock"),
        comment_prefix="NEEDS-OPERATOR: verdict-router operator-gated",
    ),
)

# --- 4. changes-requested-unblocks-with-quoted-finding ---
CHANGES_REQUESTED_UNBLOCKS_WITH_QUOTED_FINDING = VerdictFixture(
    name="changes-requested-unblocks-with-quoted-finding",
    description="CHANGES_REQUESTED on same card plans rework comment and unblock with quoted reviewer finding.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000004",
        status="blocked",
        title="Fix parser edge cases in source-only script",
        body="Source/test-only script work; no operator-gated scope.",
        block_reason="review-required: parser tests ready",
        comments=(
            Comment(
                id=104,
                author="os-reviewer",
                created_at=1783110004,
                body="REVIEW_VERDICT: CHANGES_REQUESTED\nBlocking finding: parser accepts APPROVED_WITH_NOTES; fail closed instead.\nTarget: t_a0000004",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="CHANGES_REQUESTED",
        target_validation="same-card",
        scope_class="source_docs_spec_test_only",
        action="unblock_rework",
        result="would_unblock",
        mutations=("comment", "unblock"),
        comment_prefix="verdict-router: REWORK_REQUIRED",
        comment_contains="parser accepts APPROVED_WITH_NOTES",
    ),
)

# --- 5. ambiguous-malformed-verdict-fails-closed ---
AMBIGUOUS_MALFORMED_VERDICT_FAILS_CLOSED = VerdictFixture(
    name="ambiguous-malformed-verdict-fails-closed",
    description="Custom/ambiguous verdict values fail closed with NEEDS-PM and no unblock/complete.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000005",
        status="blocked",
        title="Source-only cleanup",
        body="Source-only cleanup with tests.",
        block_reason="review-required",
        comments=(
            Comment(
                id=105,
                author="os-reviewer",
                created_at=1783110005,
                body="REVIEW_VERDICT=APPROVED_WITH_NOTES\nTarget: t_a0000005\nLooks okay with notes.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="APPROVED_WITH_NOTES",
        target_validation="same-card",
        scope_class="ambiguous",
        action="needs_pm",
        result="would_comment",
        mutations=("comment",),
        forbid_mutations=("complete", "unblock"),
        comment_prefix="NEEDS-PM: verdict-router fail-closed",
    ),
)

# --- 6. multiple-verdict-tokens-fail-closed ---
MULTIPLE_VERDICT_TOKENS_FAILS_CLOSED = VerdictFixture(
    name="multiple-verdict-tokens-fail-closed",
    description="A latest comment with more than one REVIEW_VERDICT token is ambiguous and must not complete or unblock.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000017",
        status="blocked",
        title="Source-only card with contradictory review comment",
        body="Source-only docs/tests update.",
        block_reason="review-required",
        comments=(
            Comment(
                id=118,
                author="os-reviewer",
                created_at=1783110018,
                body="REVIEW_VERDICT=APPROVED\nTarget: t_a0000017\nLater correction in same comment: REVIEW_VERDICT=CHANGES_REQUESTED\nBlocking finding: contradictory verdict tokens must fail closed.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value=None,
        target_validation="not-applicable",
        scope_class="ambiguous",
        action="needs_pm",
        result="would_comment",
        mutations=("comment",),
        forbid_mutations=("complete", "unblock"),
        comment_prefix="NEEDS-PM: verdict-router fail-closed",
        comment_contains="ambiguous or malformed verdict",
    ),
)

# --- 7. off-target-approved-fails-closed ---
OFF_TARGET_APPROVED_FAILS_CLOSED = VerdictFixture(
    name="off-target-approved-fails-closed",
    description="APPROVED comment naming a different task id fails closed.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000006",
        status="blocked",
        title="Source-only card awaiting review",
        body="Source-only tests.",
        block_reason="review-required",
        comments=(
            Comment(
                id=106,
                author="os-reviewer",
                created_at=1783110006,
                body="REVIEW_VERDICT: APPROVED\nTarget: t_b0000006\nApproved the other card.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="APPROVED",
        target_validation="cross-target",
        scope_class="ambiguous",
        action="needs_pm",
        result="would_comment",
        mutations=("comment",),
        forbid_mutations=("complete", "unblock"),
        comment_prefix="NEEDS-PM: verdict-router fail-closed",
    ),
)

# --- 8. non-latest-verdict-ignored ---
NON_LATEST_VERDICT_IGNORED = VerdictFixture(
    name="non-latest-verdict-ignored",
    description="Older verdict is ignored when a later non-router comment exists without a verdict.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000007",
        status="blocked",
        title="Source-only card with stale verdict",
        body="Source-only tests.",
        block_reason="review-required",
        comments=(
            Comment(
                id=107,
                author="os-reviewer",
                created_at=1783110007,
                body="REVIEW_VERDICT=APPROVED\nTarget: t_a0000007\nOlder approval.",
            ),
            Comment(
                id=108,
                author="builder",
                created_at=1783110008,
                body="Worker added more context after review; this comment is now latest and contains no verdict.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value=None,
        target_validation="not-applicable",
        scope_class="unknown",
        action="skip",
        result="skipped",
        mutations=(),
    ),
)

# --- 9. repeated-run-idempotent-skips-existing-key ---
REPEATED_RUN_IDEMPOTENT_SKIPS_EXISTING_KEY = VerdictFixture(
    name="repeated-run-idempotent-skips-existing-key",
    description="Repeated run with an existing idempotency marker skips mutation planning.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000008",
        status="blocked",
        title="Source-only card already routed once",
        body="Source-only tests.",
        block_reason="review-required",
        existing_idempotency_keys=(
            "verdict-router:v1:jarvis-os:t_a0000008:comment:109:action:complete",
        ),
        comments=(
            Comment(
                id=109,
                author="os-reviewer",
                created_at=1783110009,
                body="REVIEW_VERDICT=APPROVED\nTarget: t_a0000008\nApproved source-only scope.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="APPROVED",
        target_validation="same-card",
        scope_class="source_docs_spec_test_only",
        action="skip",
        result="skipped_idempotent",
        mutations=(),
    ),
)

# --- 10. repeated-needs-pm-idempotent-skips-existing-key ---
REPEATED_NEEDS_PM_IDEMPOTENT_SKIPS_EXISTING_KEY = VerdictFixture(
    name="repeated-needs-pm-idempotent-skips-existing-key",
    description="Repeated fail-closed PM comment with an existing idempotency marker skips duplicate comment planning.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000009",
        status="blocked",
        title="Source-only card already PM-routed once",
        body="Source-only tests.",
        block_reason="review-required",
        existing_idempotency_keys=(
            "verdict-router:v1:jarvis-os:t_a0000009:comment:111:action:needs_pm",
        ),
        comments=(
            Comment(
                id=111,
                author="os-reviewer",
                created_at=1783110011,
                body="REVIEW_VERDICT=APPROVED\nTarget: t_b0000009\nApproved a different source card.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="APPROVED",
        target_validation="cross-target",
        scope_class="ambiguous",
        action="skip",
        result="skipped_idempotent",
        mutations=(),
    ),
)

# --- 11. repeated-needs-operator-idempotent-skips-existing-key ---
REPEATED_NEEDS_OPERATOR_IDEMPOTENT_SKIPS_EXISTING_KEY = VerdictFixture(
    name="repeated-needs-operator-idempotent-skips-existing-key",
    description="Repeated operator-gated fail-closed comment with an existing idempotency marker skips duplicate comment planning.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000010",
        status="blocked",
        title="A3 production deploy already operator-routed once",
        body="Requires A3 approval, production deploy, live runtime activation, and gateway restart.",
        block_reason="review-required: operator gate needed",
        existing_idempotency_keys=(
            "verdict-router:v1:jarvis-os:t_a0000010:comment:112:action:needs_operator",
        ),
        comments=(
            Comment(
                id=112,
                author="os-reviewer",
                created_at=1783110012,
                body="REVIEW_VERDICT=APPROVED\nTarget: t_a0000010\nA3 deploy plan is approved for operator consideration.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="APPROVED",
        target_validation="same-card",
        scope_class="operator_gated",
        action="skip",
        result="skipped_idempotent",
        mutations=(),
    ),
)

# --- 12. repeated-unblock-rework-idempotent-skips-existing-key (NEW) ---
REPEATED_UNBLOCK_REWORK_IDEMPOTENT_SKIPS_EXISTING_KEY = VerdictFixture(
    name="repeated-unblock-rework-idempotent-skips-existing-key",
    description="Repeated CHANGES_REQUESTED unblock_rework with existing idempotency marker skips duplicate unblock planning.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000023",
        status="blocked",
        title="Source-only card already rework-routed once",
        body="Source-only tests.",
        block_reason="review-required",
        existing_idempotency_keys=(
            "verdict-router:v1:jarvis-os:t_a0000023:comment:123:action:unblock_rework",
        ),
        comments=(
            Comment(
                id=123,
                author="os-reviewer",
                created_at=1783110023,
                body="REVIEW_VERDICT=CHANGES_REQUESTED\nTarget: t_a0000023\nBlocking finding: rework idempotency test needs simulated edge condition.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="CHANGES_REQUESTED",
        target_validation="same-card",
        scope_class="source_docs_spec_test_only",
        action="skip",
        result="skipped_idempotent",
        mutations=(),
        forbid_mutations=("comment", "unblock"),
    ),
)

# --- 13. cross-target-operator-gated-approval-needs-operator ---
CROSS_TARGET_OPERATOR_GATED_APPROVAL_NEEDS_OPERATOR = VerdictFixture(
    name="cross-target-operator-gated-approval-needs-operator",
    description="Cross-target approval on an operator-gated deploy/DB card preserves NEEDS-OPERATOR instead of downgrading to NEEDS-PM.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000011",
        status="blocked",
        title="Run production DB migration after deploy review",
        body="Operator-gated schema migration with live data write and production deploy.",
        block_reason="review-required: DB deploy plan",
        comments=(
            Comment(
                id=113,
                author="os-reviewer",
                created_at=1783110013,
                body="REVIEW_VERDICT=APPROVED\nTarget: t_b0000011\nMigration approach approved for the other card.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="APPROVED",
        target_validation="cross-target",
        scope_class="operator_gated",
        action="needs_operator",
        result="would_comment",
        mutations=("comment",),
        forbid_mutations=("complete", "unblock"),
        comment_prefix="NEEDS-OPERATOR: verdict-router operator-gated",
    ),
)

# --- 13. approved-with-no-task-id-fails-closed ---
APPROVED_WITH_NO_TASK_ID_FAILS_CLOSED = VerdictFixture(
    name="approved-with-no-task-id-fails-closed",
    description="APPROVED with no explicit task id fails closed instead of inferring the current card.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000012",
        status="blocked",
        title="Source-only card awaiting explicit approval target",
        body="Source-only tests.",
        block_reason="review-required",
        comments=(
            Comment(
                id=114,
                author="os-reviewer",
                created_at=1783110014,
                body="REVIEW_VERDICT=APPROVED\nLooks good.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="APPROVED",
        target_validation="missing-target",
        scope_class="ambiguous",
        action="needs_pm",
        result="would_comment",
        mutations=("comment",),
        forbid_mutations=("complete", "unblock"),
        comment_prefix="NEEDS-PM: verdict-router fail-closed",
    ),
)

# --- 14. changes-requested-cross-target-fails-closed ---
CHANGES_REQUESTED_CROSS_TARGET_FAILS_CLOSED = VerdictFixture(
    name="changes-requested-cross-target-fails-closed",
    description="CHANGES_REQUESTED that mentions another task id fails closed and does not unblock the current card.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000013",
        status="blocked",
        title="Source-only current card",
        body="Source-only tests.",
        block_reason="review-required",
        comments=(
            Comment(
                id=115,
                author="os-reviewer",
                created_at=1783110015,
                body="REVIEW_VERDICT=CHANGES_REQUESTED\nTarget: t_b0000013\nBlocking finding: wrong target needs parser work.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="CHANGES_REQUESTED",
        target_validation="cross-target",
        scope_class="ambiguous",
        action="needs_pm",
        result="would_comment",
        mutations=("comment",),
        forbid_mutations=("complete", "unblock"),
        comment_prefix="NEEDS-PM: verdict-router fail-closed",
    ),
)

# --- 15. custom-changes-requested-for-fails-closed ---
CUSTOM_CHANGES_REQUESTED_FOR_FAILS_CLOSED = VerdictFixture(
    name="custom-changes-requested-for-fails-closed",
    description="Custom CHANGES_REQUESTED_FOR_* verdict values are malformed and fail closed.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000014",
        status="blocked",
        title="Source-only custom verdict card",
        body="Source-only tests.",
        block_reason="review-required",
        comments=(
            Comment(
                id=116,
                author="os-reviewer",
                created_at=1783110016,
                body="REVIEW_VERDICT=CHANGES_REQUESTED_FOR_DOCS\nTarget: t_a0000014\nBlocking finding: custom verdict must not be accepted.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="CHANGES_REQUESTED_FOR_DOCS",
        target_validation="same-card",
        scope_class="ambiguous",
        action="needs_pm",
        result="would_comment",
        mutations=("comment",),
        forbid_mutations=("complete", "unblock"),
        comment_prefix="NEEDS-PM: verdict-router fail-closed",
    ),
)

# --- 16. frontend-app-without-verify-pass-needs-pm ---
FRONTEND_APP_WITHOUT_VERIFY_PASS_NEEDS_PM = VerdictFixture(
    name="frontend-app-without-verify-pass-needs-pm",
    description="True frontend/app work without running-app VERIFY_PASS is not auto-completed by APPROVED alone.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000015",
        status="blocked",
        title="Fix apps/web marketplace page component",
        body="Changed files include apps/web/app/marketplace/page.tsx and packages/ui/card.tsx. Review says code is approved but running-app VERIFY_PASS is missing.",
        block_reason="review-required: no running-app VERIFY_PASS evidence yet",
        changed_files=(
            "apps/web/app/marketplace/page.tsx",
            "packages/ui/card.tsx",
        ),
        comments=(
            Comment(
                id=117,
                author="os-reviewer",
                created_at=1783110017,
                body="REVIEW_VERDICT=APPROVED\nTarget: t_a0000015\nCode review approved, but no VERIFY_PASS output is present.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="APPROVED",
        target_validation="same-card",
        scope_class="ambiguous",
        action="needs_pm",
        result="would_comment",
        mutations=("comment",),
        forbid_mutations=("complete", "unblock"),
        comment_prefix="NEEDS-PM: verdict-router fail-closed",
        comment_contains="frontend/app work without VERIFY_PASS",
    ),
)

# --- 17. changes-requested-operator-gated-needs-operator ---
CHANGES_REQUESTED_OPERATOR_GATED_NEEDS_OPERATOR = VerdictFixture(
    name="changes-requested-operator-gated-needs-operator",
    description="CHANGES_REQUESTED on a deploy/DB/live card is unsafe for deterministic unblock and remains blocked for operator routing.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000018",
        status="blocked",
        title="Fix live DB migration rollback packet",
        body="Operator-gated schema migration and live data rollback packet.",
        block_reason="review-required: DB migration packet",
        comments=(
            Comment(
                id=119,
                author="os-reviewer",
                created_at=1783110019,
                body="REVIEW_VERDICT=CHANGES_REQUESTED\nTarget: t_a0000018\nBlocking finding: live DB migration packet is missing rollback verification.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="CHANGES_REQUESTED",
        target_validation="same-card",
        scope_class="operator_gated",
        action="needs_operator",
        result="would_comment",
        mutations=("comment",),
        forbid_mutations=("complete", "unblock"),
        comment_prefix="NEEDS-OPERATOR: verdict-router operator-gated",
    ),
)

# --- 18. router-authored-verdict-value-comment-ignored ---
ROUTER_AUTHORED_VERDICT_VALUE_COMMENT_IGNORED = VerdictFixture(
    name="router-authored-verdict-value-comment-ignored",
    description="Router-authored comments use verdict_value and are ignored as verdict sources.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000016",
        status="blocked",
        title="Source-only card with router echo",
        body="Source-only tests.",
        block_reason="review-required",
        comments=(
            Comment(
                id=110,
                author="verdict-router",
                created_at=1783110010,
                body="NEEDS-PM: verdict-router fail-closed\nverdict_value=APPROVED idempotency_key=verdict-router:v1:jarvis-os:t_a0000016:comment:999:action:needs_pm",
            ),
        ),
    ),
    expect=Expect(
        verdict_value=None,
        target_validation="not-applicable",
        scope_class="unknown",
        action="skip",
        result="skipped",
        mutations=(),
    ),
)

# --- 19. nonnumeric-older-created-at-does-not-outrank-newer-numeric-verdict ---
NONNUMERIC_OLDER_CREATED_AT_DOES_NOT_OUTRANK = VerdictFixture(
    name="nonnumeric-older-created-at-does-not-outrank-newer-numeric-verdict",
    description="A corrupt older task_comments.created_at literal must sort as 0 so a newer numeric reviewer verdict wins latest-comment selection.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000019",
        status="blocked",
        title="Source-only card with corrupt comment timestamp",
        body="Source-only tests and patch evidence.",
        block_reason="review-required",
        comments=(
            Comment(
                id=120,
                author="os-reviewer",
                created_at="%s",
                body="REVIEW_VERDICT=CHANGES_REQUESTED\nTarget: t_a0000019\nBlocking finding: stale corrupt timestamp verdict must not outrank the newer numeric approval.",
            ),
            Comment(
                id=121,
                author="os-reviewer",
                created_at=1783110020,
                body="REVIEW_VERDICT=APPROVED\nTarget: t_a0000019\nSource patch approved by the newer numeric timestamp.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="APPROVED",
        target_validation="same-card",
        scope_class="source_docs_spec_test_only",
        action="complete",
        result="would_complete",
        mutations=("complete",),
    ),
)

# --- 20. nonnumeric-comment-id-percent-s-is-skipped-with-log ---
NONNUMERIC_COMMENT_ID_PERCENT_S_IS_SKIPPED = VerdictFixture(
    name="nonnumeric-comment-id-percent-s-is-skipped-with-log",
    description="A corrupt task_comments.id literal '%s' must be skipped with a log entry and must not abort the scan.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000020",
        status="blocked",
        title="Source-only card with corrupt comment id",
        body="Source-only tests and patch evidence.",
        block_reason="review-required",
        comments=(
            Comment(
                id="%s",
                author="os-reviewer",
                created_at=1783110020,
                body="REVIEW_VERDICT=APPROVED\nTarget: t_a0000020\nThis corrupt comment id must be skipped instead of crashing.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value=None,
        target_validation="not-applicable",
        scope_class="unknown",
        action="skip",
        result="skipped",
        mutations=(),
        router_log_contains=(
            "skip-nonnumeric-int context=jarvis-os/t_a0000020:task_comments.id value='%s'",
        ),
    ),
)

# --- 21. scan-continues-after-nonnumeric-comment-id ---
SCAN_CONTINUES_AFTER_NONNUMERIC_COMMENT_ID = VerdictFixture(
    name="scan-continues-after-nonnumeric-comment-id",
    description="A corrupt '%s' comment id on one task must not prevent a later valid numeric comment_id from being parsed and processed.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_a0000021",
        status="blocked",
        title="Source-only card after corrupt peer",
        body="Source-only tests and patch evidence.",
        block_reason="review-required",
        created_at=1783110021,
        comments=(
            Comment(
                id=122,
                author="os-reviewer",
                created_at=1783110021,
                body="REVIEW_VERDICT=APPROVED\nTarget: t_a0000021\nValid numeric comment id must still be routed.",
            ),
        ),
    ),
    extra_tasks=(
        FixtureTask(
            id="t_a0000022",
            status="blocked",
            title="Source-only corrupt peer scanned first",
            body="Source-only tests and patch evidence.",
            block_reason="review-required",
            created_at=1783110019,
            comments=(
                Comment(
                    id="%s",
                    author="os-reviewer",
                    created_at=1783110019,
                    body="REVIEW_VERDICT=APPROVED\nTarget: t_a0000022\nCorrupt peer must be skipped so scan reaches t_a0000021.",
                ),
            ),
        ),
    ),
    expect=Expect(
        verdict_value="APPROVED",
        target_validation="same-card",
        scope_class="source_docs_spec_test_only",
        action="complete",
        result="would_complete",
        mutations=("complete",),
        router_log_contains=(
            "skip-nonnumeric-int context=jarvis-os/t_a0000022:task_comments.id value='%s'",
        ),
    ),
)

# --- 22. c1_gate_denial_reviewer_prose_approved_completes ---
C1_GATE_DENIAL_REVIEWER_PROSE_APPROVED_COMPLETES = VerdictFixture(
    name="c1-gate-denial-reviewer-prose-approved-completes",
    description="C1 (t_8874b97b / t_9a0af491): an APPROVED card whose reviewer comment *denies* operator gates ('A3 gates intact; no credential, prod, or DB change; REVIEW_VERDICT=APPROVED') on a SOURCE/TEST-only card MUST complete through the router, and the gate-denial nouns in reviewer prose must not operator-gate the card.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_c1a0b1e2",
        status="blocked",
        title="Add unit tests for tenant id injection",
        body="Source/test-only change. Tenant coverage.",
        block_reason="review-required: tests pass; needs reviewer verdict",
        comments=(
            Comment(
                id=124,
                author="os-reviewer",
                created_at=1783110024,
                body="REVIEW_VERDICT=APPROVED\nA3 gates intact; no credential, prod, or DB change;\nTarget: t_c1a0b1e2. Source/test-only, approvable.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="APPROVED",
        target_validation="same-card",
        scope_class="source_docs_spec_test_only",
        action="complete",
        result="would_complete",
        mutations=("complete",),
    ),
)

# --- 23. c3_genuine_prod_db_credential_operator_gated_needs_operator ---
C3_GENUINE_PROD_DB_CREDENTIAL_OPERATOR_GATED_NEEDS_OPERATOR = VerdictFixture(
    name="c3-genuine-prod-db-credential-operator-gated-needs-operator",
    description="C3 (t_8874b97b / t_9a0af491): a card whose task title/body genuinely indicates deploy, DB, credential, A3, or operator-gated scope MUST still emit needs_operator and NOT auto-complete — even if the reviewer comment also says 'approved'.",
    board="jarvis-os",
    task=FixtureTask(
        id="t_c3operator1",
        status="blocked",
        title="Run production DB migration and live runtime deploy",
        body="Scope includes schema migration, live data write, production deploy, and gateway restart.",
        block_reason="review-required: migration plan ready for operator gate",
        comments=(
            Comment(
                id=125,
                author="os-reviewer",
                created_at=1783110025,
                body="REVIEW_VERDICT=APPROVED\nTarget: t_c3operator1\nMigration plan reviewed; deploy/live/DB scope remains operator gated, do not auto-complete.",
            ),
        ),
    ),
    expect=Expect(
        verdict_value="APPROVED",
        target_validation="same-card",
        scope_class="operator_gated",
        action="needs_operator",
        result="would_comment",
        mutations=("comment",),
        forbid_mutations=("complete", "unblock"),
        comment_prefix="NEEDS-OPERATOR: verdict-router operator-gated",
    ),
)

# ── Aggregate exports ───────────────────────────────────────────────────────

# All 24 cases in order — useful for iterating in parametrized tests
ALL_CASES: tuple[VerdictFixture, ...] = (
    APPROVED_SOURCE_CARD_COMPLETES,
    APPROVED_RUNTIME_A3_NEEDS_OPERATOR,
    APPROVED_DB_LIVE_SCOPE_NEEDS_OPERATOR,
    CHANGES_REQUESTED_UNBLOCKS_WITH_QUOTED_FINDING,
    AMBIGUOUS_MALFORMED_VERDICT_FAILS_CLOSED,
    MULTIPLE_VERDICT_TOKENS_FAILS_CLOSED,
    OFF_TARGET_APPROVED_FAILS_CLOSED,
    NON_LATEST_VERDICT_IGNORED,
    REPEATED_RUN_IDEMPOTENT_SKIPS_EXISTING_KEY,
    REPEATED_NEEDS_PM_IDEMPOTENT_SKIPS_EXISTING_KEY,
    REPEATED_NEEDS_OPERATOR_IDEMPOTENT_SKIPS_EXISTING_KEY,
    REPEATED_UNBLOCK_REWORK_IDEMPOTENT_SKIPS_EXISTING_KEY,
    CROSS_TARGET_OPERATOR_GATED_APPROVAL_NEEDS_OPERATOR,
    APPROVED_WITH_NO_TASK_ID_FAILS_CLOSED,
    CHANGES_REQUESTED_CROSS_TARGET_FAILS_CLOSED,
    CUSTOM_CHANGES_REQUESTED_FOR_FAILS_CLOSED,
    FRONTEND_APP_WITHOUT_VERIFY_PASS_NEEDS_PM,
    CHANGES_REQUESTED_OPERATOR_GATED_NEEDS_OPERATOR,
    ROUTER_AUTHORED_VERDICT_VALUE_COMMENT_IGNORED,
    NONNUMERIC_OLDER_CREATED_AT_DOES_NOT_OUTRANK,
    NONNUMERIC_COMMENT_ID_PERCENT_S_IS_SKIPPED,
    SCAN_CONTINUES_AFTER_NONNUMERIC_COMMENT_ID,
    C1_GATE_DENIAL_REVIEWER_PROSE_APPROVED_COMPLETES,
    C3_GENUINE_PROD_DB_CREDENTIAL_OPERATOR_GATED_NEEDS_OPERATOR,
)

# Map name → fixture for lookup
ALL_CASES_BY_NAME: dict[str, VerdictFixture] = {
    f.name: f for f in ALL_CASES
}

assert len(ALL_CASES) == 24, f"Expected 24 fixture cases, got {len(ALL_CASES)}"
