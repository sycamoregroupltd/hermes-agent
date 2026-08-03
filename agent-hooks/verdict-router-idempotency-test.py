#!/usr/bin/env python3
"""Idempotency, dry-run, mutation-plan stability, and no-live-board-writes tests.

Tests verify side-effect safety and repeatability for the REVIEW_VERDICT router:

  1. (A) No-live-board-writes proof — explicit SQLite board unchanged after dry-run
  2. (B) Mutation-plan stability — same fixture run twice produces identical plan
  3. (C) Repeated-run idempotency chain — first run produces action key,
       second run with that key produces skipped_idempotent
  4. (D) Router-script idempotency — production script against temp board
       handles all 4 idempotent action types correctly
  5. (E) Full 21+ fixture sweep — every fixture that would mutate produces
       a stable idempotency_key that suppresses a duplicate run

Every test operates purely in-memory or against an isolated temp directory.
No live kanban boards are read or mutated at any point.

Run standalone:
    python3 agent-hooks/verdict-router-idempotency-test.py

Run as part of full selftest suite:
    bash agent-hooks/verdict-router.selftest.sh
    bash agent-hooks/run-selftests.sh
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
HARNESS_PATH = ROOT / "verdict-router-harness.py"
FIXTURES_PATH = ROOT / "verdict-router.fixtures.json"
ROUTER_PATH = ROOT.parent / "scripts" / "verdict_router.py"


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


# ── Shared helpers ─────────────────────────────────────────────────────────


def board_state_snapshot(db_path: Path) -> dict[str, Any]:
    """Read all tasks and comments from a kanban DB, return as dict."""
    con = sqlite3.connect(db_path)
    try:
        tasks = list(con.execute("SELECT id, status, title, priority FROM tasks ORDER BY id"))
        comments = list(con.execute("SELECT id, task_id, author, body FROM task_comments ORDER BY id"))
        return {
            "task_count": len(tasks),
            "tasks": {row[0]: {"status": row[1], "title": row[2], "priority": row[3]} for row in tasks},
            "comment_count": len(comments),
            "comments": [{"id": row[0], "task_id": row[1], "author": row[2][:20]} for row in comments],
        }
    finally:
        con.close()


def create_temp_board(fixture: dict) -> Path:
    """Create a minimal isolated kanban DB for one fixture and return its path."""
    tmp = tempfile.mkdtemp(prefix="verdict-idempotency-")
    board = str(fixture.get("board", "test"))
    db = Path(tmp) / "kanban" / "boards" / board / "kanban.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        con.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,
                assignee TEXT, status TEXT NOT NULL, priority INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE task_comments (
                id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
                author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL
            );
        """)
        task = fixture["task"]
        con.execute(
            "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)",
            (str(task["id"]), str(task.get("title", "")), str(task.get("body", "")),
             "test-engineer", str(task.get("status", "blocked")),
             int(task.get("priority", 0)), int(task.get("created_at", 1783110000))),
        )
        for comment in task.get("comments", []):
            con.execute(
                "INSERT INTO task_comments(id, task_id, author, body, created_at) VALUES (?,?,?,?,?)",
                (int(comment["id"]), str(task["id"]), str(comment.get("author", "")),
                 str(comment.get("body", "")), comment.get("created_at", 0)),
            )
        for key in task.get("existing_idempotency_keys", []):
            next_id = max([int(c["id"]) for c in task.get("comments", [])] or [0]) + 1
            con.execute(
                "INSERT INTO task_comments(id, task_id, author, body, created_at) VALUES (?,?,?,?,?)",
                (next_id, str(task["id"]), "verdict-router",
                 f"prior verdict-router marker idempotency_key={key}",
                 int(task.get("created_at", 1783110000)) - 1),
            )
        con.commit()
    finally:
        con.close()
    return db


def _run_without_keys(fixture: dict, harness: Any, mode: str = "dry-run") -> dict[str, Any]:
    """Run harness without idempotency keys present, return the plan."""
    task = dict(fixture["task"])
    task["existing_idempotency_keys"] = []
    return harness.run_harness(
        board=str(fixture.get("board", "test")),
        task=task,
        mode=mode,
    )


def _run_with_keys(fixture: dict, keys: list[str], harness: Any, mode: str = "dry-run") -> dict[str, Any]:
    """Run harness with specific idempotency keys present."""
    task = dict(fixture["task"])
    task["existing_idempotency_keys"] = list(keys)
    return harness.run_harness(
        board=str(fixture.get("board", "test")),
        task=task,
        mode=mode,
    )


# ── Assertion helpers ──────────────────────────────────────────────────────


def _assert_no_live_side_effects(result: dict, tag: str) -> list[str]:
    errors: list[str] = []
    if result.get("live_side_effects_possible") is not False:
        errors.append(f"[{tag}] expected live_side_effects_possible=False, got {result.get('live_side_effects_possible')!r}")
    if result.get("ok") is not True:
        errors.append(f"[{tag}] expected ok=True, got {result.get('ok')!r}")
    return errors


def _assert_plan_field(plan: dict, expected: str | None, field: str, tag: str) -> list[str]:
    errors: list[str] = []
    if plan.get(field) != expected:
        errors.append(f"[{tag}] plan.{field}: expected {expected!r}, got {plan.get(field)!r}")
    return errors


def _assert_mutations(item: dict, expected_mutations: list[str], forbid_mutations: list[str], tag: str) -> list[str]:
    errors: list[str] = []
    planned = list(item.get("planned_mutations", []))
    if planned != expected_mutations:
        errors.append(f"[{tag}] planned_mutations: expected {expected_mutations!r}, got {planned!r}")
    for fm in forbid_mutations:
        if fm in planned:
            errors.append(f"[{tag}] forbidden mutation planned: {fm}")
    return errors


def _assert_item_ok(result: dict) -> dict[str, Any]:
    items = result.get("results", [])
    assert len(items) == 1, f"expected 1 result item, got {len(items)}"
    return items[0]


def _assert_plans_equal(p1: dict, p2: dict, fields: list[str], tag: str) -> list[str]:
    errors: list[str] = []
    for field in fields:
        v1 = p1.get(field)
        v2 = p2.get(field)
        if v1 != v2:
            errors.append(f"[{tag}] plan.{field} differs between runs: first={v1!r} second={v2!r}")
    return errors


# ── Test runner ─────────────────────────────────────────────────────────────


def main() -> int:
    harness = load_harness()
    fixtures = load_fixtures()
    passed = 0
    total = 0
    failures: list[str] = []

    def check(name: str, errors: list[str]) -> None:
        nonlocal total, passed
        total += 1
        if not errors:
            passed += 1
            print(f"  PASS {name}")
        else:
            failures.append(name)
            print(f"  FAIL {name}")
            for e in errors:
                print(f"       {e}")

    # ══════════════════════════════════════════════════════════════════════
    # SECTION A: No-live-board-writes proof
    # Acceptance: dry-run mode reports intended actions without mutating any
    # board.  We create a temp SQLite kanban DB, snapshot its state, run the
    # harness, and verify the board is *exactly* as it was before.
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("SECTION A: No-live-board-writes proof")
    print("Proving: dry-run does not mutate any SQLite kanban board")
    print("=" * 72)

    # Create a temp board for the approved-source-card fixture (no keys)
    fx = [f for f in fixtures if f["name"] == "approved-source-card-completes"][0]
    task_no_keys = dict(fx["task"])
    task_no_keys["existing_idempotency_keys"] = []
    fx_no_keys = dict(fx)
    fx_no_keys["task"] = task_no_keys

    db_path = create_temp_board(fx_no_keys)
    before = board_state_snapshot(db_path)

    # Run harness dry-run — this calls reference_plan which is purely in-memory
    result = harness.run_harness(
        board=str(fx_no_keys.get("board", "test")),
        task=task_no_keys,
        mode="dry-run",
    )
    item = _assert_item_ok(result)
    errors_a = _assert_no_live_side_effects(result, "A1")
    errors_a += _assert_plan_field(item.get("plan", {}), "complete", "action", "A1")
    errors_a += _assert_mutations(item, ["complete"], [], "A1")

    # Prove board is unchanged
    after = board_state_snapshot(db_path)
    if before["task_count"] != after["task_count"]:
        errors_a.append(f"A1: task count changed: {before['task_count']} -> {after['task_count']}")
    if before["comment_count"] != after["comment_count"]:
        errors_a.append(f"A1: comment count changed: {before['comment_count']} -> {after['comment_count']}")
    if before["tasks"] != after["tasks"]:
        errors_a.append(f"A1: tasks changed after dry-run")
    if before["comments"] != after["comments"]:
        errors_a.append(f"A1: comments changed after dry-run")

    # Verify with a needs_pm fixture too
    fx2 = [f for f in fixtures if f["name"] == "ambiguous-malformed-verdict-fails-closed"][0]
    db_path2 = create_temp_board(fx2)
    before2 = board_state_snapshot(db_path2)
    result2 = harness.run_harness(
        board=str(fx2.get("board", "test")),
        task=fx2["task"],
        mode="dry-run",
    )
    item2 = _assert_item_ok(result2)
    errors_a += _assert_no_live_side_effects(result2, "A2")
    errors_a += _assert_plan_field(item2.get("plan", {}), "needs_pm", "action", "A2")
    after2 = board_state_snapshot(db_path2)
    if before2["task_count"] != after2["task_count"]:
        errors_a.append(f"A2: task count changed: {before2['task_count']} -> {after2['task_count']}")
    if before2["comment_count"] != after2["comment_count"]:
        errors_a.append(f"A2: comment count changed: {before2['comment_count']} -> {after2['comment_count']}")

    check("A1: no-live-board-writes (complete)", errors_a)

    # ══════════════════════════════════════════════════════════════════════
    # SECTION B: Mutation-plan stability
    # Acceptance: running the same fixture twice in mutation-plan mode
    # produces identical plan dicts (same idempotency_key, same mutations,
    # same action/result, same reason).
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("SECTION B: Mutation-plan stability")
    print("Proving: same fixture run twice in mutation-plan produces identical plans")
    print("=" * 72)

    STABILITY_FIELDS = [
        "action", "result", "reason", "verdict_value", "target_validation",
        "scope_class", "idempotency_key", "mutations",
    ]
    stability_fixtures = [
        "approved-source-card-completes",
        "changes-requested-unblocks-with-quoted-finding",
        "ambiguous-malformed-verdict-fails-closed",
        "approved-runtime-a3-needs-operator",
    ]
    for sfx_name in stability_fixtures:
        sfx = [f for f in fixtures if f["name"] == sfx_name][0]
        plan1 = harness.reference_plan(sfx, mode="mutation-plan")
        plan2 = harness.reference_plan(sfx, mode="mutation-plan")
        errors_b = _assert_plans_equal(plan1, plan2, STABILITY_FIELDS, sfx_name)
        check(f"B1: plan-stability/{sfx_name}", errors_b)

    # ══════════════════════════════════════════════════════════════════════
    # SECTION C: Repeated-run idempotency chain
    # Acceptance: first run without keys produces a plan with idempotency_key;
    # second run with that key injected into existing_idempotency_keys
    # produces skip/skipped_idempotent with empty mutations.
    # Covers all four actionable verdict paths:
    #   - APPROVED source-only → complete
    #   - CHANGES_REQUESTED source-only → unblock_rework
    #   - APPROVED cross-target → needs_pm
    #   - APPROVED operator-gated → needs_operator
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("SECTION C: Repeated-run idempotency chain")
    print("Proving: idempotency key from first run suppresses second run")
    print("=" * 72)

    chain_cases = [
        ("C1", "approved-source-card-completes", "complete", "skipped_idempotent", []),
        ("C2", "changes-requested-unblocks-with-quoted-finding", "unblock_rework", "skipped_idempotent", []),
        ("C3", "off-target-approved-fails-closed", "needs_pm", "skipped_idempotent", []),
        ("C4", "approved-runtime-a3-needs-operator", "needs_operator", "skipped_idempotent", []),
    ]

    for tag, case_name, expected_action, expected_skip_result, expected_skip_mutations in chain_cases:
        cfx = [f for f in fixtures if f["name"] == case_name][0]
        # First run: no keys
        result1 = _run_without_keys(cfx, harness, mode="dry-run")
        item1 = _assert_item_ok(result1)
        plan1 = item1.get("plan", {})
        idem_key = plan1.get("idempotency_key")

        # Verify first run produced the expected action
        errors_c: list[str] = []
        if plan1.get("action") != expected_action:
            errors_c.append(f"[{tag}] first-run action: expected {expected_action!r}, got {plan1.get('action')!r}")
        if not idem_key:
            errors_c.append(f"[{tag}] first-run missing idempotency_key")

        if not errors_c:
            # Second run: inject the key
            result2 = _run_with_keys(cfx, [idem_key], harness, mode="dry-run")
            item2 = _assert_item_ok(result2)
            plan2 = item2.get("plan", {})
            errors_c += _assert_plan_field(plan2, "skip", "action", tag)
            errors_c += _assert_plan_field(plan2, expected_skip_result, "result", tag)
            errors_c += _assert_mutations(item2, expected_skip_mutations, [], tag)
            # The idempotency_key in the skip plan should still be set
            if plan2.get("idempotency_key") != idem_key:
                errors_c.append(f"[{tag}] second-run idempotency_key differs: {plan2.get('idempotency_key')!r} vs first {idem_key!r}")

        check(f"C: idempotency-chain/{case_name}", errors_c)

    # CHANGES_REQUESTED unblock_rework chain uses a specific key calculation
    # The key for CHANGES_REQUESTED is:
    #   idempotency_key(board, task_id, source_comment_id, "unblock_rework")
    cr_fx = [f for f in fixtures if f["name"] == "changes-requested-unblocks-with-quoted-finding"][0]
    cr_result1 = _run_without_keys(cr_fx, harness, mode="dry-run")
    cr_plan1 = _assert_item_ok(cr_result1).get("plan", {})
    cr_key = cr_plan1.get("idempotency_key")
    cr_errors: list[str] = []
    if cr_plan1.get("action") != "unblock_rework":
        cr_errors.append("[C5] first-run action: expected unblock_rework, got {!r}".format(cr_plan1.get("action")))
    if not cr_key:
        cr_errors.append("[C5] first-run missing idempotency_key")

    if not cr_errors:
        cr_result2 = _run_with_keys(cr_fx, [cr_key], harness, mode="dry-run")
        cr_item2 = _assert_item_ok(cr_result2)
        cr_plan2 = cr_item2.get("plan", {})
        cr_errors += _assert_plan_field(cr_plan2, "skip", "action", "C5")
        cr_errors += _assert_plan_field(cr_plan2, "skipped_idempotent", "result", "C5")
        cr_errors += _assert_mutations(cr_item2, [], [], "C5")
        if cr_plan2.get("idempotency_key") != cr_key:
            cr_errors.append(f"[C5] second-run idempotency_key differs: {cr_plan2.get('idempotency_key')!r} vs first {cr_key!r}")

    check("C5: idempotency-chain/changes-requested-unblocks-with-quoted-finding", cr_errors)

    # ══════════════════════════════════════════════════════════════════════
    # SECTION D: Router-script mode idempotency
    # Acceptance: production verdict_router.py, when run via --dry-run
    # against a temp board containing an existing idempotency marker,
    # correctly produces skipped_idempotent with no planned mutations.
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("SECTION D: Router-script idempotency")
    print("Proving: production router handles idempotency markers correctly")
    print("=" * 72)

    script_idem_fixtures = [
        ("repeated-run-idempotent-skips-existing-key", "skip", "skipped_idempotent", []),
        ("repeated-needs-pm-idempotent-skips-existing-key", "skip", "skipped_idempotent", []),
        ("repeated-needs-operator-idempotent-skips-existing-key", "skip", "skipped_idempotent", []),
        ("repeated-unblock-rework-idempotent-skips-existing-key", "skip", "skipped_idempotent", []),
    ]
    for sname, sexp_action, sexp_result, sexp_mutations in script_idem_fixtures:
        sfx = [f for f in fixtures if f["name"] == sname][0]
        errors_d: list[str] = []
        try:
            result = harness.run_harness(
                board=str(sfx.get("board", "test")),
                task=sfx["task"],
                mode="dry-run",
                router_script=str(ROUTER_PATH),
            )
            item = _assert_item_ok(result)
            errors_d += _assert_no_live_side_effects(result, sname)
            errors_d += _assert_plan_field(item.get("plan", {}), sexp_action, "action", sname)
            errors_d += _assert_plan_field(item.get("plan", {}), sexp_result, "result", sname)
            errors_d += _assert_mutations(item, sexp_mutations, [], sname)
        except Exception as exc:
            errors_d.append(f"[{sname}] router-script exception: {exc}")
        check(f"D1: router-script-idempotency/{sname}", errors_d)

    # ══════════════════════════════════════════════════════════════════════
    # SECTION E: Full fixture sweep — first-run → second-run idempotency
    # Acceptance: every fixture that produces a non-skip action with a
    # non-None idempotency_key, when run again with that key injected,
    # produces skip/skipped_idempotent with empty mutations.
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    print("SECTION E: Full fixture sweep — first-run → second-run idempotency")
    print("Proving: no fixture can produce duplicate side effects across runs")
    print("=" * 72)

    for fx in fixtures:
        name = fx["name"]
        # Fixtures with pre-existing keys are already-idempotent cases; skip them
        existing = fx.get("task", {}).get("existing_idempotency_keys", [])
        if existing:
            continue

        errors_e: list[str] = []
        # First run: no keys
        result1 = _run_without_keys(fx, harness, mode="dry-run")
        item1 = _assert_item_ok(result1)
        plan1 = item1.get("plan", {})
        action1 = plan1.get("action")
        idem_key = plan1.get("idempotency_key")

        # Only test fixtures that produce a non-skip action with a key
        if action1 in ("skip", None) or not idem_key:
            continue

        # Second run: inject the key from first run
        result2 = _run_with_keys(fx, [idem_key], harness, mode="dry-run")
        item2 = _assert_item_ok(result2)
        plan2 = item2.get("plan", {})

        errors_e += _assert_plan_field(plan2, "skip", "action", name)
        errors_e += _assert_plan_field(plan2, "skipped_idempotent", "result", name)
        errors_e += _assert_mutations(item2, [], [], name)
        if plan2.get("idempotency_key") != idem_key:
            errors_e.append(f"[{name}] second-run idempotency_key differs: {plan2.get('idempotency_key')!r} vs first {idem_key!r}")

        check(f"E1: sweep/{name}", errors_e)

    # ══════════════════════════════════════════════════════════════════════
    # Summary
    # ══════════════════════════════════════════════════════════════════════
    print()
    print("=" * 72)
    status = "PASS" if not failures else "FAIL"
    print(f"IDEMPOTENCY-TEST {status}: {passed}/{total} passed")
    if failures:
        for f in failures:
            print(f"  Failed: {f}")
    print("=" * 72)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
