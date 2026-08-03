#!/usr/bin/env python3
"""Explicit dry-run and planned-mutation mode tests for the REVIEW_VERDICT router harness.

This file is a standalone test that uses the importable harness API and the fixture
JSON file to prove:

1. Dry-run mode causes NO mutation execution (live_side_effects_possible=False)
   while still faithfully reporting what WOULD happen (planned_mutations, comments,
   unblock_actions, completion_actions).

2. Planned-mutation mode captures the INTENDED operations (same verdict parsing,
   same safety classification, same action/result, same planned_mutations) while
   still reporting live_side_effects_possible=False.

3. Both modes are consistent — they agree on the analysis; mutation-plan mode just
   exposes more action detail (comments, unblock_actions, completion_actions).

4. Router-script mode (production script against temp DB) also preserves
   side-effect-free execution via dry-run flags.

Run standalone:
    python3 agent-hooks/verdict-router-harness-modes-test.py

Run as part of full selftest suite:
    bash agent-hooks/run-selftests.sh              # umbrella runner
    bash agent-hooks/verdict-router.selftest.sh     # verdict-router suite only

Requires no live board credentials or network access.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = ROOT / "agent-hooks" / "verdict-router-harness.py"
FIXTURES_PATH = ROOT / "agent-hooks" / "verdict-router.fixtures.json"
ROUTER_PATH = ROOT / "scripts" / "verdict_router.py"


def load_harness():
    spec = importlib.util.spec_from_file_location("verdict_router_harness", HARNESS_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load harness from {HARNESS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_fixtures() -> list[dict]:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


def main() -> int:
    harness = load_harness()
    fixtures = load_fixtures()
    total = 0
    passed = 0
    failures: list[str] = []

    # ── 1. Full fixture suite: dry-run mode ──────────────────────────────────
    print("=" * 72)
    print("SECTION 1: Dry-run mode — 22 fixtures")
    print("Proving: no mutation execution, faithful plan reporting")
    print("=" * 72)

    dry_results = {}
    for fx in fixtures:
        name = fx["name"]
        total += 1
        try:
            result = harness.run_harness(
                board=str(fx.get("board", "test")),
                task=fx["task"],
                mode="dry-run",
            )
            dry_results[name] = result
            ok = _assert_dry_run(name, result, fx, harness)
            if ok:
                passed += 1
                print(f"  PASS dry-run/{name}")
            else:
                failures.append(f"dry-run/{name}")
                print(f"  FAIL dry-run/{name}")
        except Exception as exc:
            failures.append(f"dry-run/{name}: {exc}")
            print(f"  FAIL dry-run/{name}: {exc}")

    # ── 2. Full fixture suite: planned-mutation mode ────────────────────────
    print()
    print("=" * 72)
    print("SECTION 2: Planned-mutation mode — 22 fixtures")
    print("Proving: captures intended operations, same analysis as dry-run")
    print("=" * 72)

    plan_results = {}
    for fx in fixtures:
        name = fx["name"]
        total += 1
        try:
            result = harness.run_harness(
                board=str(fx.get("board", "test")),
                task=fx["task"],
                mode="mutation-plan",
            )
            plan_results[name] = result
            ok = _assert_planned_mutation(name, result, fx, harness)
            if ok:
                passed += 1
                print(f"  PASS plan/{name}")
            else:
                failures.append(f"plan/{name}")
                print(f"  FAIL plan/{name}")
        except Exception as exc:
            failures.append(f"plan/{name}: {exc}")
            print(f"  FAIL plan/{name}: {exc}")

    # ── 3. Cross-mode consistency ────────────────────────────────────────────
    print()
    print("=" * 72)
    print("SECTION 3: Cross-mode consistency — dry-run vs mutation-plan")
    print("Proving: both modes agree on analysis, differ only in action detail")
    print("=" * 72)

    for fx in fixtures:
        name = fx["name"]
        total += 1
        try:
            dry = dry_results.get(name)
            plan = plan_results.get(name)
            if dry is None or plan is None:
                failures.append(f"cross-mode/{name}: missing result")
                print(f"  FAIL cross-mode/{name}: missing result")
                continue
            ok = _assert_cross_mode_consistency(name, dry, plan, fx)
            if ok:
                passed += 1
                print(f"  PASS cross-mode/{name}")
            else:
                failures.append(f"cross-mode/{name}")
                print(f"  FAIL cross-mode/{name}")
        except Exception as exc:
            failures.append(f"cross-mode/{name}: {exc}")
            print(f"  FAIL cross-mode/{name}: {exc}")

    # ── 4. Router-script mode: production script is side-effect-free ─────────
    print()
    print("=" * 72)
    print("SECTION 4: Router-script mode — production script temp DB")
    print("Proving: production script run via --dry-run against temp DB has no live side effects")
    print("=" * 72)

    script_fixtures = [
        fx
        for fx in fixtures
        if fx["name"]
        in {
            "approved-source-card-completes",
            "changes-requested-unblocks-with-quoted-finding",
            "non-latest-verdict-ignored",
            "off-target-approved-fails-closed",
            "ambiguous-malformed-verdict-fails-closed",
        }
    ]
    for fx in script_fixtures:
        name = fx["name"]
        total += 1
        try:
            result = harness.run_harness(
                board=str(fx.get("board", "test")),
                task=fx["task"],
                mode="dry-run",
                router_script=str(ROUTER_PATH),
            )
            ok = _assert_router_script_result(name, result, fx, harness)
            if ok:
                passed += 1
                print(f"  PASS router-script/{name}")
            else:
                failures.append(f"router-script/{name}")
                print(f"  FAIL router-script/{name}")
        except Exception as exc:
            failures.append(f"router-script/{name}: {exc}")
            print(f"  FAIL router-script/{name}: {exc}")

    # ── Summary ──────────────────────────────────────────────────────────────
    print()
    print("=" * 72)
    status = "PASS" if not failures else "FAIL"
    print(f"DRY-RUN-PLANNED-MUTATION-TEST {status}: {passed}/{total} passed")
    if failures:
        for f in failures:
            print(f"  Failed: {f}")
    print("=" * 72)
    return 0 if not failures else 1


def _assert_dry_run(name: str, result: dict, fixture: dict, harness) -> bool:
    """Assert dry-run produces no side effects and faithfully reports the plan."""
    errors: list[str] = []

    # Core dry-run invariants
    if result.get("mode") != "dry-run":
        errors.append(f"expected mode=dry-run, got {result.get('mode')!r}")
    if result.get("live_side_effects_possible") is not False:
        errors.append(
            f"expected live_side_effects_possible=False, got {result.get('live_side_effects_possible')!r}"
        )
    if result.get("ok") is not True:
        errors.append(f"expected ok=True, got {result.get('ok')!r}")

    # Must have exactly one result item
    items = result.get("results", [])
    if len(items) != 1:
        errors.append(f"expected 1 result item, got {len(items)}")
        _report_errors(name, errors)
        return False

    item = items[0]
    expect = fixture.get("expect", {})

    # Parsed verdict matches expected
    pv = item.get("parsed_verdict", {})
    if pv.get("value") != expect.get("verdict_value"):
        errors.append(
            f"parsed_verdict.value: expected {expect.get('verdict_value')!r}, got {pv.get('value')!r}"
        )
    if pv.get("target_validation") != expect.get("target_validation"):
        errors.append(
            f"target_validation: expected {expect.get('target_validation')!r}, got {pv.get('target_validation')!r}"
        )

    # Safety classification
    if item.get("safety_classification") != expect.get("scope_class"):
        errors.append(
            f"safety_classification: expected {expect.get('scope_class')!r}, got {item.get('safety_classification')!r}"
        )

    # Planned mutations
    expected_mutations = expect.get("mutations", [])
    if item.get("planned_mutations") != expected_mutations:
        errors.append(
            f"planned_mutations: expected {expected_mutations!r}, got {item.get('planned_mutations')!r}"
        )

    # Forbidden mutations (must NOT be in planned_mutations)
    for forbidden in expect.get("forbid_mutations", []):
        if forbidden in (item.get("planned_mutations") or []):
            errors.append(f"forbidden mutation planned: {forbidden}")

    # ── DRY-RUN SPECIFIC ASSERTIONS ──
    # Dry-run does NOT suppress planned actions. The structured result must still
    # report what WOULD happen — but the mode flag and live_side_effects flag
    # prove nothing actually executes.

    action = item.get("plan", {}).get("action", item["parsed_verdict"].get("value"))
    expect_action = expect.get("action")

    # For dry-run, if the action is complete, the structured result must show
    # completion_actions but NO comments or unblock_actions.
    if expect_action == "complete":
        if not item.get("completion_actions"):
            errors.append("dry-run: expected non-empty completion_actions for complete action")
        if item.get("comments"):
            errors.append("dry-run: expected empty comments for complete action (no router comment needed)")
        if item.get("unblock_actions"):
            errors.append("dry-run: expected empty unblock_actions for complete action")
        for ca in item.get("completion_actions", []):
            if ca.get("task_id") != str(fixture["task"]["id"]):
                errors.append(f"dry-run: completion_actions task_id mismatch: {ca.get('task_id')}")
            if not ca.get("idempotency_key"):
                errors.append("dry-run: completion_actions missing idempotency_key")

    # For unblock_rework, show both comment AND unblock_actions
    elif expect_action == "unblock_rework":
        if not item.get("comments"):
            errors.append("dry-run: expected non-empty comments for unblock_rework action")
        if not item.get("unblock_actions"):
            errors.append("dry-run: expected non-empty unblock_actions for unblock_rework action")
        if item.get("completion_actions"):
            errors.append("dry-run: expected empty completion_actions for unblock_rework action")
        for comment in item.get("comments", []):
            if not str(comment.get("body", "")).startswith("verdict-router: REWORK_REQUIRED"):
                errors.append(
                    "dry-run: comment should start with verict-router: REWORK_REQUIRED"
                )
            if not comment.get("idempotency_key"):
                errors.append("dry-run: comment missing idempotency_key")

    # For needs_pm / needs_operator, show comment but NO completion or unblock
    elif expect_action in ("needs_pm", "needs_operator"):
        if not item.get("comments"):
            errors.append(f"dry-run: expected non-empty comments for {expect_action} action")
        if item.get("completion_actions"):
            errors.append(f"dry-run: expected empty completion_actions for {expect_action} action")
        if item.get("unblock_actions"):
            errors.append(f"dry-run: expected empty unblock_actions for {expect_action} action")
        prefix = "NEEDS-OPERATOR" if expect_action == "needs_operator" else "NEEDS-PM"
        for comment in item.get("comments", []):
            if prefix not in str(comment.get("body", "")):
                errors.append(f"dry-run: comment should contain '{prefix}' prefix")
            if not comment.get("idempotency_key"):
                errors.append("dry-run: comment missing idempotency_key")

    # For skip actions, everything must be empty
    elif expect_action == "skip" or expect_action is None:
        if item.get("comments"):
            errors.append("dry-run: expected empty comments for skip/noop action")
        if item.get("unblock_actions"):
            errors.append("dry-run: expected empty unblock_actions for skip/noop action")
        if item.get("completion_actions"):
            errors.append("dry-run: expected empty completion_actions for skip/noop action")
        # ignored_noop_results should be populated for skip
        if not item.get("ignored_noop_results"):
            errors.append("dry-run: expected non-empty ignored_noop_results for skip")

    # Router comment must not contain parseable REVIEW_VERDICT tokens
    for comment in item.get("comments", []):
        if harness.comment_without_parseable_verdict(str(comment.get("body", ""))):
            pass  # good
        else:
            errors.append("dry-run: router comment contains parseable REVIEW_VERDICT token")

    # Idempotency key must be present for any non-skip action
    plan = item.get("plan", {})
    if expect_action not in ("skip", None) and not plan.get("idempotency_key"):
        errors.append("dry-run: non-skip plan missing idempotency_key")

    _report_errors(name, errors)
    return len(errors) == 0


def _assert_planned_mutation(name: str, result: dict, fixture: dict, harness) -> bool:
    """Assert mutation-plan mode captures intended operations without executing."""
    errors: list[str] = []

    # Mode invariant
    if result.get("mode") != "mutation-plan":
        errors.append(f"expected mode=mutation-plan, got {result.get('mode')!r}")
    if result.get("live_side_effects_possible") is not False:
        errors.append(
            f"expected live_side_effects_possible=False, got {result.get('live_side_effects_possible')!r}"
        )
    if result.get("ok") is not True:
        errors.append(f"expected ok=True, got {result.get('ok')!r}")

    items = result.get("results", [])
    if len(items) != 1:
        errors.append(f"expected 1 result item, got {len(items)}")
        _report_errors(name, errors)
        return False

    item = items[0]
    expect = fixture.get("expect", {})

    # ── PLANNED-MUTATION SPECIFIC ASSERTIONS ──
    # Planned-mutation mode must faithfully describe the INTENDED operations.
    # The structured result must populate comments, unblock_actions,
    # completion_actions, and ignored_noop_results based on the plan.

    # Plan action/result
    plan_action = item.get("plan", {}).get("action")
    plan_result = item.get("plan", {}).get("result")
    expect_action = expect.get("action")
    expect_result = expect.get("result")

    if plan_action != expect_action:
        errors.append(
            f"plan.action: expected {expect_action!r}, got {plan_action!r}"
        )
    if plan_result != expect_result:
        errors.append(
            f"plan.result: expected {expect_result!r}, got {plan_result!r}"
        )

    # Planned mutations
    expected_mutations = expect.get("mutations", [])
    if item.get("planned_mutations") != expected_mutations:
        errors.append(
            f"planned_mutations: expected {expected_mutations!r}, got {item.get('planned_mutations')!r}"
        )
    for forbidden in expect.get("forbid_mutations", []):
        if forbidden in (item.get("planned_mutations") or []):
            errors.append(f"forbidden mutation planned: {forbidden}")

    # Comment assertions
    expected_prefix = expect.get("comment_prefix")
    expected_contains = expect.get("comment_contains")
    if expected_prefix is not None:
        # Comment must be populated
        if not item.get("comments"):
            errors.append(
                f"mutation-plan: expected comment with prefix {expected_prefix!r}, got none"
            )
        else:
            for comment in item.get("comments", []):
                body = str(comment.get("body", ""))
                if not body.startswith(expected_prefix):
                    errors.append(
                        f"mutation-plan: comment prefix expected {expected_prefix!r}, got {body[:80]!r}"
                    )
                if expected_contains and expected_contains not in body:
                    errors.append(
                        f"mutation-plan: comment missing expected excerpt {expected_contains!r}"
                    )
                if harness.comment_without_parseable_verdict(body):
                    pass  # good
                else:
                    errors.append("mutation-plan: router comment contains parseable REVIEW_VERDICT token")
    elif item.get("comments"):
        # No comment expected, but we may have one for needs_pm/needs_operator/unblock_rework
        # Only an issue if action matches
        if expect_action in ("complete", "skip", None):
            if item.get("comments"):
                errors.append(f"mutation-plan: unexpected comment for action {expect_action}")

    # Completion actions
    if "complete" in (expected_mutations or []):
        if not item.get("completion_actions"):
            errors.append("mutation-plan: expected completion_actions for complete mutation")
        for ca in item.get("completion_actions", []):
            if ca.get("task_id") != str(fixture["task"]["id"]):
                errors.append(f"mutation-plan: completion_actions task_id mismatch: {ca.get('task_id')}")
            if not ca.get("idempotency_key"):
                errors.append("mutation-plan: completion_actions missing idempotency_key")
            if not ca.get("summary"):
                errors.append("mutation-plan: completion_actions missing summary")
    elif item.get("completion_actions"):
        errors.append(f"mutation-plan: unexpected completion_actions for mutations={expected_mutations}")

    # Unblock actions
    if "unblock" in (expected_mutations or []):
        if not item.get("unblock_actions"):
            errors.append("mutation-plan: expected unblock_actions for unblock mutation")
        for ua in item.get("unblock_actions", []):
            if ua.get("task_id") != str(fixture["task"]["id"]):
                errors.append(f"mutation-plan: unblock_actions task_id mismatch: {ua.get('task_id')}")
            if not ua.get("idempotency_key"):
                errors.append("mutation-plan: unblock_actions missing idempotency_key")
    elif item.get("unblock_actions"):
        errors.append(f"mutation-plan: unexpected unblock_actions for mutations={expected_mutations}")

    # Ignored/noop results
    if expect_action == "skip" or not expected_mutations:
        if not item.get("ignored_noop_results"):
            errors.append("mutation-plan: expected ignored_noop_results for skip/noop")
        for nr in item.get("ignored_noop_results", []):
            if nr.get("task_id") != str(fixture["task"]["id"]):
                errors.append(f"mutation-plan: ignored_noop_results task_id mismatch: {nr.get('task_id')}")
            if not nr.get("reason"):
                errors.append("mutation-plan: ignored_noop_results missing reason")
    elif item.get("ignored_noop_results"):
        errors.append(f"mutation-plan: unexpected ignored_noop_results for action {expect_action}")

    _report_errors(name, errors)
    return len(errors) == 0


def _assert_cross_mode_consistency(name: str, dry: dict, plan: dict, fixture: dict) -> bool:
    """Assert dry-run and mutation-plan agree on analysis-level fields."""
    errors: list[str] = []

    dry_item = dry["results"][0]
    plan_item = plan["results"][0]

    # Same verdict parsing
    for field in ("value", "raw_value", "token_count", "source_comment_id",
                  "source_author", "target_validation"):
        dv = dry_item["parsed_verdict"].get(field)
        pv = plan_item["parsed_verdict"].get(field)
        if dv != pv:
            errors.append(
                f"cross-mode: parsed_verdict.{field} differs: dry={dv!r} plan={pv!r}"
            )

    # Same safety classification
    if dry_item.get("safety_classification") != plan_item.get("safety_classification"):
        errors.append(
            f"cross-mode: safety_classification differs: dry={dry_item.get('safety_classification')!r} plan={plan_item.get('safety_classification')!r}"
        )

    # Same planned mutations
    if dry_item.get("planned_mutations") != plan_item.get("planned_mutations"):
        errors.append(
            f"cross-mode: planned_mutations differs: dry={dry_item.get('planned_mutations')!r} plan={plan_item.get('planned_mutations')!r}"
        )

    # Same plan action/result
    dp = dry_item.get("plan", {})
    pp = plan_item.get("plan", {})
    for field in ("action", "result", "verdict_value", "target_validation", "scope_class"):
        if dp.get(field) != pp.get(field):
            errors.append(
                f"cross-mode: plan.{field} differs: dry={dp.get(field)!r} plan={pp.get(field)!r}"
            )

    # Same idempotency_key (only relevant for non-skip actions)
    if dp.get("idempotency_key") != pp.get("idempotency_key"):
        errors.append(
            f"cross-mode: idempotency_key differs: dry={dp.get('idempotency_key')!r} plan={pp.get('idempotency_key')!r}"
        )

    # Both modes must report no live side effects
    if dry.get("live_side_effects_possible") is not False:
        errors.append("cross-mode: dry-run has live_side_effects_possible=True")
    if plan.get("live_side_effects_possible") is not False:
        errors.append("cross-mode: mutation-plan has live_side_effects_possible=True")

    # Mode-specific difference: plan may populate comments when dry doesn't
    expect_action = fixture.get("expect", {}).get("action")
    if expect_action == "complete":
        # Both should have same completion_actions structure
        if dry_item.get("completion_actions") != plan_item.get("completion_actions"):
            errors.append("cross-mode: completion_actions differ between dry-run and mutation-plan")

    _report_errors(name, errors)
    return len(errors) == 0


def _assert_router_script_result(name: str, result: dict, fixture: dict, harness) -> bool:
    """Assert router-script mode (production script) also respects dry-run."""
    errors: list[str] = []

    # Must be using router-script implementation
    if result.get("implementation") != "router-script":
        errors.append(
            f"expected implementation=router-script, got {result.get('implementation')!r}"
        )

    # Must be in dry-run mode
    if result.get("mode") != "dry-run":
        errors.append(f"expected mode=dry-run, got {result.get('mode')!r}")

    # Must report no live side effects
    if result.get("live_side_effects_possible") is not False:
        errors.append(
            f"expected live_side_effects_possible=False, got {result.get('live_side_effects_possible')!r}"
        )

    # Must pass
    if result.get("ok") is not True:
        errors.append(f"expected ok=True, got {result.get('ok')!r}")

    # Result structure should match the reference plan
    items = result.get("results", [])
    if len(items) != 1:
        errors.append(f"expected 1 result item, got {len(items)}")
        _report_errors(name, errors)
        return False

    item = items[0]
    expect = fixture.get("expect", {})

    # Verify planned fields match fixture expectations
    for field, efield in [("action", "action"), ("verdict_value", "verdict_value"),
                          ("target_validation", "target_validation"),
                          ("scope_class", "scope_class")]:
        plan_val = item.get("plan", {}).get(field)
        expected = expect.get(efield)
        if plan_val != expected:
            errors.append(
                f"router-script: plan.{field}: expected {expected!r}, got {plan_val!r}"
            )

    # Verify mutations
    expected_mutations = expect.get("mutations", [])
    if item.get("planned_mutations") != expected_mutations:
        errors.append(
            f"router-script: planned_mutations: expected {expected_mutations!r}, got {item.get('planned_mutations')!r}"
        )

    # Verify router produces a non-empty reason
    reason = item.get("plan", {}).get("reason")
    if not str(reason or "").strip():
        errors.append("router-script: plan missing non-empty reason")

    # Router comment must not contain parseable REVIEW_VERDICT
    for comment in item.get("comments", []):
        if not harness.comment_without_parseable_verdict(str(comment.get("body", ""))):
            errors.append("router-script: router comment contains parseable REVIEW_VERDICT token")

    _report_errors(name, errors)
    return len(errors) == 0


def _report_errors(name: str, errors: list[str]) -> None:
    for e in errors:
        print(f"    ASSERT {name}: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
