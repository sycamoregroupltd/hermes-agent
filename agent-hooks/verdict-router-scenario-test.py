#!/usr/bin/env python3
"""Scenario-focused REVIEW_VERDICT parsing, safety classification, and action planning tests.

Uses the importable fixture dataclasses and the isolated harness API to exercise
every scenario group from the fixture matrix. Every scenario asserts:
  - Parsed verdict value & token count
  - Target validation
  - Safety classification (scope_class)
  - Planned action & result
  - Planned mutations (and forbidden mutations)
  - Expected comment prefix/contains
  - Absence/presence of completion/block/unblock/comment behavior

This complements the already-excellent verdict-router-harness-modes-test.py
(generic 21-fixture parametrized tests) by providing NAMED per-scenario tests
with explicit assertions tied to business requirements.

Run standalone:
    python3 agent-hooks/verdict-router-scenario-test.py

Run as part of full selftest suite:
    bash agent-hooks/verdict-router.selftest.sh   # verdict-router suite only
    bash agent-hooks/run-selftests.sh              # umbrella runner

Requires no live board credentials or network access.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import asdict as dataclass_asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = ROOT / "agent-hooks" / "verdict-router-harness.py"
FIXTURES_MODULE_PATH = ROOT / "agent-hooks" / "verdict_router_fixtures.py"


def load_harness():
    spec = importlib.util.spec_from_file_location("verdict_router_harness", HARNESS_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load harness from {HARNESS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_fixtures():
    spec = importlib.util.spec_from_file_location(
        "verdict_router_fixtures", FIXTURES_MODULE_PATH
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load fixtures from {FIXTURES_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture_to_task_dict(fxt: Any) -> dict[str, Any]:
    """Convert a FixtureTask dataclass to the dict shape run_harness expects."""
    d = dataclass_asdict(fxt)
    d["comments"] = [
        dict(id=c["id"], author=c["author"], created_at=c["created_at"], body=c["body"])
        for c in d.pop("comments", ())
    ]
    d["existing_idempotency_keys"] = list(d.pop("existing_idempotency_keys", ()))
    d["changed_files"] = list(d.pop("changed_files", ()))
    return d


def _make_fixture(harness: Any, fxt: Any, mode: str = "dry-run") -> dict[str, Any]:
    """Run the harness for one VerdictFixture dataclass and return the structured result."""
    task_dict = _fixture_to_task_dict(fxt.task)
    result = harness.run_harness(
        board=str(fxt.board),
        task=task_dict,
        mode=mode,
    )
    return result


def _get_item(result: dict[str, Any]) -> dict[str, Any]:
    """Extract the single result item from a harness result."""
    items = result.get("results", [])
    assert len(items) == 1, f"expected 1 result item, got {len(items)}"
    return items[0]


# ── Assertion helpers ───────────────────────────────────────────────────


def assert_parsed_verdict(item: dict[str, Any], expect: dict[str, Any], tag: str = "") -> list[str]:
    """Assert parsed verdict fields match expectations."""
    errors: list[str] = []
    pv = item.get("parsed_verdict", {})
    prefix = f"[{tag}] " if tag else ""

    expected_value = expect.get("verdict_value")
    if pv.get("value") != expected_value:
        errors.append(f"{prefix}parsed_verdict.value: expected {expected_value!r}, got {pv.get('value')!r}")

    expected_target = expect.get("target_validation")
    if pv.get("target_validation") != expected_target:
        errors.append(f"{prefix}target_validation: expected {expected_target!r}, got {pv.get('target_validation')!r}")

    return errors


def assert_safety_classification(item: dict[str, Any], expect: dict[str, Any], tag: str = "") -> list[str]:
    """Assert safety classification matches expectations."""
    errors: list[str] = []
    prefix = f"[{tag}] " if tag else ""
    expected = expect.get("scope_class")
    if item.get("safety_classification") != expected:
        errors.append(f"{prefix}safety_classification: expected {expected!r}, got {item.get('safety_classification')!r}")
    return errors


def assert_action_and_result(item: dict[str, Any], expect: dict[str, Any], tag: str = "") -> list[str]:
    """Assert planned action and result match expectations."""
    errors: list[str] = []
    plan = item.get("plan", {})
    prefix = f"[{tag}] " if tag else ""

    expected_action = expect.get("action")
    if plan.get("action") != expected_action:
        errors.append(f"{prefix}action: expected {expected_action!r}, got {plan.get('action')!r}")

    expected_result = expect.get("result")
    if plan.get("result") != expected_result:
        errors.append(f"{prefix}result: expected {expected_result!r}, got {plan.get('result')!r}")

    return errors


def assert_mutations(item: dict[str, Any], expect: dict[str, Any], tag: str = "") -> list[str]:
    """Assert planned mutations and forbidden mutations match expectations."""
    errors: list[str] = []
    planned = list(item.get("planned_mutations", []))
    prefix = f"[{tag}] " if tag else ""

    expected_mutations = list(expect.get("mutations", []))
    if planned != expected_mutations:
        errors.append(f"{prefix}planned_mutations: expected {expected_mutations!r}, got {planned!r}")

    for forbidden in expect.get("forbid_mutations", []):
        if forbidden in planned:
            errors.append(f"{prefix}forbidden mutation planned: {forbidden}")

    return errors


def assert_comment(item: dict[str, Any], expect: dict[str, Any], tag: str = "") -> list[str]:
    """Assert comment content matches expectations."""
    errors: list[str] = []
    prefix = f"[{tag}] " if tag else ""

    comments = item.get("comments", [])
    expected_prefix = expect.get("comment_prefix")
    expected_contains = expect.get("comment_contains")

    if expected_prefix is not None:
        if not comments:
            errors.append(f"{prefix}expected comment with prefix {expected_prefix!r}, got none")
        else:
            bodies = [str(c.get("body", "")) for c in comments]
            all_body = "\n".join(bodies)
            if not bodies[0].startswith(expected_prefix):
                errors.append(f"{prefix}comment prefix: expected {expected_prefix!r}, got {bodies[0][:100]!r}")
            if expected_contains and expected_contains not in all_body:
                errors.append(f"{prefix}comment missing expected excerpt {expected_contains!r}")

    return errors


def assert_completion_behavior(item: dict[str, Any], expect: dict[str, Any], tag: str = "") -> list[str]:
    """Assert completion/unblock/comment behavior matches expectations."""
    errors: list[str] = []
    prefix = f"[{tag}] " if tag else ""

    expected_action = expect.get("action")
    expected_mutations = expect.get("mutations", [])

    if expected_action == "complete":
        # Must have completion_actions, NO comments, NO unblock_actions
        if not item.get("completion_actions"):
            errors.append(f"{prefix}expected non-empty completion_actions for 'complete' action")
        for ca in item.get("completion_actions", []):
            if not ca.get("idempotency_key"):
                errors.append(f"{prefix}completion_actions missing idempotency_key")

    elif expected_action == "unblock_rework":
        # Must have BOTH comments AND unblock_actions
        if not item.get("comments"):
            errors.append(f"{prefix}expected non-empty comments for 'unblock_rework' action")
        if not item.get("unblock_actions"):
            errors.append(f"{prefix}expected non-empty unblock_actions for 'unblock_rework' action")
        if item.get("completion_actions"):
            errors.append(f"{prefix}expected empty completion_actions for 'unblock_rework' action")

    elif expected_action in ("needs_pm", "needs_operator"):
        # Must have comment, NO completion_actions, NO unblock_actions
        if not item.get("comments"):
            errors.append(f"{prefix}expected non-empty comments for '{expected_action}' action")
        if item.get("completion_actions"):
            errors.append(f"{prefix}expected empty completion_actions for '{expected_action}' action")
        if item.get("unblock_actions"):
            errors.append(f"{prefix}expected empty unblock_actions for '{expected_action}' action")

    elif expected_action == "skip" or expected_action is None:
        # Must have NO comments, NO completion_actions, NO unblock_actions
        if item.get("comments"):
            errors.append(f"{prefix}expected empty comments for 'skip' action")
        if item.get("completion_actions"):
            errors.append(f"{prefix}expected empty completion_actions for 'skip' action")
        if item.get("unblock_actions"):
            errors.append(f"{prefix}expected empty unblock_actions for 'skip' action")
        if not item.get("ignored_noop_results"):
            errors.append(f"{prefix}expected non-empty ignored_noop_results for 'skip' action")

    # Router comment must not contain parseable REVIEW_VERDICT token
    for comment in item.get("comments", []):
        body = str(comment.get("body", ""))
        if "REVIEW_VERDICT" in body and "=" not in body:
            pass  # verdict_value= is okay (not a parseable REVIEW_VERDICT= or REVIEW_VERDICT:)
        elif "REVIEW_VERDICT" in body and "verdict_value=" not in body:
            errors.append(f"{prefix}router comment contains parseable REVIEW_VERDICT token")

    # Idempotency key required for non-skip actions
    plan = item.get("plan", {})
    if expected_action not in ("skip", None) and not plan.get("idempotency_key"):
        errors.append(f"{prefix}non-skip plan missing idempotency_key")

    return errors


def run_scenario(
    harness: Any,
    fixture: Any,
    name: str,
    tag: str = "",
) -> list[str]:
    """Run one fixture through the harness and assert ALL acceptance dimensions.

    Returns a list of assertion error strings (empty = all pass).
    """
    expect = fixture.expect
    result = _make_fixture(harness, fixture, mode="dry-run")
    item = _get_item(result)

    errors: list[str] = []
    errors.extend(assert_parsed_verdict(item, dataclass_asdict(expect), tag=tag))
    errors.extend(assert_safety_classification(item, dataclass_asdict(expect), tag=tag))
    errors.extend(assert_action_and_result(item, dataclass_asdict(expect), tag=tag))
    errors.extend(assert_mutations(item, dataclass_asdict(expect), tag=tag))
    errors.extend(assert_comment(item, dataclass_asdict(expect), tag=tag))
    errors.extend(assert_completion_behavior(item, dataclass_asdict(expect), tag=tag))
    return errors


# ── Main test runner ──────────────────────────────────────────────────────


def main() -> int:
    harness = load_harness()
    fx_mod = load_fixtures()

    passed = 0
    total = 0
    failures: list[tuple[str, list[str]]] = []

    def check(scenario_name: str, errors: list[str]) -> None:
        nonlocal total, passed
        total += 1
        if not errors:
            passed += 1
            print(f"  PASS {scenario_name}")
        else:
            failures.append((scenario_name, errors))
            print(f"  FAIL {scenario_name}")
            for err in errors:
                print(f"       {err}")

    # ══════════════════════════════════════════════════════════════════════
    # Scenario Group A: APPROVED source-card completion
    # Acceptance: eligible APPROVED source card completes
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("GROUP A: APPROVED source-card completion")
    print("Acceptance: eligible APPROVED source/docs/spec/test-only card completes")
    print("=" * 72)

    errors = run_scenario(
        harness, fx_mod.APPROVED_SOURCE_CARD_COMPLETES,
        "A1: approved-source-card-completes",
        tag="A1",
    )
    check("A1: approved-source-card-completes", errors)

    # ══════════════════════════════════════════════════════════════════════
    # Scenario Group B: APPROVED deploy/runtime/A3 → NEEDS-OPERATOR
    # Acceptance: APPROVED on deploy/runtime/A3 card gets NEEDS-OPERATOR
    #   and does NOT complete
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("GROUP B: APPROVED deploy/runtime/A3 → NEEDS-OPERATOR")
    print("Acceptance: APPROVED on operator-gated card gets NEEDS-OPERATOR and does NOT complete")
    print("=" * 72)

    errors = run_scenario(
        harness, fx_mod.APPROVED_RUNTIME_A3_NEEDS_OPERATOR,
        "B1: approved-runtime-a3-needs-operator",
        tag="B1",
    )
    check("B1: approved-runtime-a3-needs-operator", errors)

    # ══════════════════════════════════════════════════════════════════════
    # Scenario Group C: APPROVED deploy/live/DB scope is blocked
    # Acceptance: APPROVED verdict mentioning deploy/live/DB scope is
    #   operator-gated; must NOT complete
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("GROUP C: APPROVED deploy/live/DB scope is blocked")
    print("Acceptance: deploy/live/DB content → operator-gated → MUST NOT complete")
    print("=" * 72)

    errors = run_scenario(
        harness, fx_mod.APPROVED_DB_LIVE_SCOPE_NEEDS_OPERATOR,
        "C1: approved-db-live-scope-needs-operator",
        tag="C1",
    )
    check("C1: approved-db-live-scope-needs-operator", errors)

    # ══════════════════════════════════════════════════════════════════════
    # Scenario Group C1: Reviewer gate-denial prose does not strand APPROVED
    # Acceptance: reviewer comment denying A3/credential/prod/DB gates on a
    #   source/docs/spec/test-only card MUST complete (not operator_gated).
    # ══════════════════════════════════════════════════════════════════════

    errors = run_scenario(
        harness, fx_mod.C1_GATE_DENIAL_REVIEWER_PROSE_APPROVED_COMPLETES,
        "C1(harness): c1-gate-denial-reviewer-prose-approved-completes",
        tag="C1",
    )
    check("C1(harness): c1-gate-denial-reviewer-prose-approved-completes", errors)

    # ══════════════════════════════════════════════════════════════════════
    # Scenario Group D: CHANGES_REQUESTED unblocks with quoted finding
    # Acceptance: CHANGES_REQUESTED on same-card source/docs/test returns source
    #   worker to rework: unblock + comment containing the reviewer's finding.
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("GROUP D: CHANGES_REQUESTED unblocks with quoted finding")
    print("Acceptance: unblock_rework + comment containing reviewer's quoted finding")
    print("=" * 72)

    errors = run_scenario(
        harness, fx_mod.CHANGES_REQUESTED_UNBLOCKS_WITH_QUOTED_FINDING,
        "D1: changes-requested-unblocks-with-quoted-finding",
        tag="D1",
    )
    check("D1: changes-requested-unblocks-with-quoted-finding", errors)

    # ══════════════════════════════════════════════════════════════════════
    # Scenario Group E: Ambiguous/malformed verdict fails closed
    # Acceptance: custom/ambiguous/unknown verdict values fail closed —
    #   no complete, no unblock, only a NEEDS-PM comment.
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("GROUP E: Ambiguous/malformed verdict fails closed")
    print("Acceptance: ambiguous/malformed token → needs_pm → no complete/unblock")
    print("=" * 72)

    errors = run_scenario(
        harness, fx_mod.AMBIGUOUS_MALFORMED_VERDICT_FAILS_CLOSED,
        "E1: ambiguous-malformed-verdict-fails-closed (APPROVED_WITH_NOTES)",
        tag="E1",
    )
    check("E1: ambiguous-malformed-verdict-fails-closed (APPROVED_WITH_NOTES)", errors)

    errors = run_scenario(
        harness, fx_mod.MULTIPLE_VERDICT_TOKENS_FAILS_CLOSED,
        "E2: multiple-verdict-tokens-fails-closed (APPROVED + CHANGES_REQUESTED)",
        tag="E2",
    )
    check("E2: multiple-verdict-tokens-fails-closed (APPROVED + CHANGES_REQUESTED)", errors)

    errors = run_scenario(
        harness, fx_mod.CUSTOM_CHANGES_REQUESTED_FOR_FAILS_CLOSED,
        "E3: custom-changes-requested-for-fails-closed (CHANGES_REQUESTED_FOR_DOCS)",
        tag="E3",
    )
    check("E3: custom-changes-requested-for-fails-closed (CHANGES_REQUESTED_FOR_DOCS)", errors)

    # ══════════════════════════════════════════════════════════════════════
    # Scenario Group F: Off-target verdict fails closed
    # Acceptance: a verdict naming a different task id must fail closed
    #   and not affect the current card.
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("GROUP F: Off-target verdict fails closed")
    print("Acceptance: cross-target verdict → needs_pm → no complete/unblock")
    print("=" * 72)

    errors = run_scenario(
        harness, fx_mod.OFF_TARGET_APPROVED_FAILS_CLOSED,
        "F1: off-target-approved-fails-closed",
        tag="F1",
    )
    check("F1: off-target-approved-fails-closed", errors)

    errors = run_scenario(
        harness, fx_mod.CHANGES_REQUESTED_CROSS_TARGET_FAILS_CLOSED,
        "F2: changes-requested-cross-target-fails-closed",
        tag="F2",
    )
    check("F2: changes-requested-cross-target-fails-closed", errors)

    errors = run_scenario(
        harness, fx_mod.APPROVED_WITH_NO_TASK_ID_FAILS_CLOSED,
        "F3: approved-with-no-task-id-fails-closed",
        tag="F3",
    )
    check("F3: approved-with-no-task-id-fails-closed", errors)

    # ══════════════════════════════════════════════════════════════════════
    # Scenario Group G: Non-latest verdict is ignored
    # Acceptance: an older verdict overridden by a later non-verdict comment
    #   must be silently skipped (no action).
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("GROUP G: Non-latest verdict is ignored")
    print("Acceptance: stale/overridden verdict → skip → no mutations")
    print("=" * 72)

    errors = run_scenario(
        harness, fx_mod.NON_LATEST_VERDICT_IGNORED,
        "G1: non-latest-verdict-ignored",
        tag="G1",
    )
    check("G1: non-latest-verdict-ignored", errors)

    errors = run_scenario(
        harness, fx_mod.ROUTER_AUTHORED_VERDICT_VALUE_COMMENT_IGNORED,
        "G2: router-authored-verdict-value-comment-ignored",
        tag="G2",
    )
    check("G2: router-authored-verdict-value-comment-ignored", errors)

    # ══════════════════════════════════════════════════════════════════════
    # Scenario Group H: Idempotent repeated runs
    # Acceptance: repeated run with existing idempotency key → skip,
    #   no duplicate mutations.
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("GROUP H: Idempotent repeated runs")
    print("Acceptance: existing idempotency key → skip/noop → no duplicate mutations")
    print("=" * 72)

    errors = run_scenario(
        harness, fx_mod.REPEATED_RUN_IDEMPOTENT_SKIPS_EXISTING_KEY,
        "H1: repeated-run-idempotent-skips-existing-key (complete)",
        tag="H1",
    )
    check("H1: repeated-run-idempotent-skips-existing-key (complete)", errors)

    errors = run_scenario(
        harness, fx_mod.REPEATED_NEEDS_PM_IDEMPOTENT_SKIPS_EXISTING_KEY,
        "H2: repeated-needs-pm-idempotent-skips-existing-key (needs_pm)",
        tag="H2",
    )
    check("H2: repeated-needs-pm-idempotent-skips-existing-key (needs_pm)", errors)

    errors = run_scenario(
        harness, fx_mod.REPEATED_NEEDS_OPERATOR_IDEMPOTENT_SKIPS_EXISTING_KEY,
        "H3: repeated-needs-operator-idempotent-skips-existing-key (needs_operator)",
        tag="H3",
    )
    check("H3: repeated-needs-operator-idempotent-skips-existing-key (needs_operator)", errors)

    errors = run_scenario(
        harness, fx_mod.REPEATED_UNBLOCK_REWORK_IDEMPOTENT_SKIPS_EXISTING_KEY,
        "H4: repeated-unblock-rework-idempotent-skips-existing-key (unblock_rework)",
        tag="H4",
    )
    check("H4: repeated-unblock-rework-idempotent-skips-existing-key (unblock_rework)", errors)

    # ══════════════════════════════════════════════════════════════════════
    # Scenario Group I: Cross-target operator-gated edge case
    # Acceptance: cross-target APPROVED on an operator-gated card still
    #   routes to NEEDS-OPERATOR (not downgraded to NEEDS-PM).
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("GROUP I: Cross-target + operator-gated edge case")
    print("Acceptance: cross-target approval on operator-gated card → NEEDS-OPERATOR")
    print("=" * 72)

    errors = run_scenario(
        harness, fx_mod.CROSS_TARGET_OPERATOR_GATED_APPROVAL_NEEDS_OPERATOR,
        "I1: cross-target-operator-gated-approval-needs-operator",
        tag="I1",
    )
    check("I1: cross-target-operator-gated-approval-needs-operator", errors)

    # ══════════════════════════════════════════════════════════════════════
    # Scenario Group J: Frontend app without VERIFY_PASS
    # Acceptance: APPOVED on frontend/app work without running-app VERIFY_PASS
    #   → needs_pm (auto-complete is blocked).
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("GROUP J: Frontend/app without VERIFY_PASS")
    print("Acceptance: frontend/app work without VERIFY_PASS → needs_pm")
    print("=" * 72)

    errors = run_scenario(
        harness, fx_mod.FRONTEND_APP_WITHOUT_VERIFY_PASS_NEEDS_PM,
        "J1: frontend-app-without-verify-pass-needs-pm",
        tag="J1",
    )
    check("J1: frontend-app-without-verify-pass-needs-pm", errors)

    # ══════════════════════════════════════════════════════════════════════
    # Scenario Group K: CHANGES_REQUESTED on operator-gated card
    # Acceptance: CHANGES_REQUESTED on a deploy/DB/live card → needs_operator
    #   (not unblock_rework), because deterministic unblock is unsafe.
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("GROUP K: CHANGES_REQUESTED on operator-gated card")
    print("Acceptance: CR on deploy/DB/live → needs_operator NOT unblock_rework")
    print("=" * 72)

    errors = run_scenario(
        harness, fx_mod.CHANGES_REQUESTED_OPERATOR_GATED_NEEDS_OPERATOR,
        "K1: changes-requested-operator-gated-needs-operator",
        tag="K1",
    )
    check("K1: changes-requested-operator-gated-needs-operator", errors)

    # ══════════════════════════════════════════════════════════════════════
    # Scenario Group L: Corrupt comment handling (non-numeric)
    # Acceptance: corrupt comment id '%s' is skipped without crashing;
    #   corrupt timestamp sorts as 0 and does not outrank valid timestamps;
    #   scan continues past corrupt peers to reach valid cards.
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("GROUP L: Corrupt comment handling (non-numeric ID/timestamp)")
    print("Acceptance: corrupt data is safely skipped; scan continues to valid cards")
    print("=" * 72)

    errors = run_scenario(
        harness, fx_mod.NONNUMERIC_OLDER_CREATED_AT_DOES_NOT_OUTRANK,
        "L1: nonnumeric-older-created-at-does-not-outrank-newer-numeric-verdict",
        tag="L1",
    )
    check("L1: nonnumeric-older-created-at-does-not-outrank-newer-numeric-verdict", errors)

    errors = run_scenario(
        harness, fx_mod.NONNUMERIC_COMMENT_ID_PERCENT_S_IS_SKIPPED,
        "L2: nonnumeric-comment-id-percent-s-is-skipped",
        tag="L2",
    )
    check("L2: nonnumeric-comment-id-percent-s-is-skipped", errors)

    errors = run_scenario(
        harness, fx_mod.SCAN_CONTINUES_AFTER_NONNUMERIC_COMMENT_ID,
        "L3: scan-continues-after-nonnumeric-comment-id",
        tag="L3",
    )
    check("L3: scan-continues-after-nonnumeric-comment-id", errors)

    # ══════════════════════════════════════════════════════════════════════
    # Scenario Group C3: Genuine operator-gated scope still needs_operator
    # Acceptance: deploy/DB/live/A3/cron content in title/body → operator_gated
    #   → MUST NOT complete, MUST emit needs_operator.
    # ══════════════════════════════════════════════════════════════════════

    errors = run_scenario(
        harness, fx_mod.C3_GENUINE_PROD_DB_CREDENTIAL_OPERATOR_GATED_NEEDS_OPERATOR,
        "C3(harness): c3-genuine-prod-db-credential-operator-gated-needs-operator",
        tag="C3",
    )
    check("C3(harness): c3-genuine-prod-db-credential-operator-gated-needs-operator", errors)

    # ══════════════════════════════════════════════════════════════════════
    # Summary
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    status = "PASS" if not failures else "FAIL"
    print(f"VERDICT-ROUTER-SCENARIO-TEST {status}: {passed}/{total} passed")
    if failures:
        for name, errs in failures:
            print(f"  FAILED: {name}")
            for e in errs:
                print(f"    {e}")
    print("=" * 72)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
