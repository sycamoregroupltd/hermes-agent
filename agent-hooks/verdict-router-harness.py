#!/usr/bin/env python3
"""Deterministic REVIEW_VERDICT router verification harness.

The harness uses JSON fixtures and an in-memory board model. By default it runs a
small reference planner that encodes the safety contract from t_26d74e85. To test
an implementation, pass --router-command '<cmd>'; the command receives one fixture
JSON object on stdin and must print one plan JSON object on stdout with the fields
validated below. To exercise the production verdict_router.py implementation,
pass --router-script /path/to/verdict_router.py; the harness creates an isolated
temporary kanban DB and redirects router logs/notes away from live boards. No live
kanban board is read or mutated.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "v1"
ROUTER_AUTHORS = {"verdict-router", "cron:deterministic-verdict-router"}
VALID_VERDICTS = {"APPROVED", "APPROVE", "CHANGES_REQUESTED"}
APPROVED_ALIASES = {"APPROVE": "APPROVED"}
OPERATOR_TERMS = [
    "deploy",
    "production deploy",
    "live runtime",
    "gateway restart",
    "service restart",
    "runtime activation",
    "cron activation",
    "apply sentinel",
    "db migration",
    "db write",
    "schema",
    "seed",
    "delete",
    "truncate",
    "irreversible data",
    "live data",
    "a3",
    "upstream proposal",
    "frank approval",
    "operator approval",
    "maintainer approval",
    "push/merge-to-trunk",
    "github write permission",
    "live trading",
    "trade intent",
    "live_capped",
    "orders",
    "real money",
    "payment",
    "pricing",
    "checkout",
    "refund",
    "spend",
    "subscription",
    "credentials",
    "secrets",
    "token creation",
    "token rotation",
    "workforce-scaler",
]

VERDICT_RE = re.compile(r"REVIEW_VERDICT\s*[:=]\s*([A-Z0-9_]+)", re.I)
TASK_RE = re.compile(r"(?:(?P<board>[a-z0-9][a-z0-9_-]*)/)?(?P<task>t_[0-9a-zA-Z]+)")


def safe_sort_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def fixture_tasks(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the primary task plus optional board peers used to prove scan continuity."""
    return [fixture["task"], *fixture.get("extra_tasks", [])]


def has_nonnumeric_comment_id(fixture: dict[str, Any]) -> bool:
    for task in fixture_tasks(fixture):
        for comment in task.get("comments", []):
            if safe_sort_int(comment.get("id")) == 0 and str(comment.get("id")) != "0":
                return True
    return False


@dataclass(frozen=True)
class VerdictParse:
    raw_value: str | None
    verdict_value: str | None
    token_count: int
    comment: dict[str, Any] | None
    mentioned_task_ids: list[str]
    mentioned_board_refs: list[str]
    finding_excerpt: str | None


def latest_non_router_comment(task: dict[str, Any]) -> dict[str, Any] | None:
    comments = [
        c
        for c in task.get("comments", [])
        if str(c.get("author", "")) not in ROUTER_AUTHORS
        and not (safe_sort_int(c.get("id")) == 0 and str(c.get("id")) != "0")
    ]
    if not comments:
        return None
    return max(comments, key=lambda c: (safe_sort_int(c.get("created_at", 0)), safe_sort_int(c.get("id", 0))))


def parse_latest_verdict(task: dict[str, Any]) -> VerdictParse:
    comment = latest_non_router_comment(task)
    if not comment:
        return VerdictParse(None, None, 0, None, [], [], None)
    body = str(comment.get("body", ""))
    matches = list(VERDICT_RE.finditer(body))
    task_refs: list[str] = []
    board_refs: list[str] = []
    for m in TASK_RE.finditer(body):
        tid = m.group("task")
        task_refs.append(tid)
        if m.group("board"):
            board_refs.append(f"{m.group('board')}/{tid}")
    if len(matches) != 1:
        return VerdictParse(None, None, len(matches), comment, task_refs, board_refs, first_finding(body))
    raw = matches[0].group(1).upper()
    canonical = APPROVED_ALIASES.get(raw, raw)
    if raw not in VALID_VERDICTS:
        canonical = raw
    return VerdictParse(raw, canonical, 1, comment, task_refs, board_refs, first_finding(body))


def first_finding(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("REVIEW_VERDICT") or stripped.lower().startswith("target"):
            continue
        if "finding" in stripped.lower() or "block" in stripped.lower() or "fail" in stripped.lower():
            return stripped[:240]
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.upper().startswith("REVIEW_VERDICT"):
            return stripped[:240]
    return None


def target_validation(board: str, task: dict[str, Any], parsed: VerdictParse) -> str:
    if not parsed.comment or not parsed.verdict_value or parsed.token_count != 1:
        return "not-applicable"
    tid = str(task["id"])
    mentioned = parsed.mentioned_task_ids
    board_refs = parsed.mentioned_board_refs
    if parsed.verdict_value == "CHANGES_REQUESTED":
        if not mentioned:
            return "same-card"
        unique = set(mentioned)
        return "same-card" if unique == {tid} else "cross-target"
    if not mentioned:
        return "missing-target"
    if len(set(mentioned)) > 1:
        return "multi-target"
    if mentioned[0] != tid:
        return "cross-target"
    # A board-qualified reference to a different board is also cross-target.
    for ref in board_refs:
        ref_board, ref_tid = ref.split("/", 1)
        if ref_tid == tid and ref_board != board:
            return "cross-target"
    return "same-card"


def detect_operator_term(*parts: str) -> str | None:
    text = "\n".join(parts).lower()
    for term in OPERATOR_TERMS:
        if term in text:
            return term
    return None


def scope_class(task: dict[str, Any], parsed: VerdictParse) -> tuple[str, str | None]:
    # C2 fix (t_8874b97b / t_9a0af491): operator-gate detection scans ONLY the
    # task title/body scope — NOT the reviewer comment prose and NOT the
    # block_reason. A verdict that *denies* a gate ("no prod, no creds",
    # "A3-safe") must not strand an approved card. This matches the production
    # router's operator_gate_terms(title, body) contract.
    term = detect_operator_term(
        str(task.get("title", "")),
        str(task.get("body", "")),
    )
    if term:
        return "operator_gated", term
    if parsed.token_count != 1 or parsed.raw_value not in VALID_VERDICTS:
        return "ambiguous", None
    return "source_docs_spec_test_only", None


def frontend_app_without_verify_pass(task: dict[str, Any], parsed: VerdictParse) -> bool:
    changed_files = [str(path) for path in task.get("changed_files", [])]
    app_path = any(path.startswith("apps/web/") for path in changed_files)
    if not app_path:
        return False
    latest_body = str(parsed.comment.get("body", "")) if parsed.comment else ""
    evidence = "\n".join(
        [
            str(task.get("body", "")),
            str(task.get("block_reason", "")),
            latest_body,
        ]
    )
    for line in evidence.splitlines():
        if "VERIFY_PASS" not in line:
            continue
        if re.search(r"\b(no|missing|without)\b.*\bVERIFY_PASS\b|\bVERIFY_PASS\b.*\b(missing|absent|not present)\b", line, re.I):
            continue
        return False
    return True


def idempotency_key(board: str, task_id: str, comment_id: int | str | None, action: str) -> str:
    return f"verdict-router:{CONTRACT_VERSION}:{board}:{task_id}:comment:{comment_id}:action:{action}"


def comment_without_parseable_verdict(text: str) -> bool:
    return not re.search(r"REVIEW_VERDICT\s*[:=]", text, re.I)


def reference_plan(fixture: dict[str, Any], mode: str = "dry-run") -> dict[str, Any]:
    board = str(fixture.get("board", "test"))
    task = fixture["task"]
    task_id = str(task["id"])
    parsed = parse_latest_verdict(task)
    validation = target_validation(board, task, parsed)
    scope, operator_term = scope_class(task, parsed)
    source_comment_id = parsed.comment.get("id") if parsed.comment else None
    source_author = parsed.comment.get("author") if parsed.comment else None

    plan: dict[str, Any] = {
        "script_version": CONTRACT_VERSION,
        "mode": mode,
        "task_id": task_id,
        "existing_idempotency_keys": list(task.get("existing_idempotency_keys", [])),
        "source_comment_id": source_comment_id,
        "source_author": source_author,
        "verdict_value": parsed.verdict_value if parsed.raw_value in VALID_VERDICTS else parsed.raw_value,
        "target_validation": validation,
        "scope_class": scope if parsed.verdict_value else "unknown",
        "action": "skip",
        "result": "skipped",
        "reason": "no latest parseable non-router verdict",
        "mutations": [],
        "idempotency_key": None,
        "comment": None,
    }

    if str(task.get("status")) != "blocked":
        plan["reason"] = f"task status is {task.get('status')}; only blocked tasks are candidates"
        return plan
    if not parsed.comment or parsed.token_count == 0:
        return plan
    if parsed.token_count != 1 or parsed.raw_value not in VALID_VERDICTS:
        return fail_closed(plan, board, task_id, source_comment_id, "needs_pm", "ambiguous or malformed verdict")
    if validation not in {"same-card"}:
        action = "needs_operator" if scope == "operator_gated" and parsed.verdict_value == "APPROVED" else "needs_pm"
        return fail_closed(plan, board, task_id, source_comment_id, action, f"target validation {validation}")
    if scope == "operator_gated" and parsed.verdict_value == "APPROVED":
        return fail_closed(plan, board, task_id, source_comment_id, "needs_operator", f"operator-gated term: {operator_term}")
    if parsed.verdict_value == "APPROVED" and frontend_app_without_verify_pass(task, parsed):
        return fail_closed(plan, board, task_id, source_comment_id, "needs_pm", "frontend/app work without VERIFY_PASS")

    if parsed.verdict_value == "APPROVED":
        key = idempotency_key(board, task_id, source_comment_id, "complete")
        if key in task.get("existing_idempotency_keys", []):
            plan.update({"action": "skip", "result": "skipped_idempotent", "reason": "idempotency key already present", "idempotency_key": key, "mutations": []})
            return plan
        plan.update(
            {
                "scope_class": scope,
                "action": "complete",
                "result": "would_complete",
                "reason": "same-card approval for source/docs/spec/test-only scope",
                "idempotency_key": key,
                "mutations": ["complete"],
                "metadata": {
                    "router": "deterministic-verdict-router",
                    "source_comment_id": source_comment_id,
                    "source_author": source_author,
                    "verdict": "APPROVED",
                    "idempotency_key": key,
                    "scope_class": scope,
                },
            }
        )
        return plan

    if parsed.verdict_value == "CHANGES_REQUESTED":
        if scope == "operator_gated":
            return fail_closed(plan, board, task_id, source_comment_id, "needs_operator", f"operator-gated term: {operator_term}")
        key = idempotency_key(board, task_id, source_comment_id, "unblock_rework")
        if key in task.get("existing_idempotency_keys", []):
            plan.update({"action": "skip", "result": "skipped_idempotent", "reason": "idempotency key already present", "idempotency_key": key, "mutations": []})
            return plan
        finding = parsed.finding_excerpt or "reviewer requested changes"
        comment = (
            "verdict-router: REWORK_REQUIRED\n"
            f"source_comment_id={source_comment_id} source_author={source_author} verdict_value=CHANGES_REQUESTED\n"
            f"blocking_finding={finding}\n"
            "Address the finding, then block again as review-required when ready.\n"
            f"idempotency_key={key}"
        )
        plan.update(
            {
                "scope_class": scope,
                "action": "unblock_rework",
                "result": "would_unblock",
                "reason": "same-card changes requested verdict; return source worker to rework",
                "idempotency_key": key,
                "mutations": ["comment", "unblock"],
                "comment": comment,
            }
        )
        return plan

    return fail_closed(plan, board, task_id, source_comment_id, "needs_pm", "unhandled verdict")


def fail_closed(plan: dict[str, Any], board: str, task_id: str, comment_id: int | str | None, action: str, reason: str) -> dict[str, Any]:
    key = idempotency_key(board, task_id, comment_id, action)
    failed_scope = "operator_gated" if action == "needs_operator" else "ambiguous"
    if key in plan.get("existing_idempotency_keys", []):
        plan.update({"scope_class": failed_scope, "action": "skip", "result": "skipped_idempotent", "reason": "idempotency key already present", "idempotency_key": key, "mutations": []})
        return plan
    prefix = "NEEDS-OPERATOR: verdict-router operator-gated" if action == "needs_operator" else "NEEDS-PM: verdict-router fail-closed"
    plan.update(
        {
            "scope_class": failed_scope,
            "action": action,
            "result": "would_comment",
            "reason": reason,
            "idempotency_key": key,
            "mutations": ["comment"],
            "comment": f"{prefix}\nsource_comment_id={comment_id} verdict_value={plan.get('verdict_value')} reason={reason}\nidempotency_key={key}",
        }
    )
    return plan


def run_external(command: str, fixture: dict[str, Any]) -> dict[str, Any]:
    proc = subprocess.run(
        command,
        input=json.dumps(fixture),
        text=True,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"router command exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"router command did not print JSON: {proc.stdout!r}") from exc


def write_fixture_board(root: Path, fixture: dict[str, Any]) -> Path:
    """Create a minimal isolated kanban DB for one fixture and return its path."""
    import sqlite3

    board = str(fixture.get("board", "test"))
    task = fixture["task"]
    db = root / "kanban" / "boards" / board / "kanban.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        comment_id_type = "TEXT" if has_nonnumeric_comment_id(fixture) else "INTEGER"
        con.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT,
                assignee TEXT,
                status TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            """
            + f"""
            CREATE TABLE task_comments (
                id {comment_id_type} PRIMARY KEY,
                task_id TEXT NOT NULL,
                author TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        for task in fixture_tasks(fixture):
            con.execute(
                "INSERT INTO tasks(id,title,body,assignee,status,priority,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    str(task["id"]),
                    str(task.get("title", "")),
                    str(task.get("body", "")),
                    str(task.get("assignee", "test-engineer")),
                    str(task.get("status", "blocked")),
                    int(task.get("priority", 0)),
                    int(task.get("created_at", 1783110000)),
                ),
            )
            for comment in task.get("comments", []):
                con.execute(
                    "INSERT INTO task_comments(id, task_id, author, body, created_at) VALUES (?,?,?,?,?)",
                    (
                        str(comment["id"]) if comment_id_type == "TEXT" else int(comment["id"]),
                        str(task["id"]),
                        str(comment.get("author", "")),
                        str(comment.get("body", "")),
                        comment.get("created_at", 0),
                    ),
                )
            next_comment_id = max([safe_sort_int(c.get("id", 0)) for c in task.get("comments", [])] or [0]) + 1
            for key in task.get("existing_idempotency_keys", []):
                con.execute(
                    "INSERT INTO task_comments(id, task_id, author, body, created_at) VALUES (?,?,?,?,?)",
                    (
                        str(next_comment_id) if comment_id_type == "TEXT" else next_comment_id,
                        str(task["id"]),
                        "verdict-router",
                        f"prior verdict-router marker idempotency_key={key}",
                        int(task.get("created_at", 1783110000)) - 1,
                    ),
                )
                next_comment_id += 1
        con.commit()
    finally:
        con.close()
    return db


def run_router_script(script_path: str, fixture: dict[str, Any]) -> dict[str, Any]:
    """Execute verdict_router.py against an isolated temp board, never live DBs."""
    with tempfile.TemporaryDirectory(prefix="verdict-router-harness-") as tmp:
        root = Path(tmp)
        write_fixture_board(root, fixture)
        env = os.environ.copy()
        env.update(
            {
                "VERDICT_ROUTER_ROOT": str(root),
                "VERDICT_ROUTER_BOARDS_DIR": str(root / "kanban" / "boards"),
                "VERDICT_ROUTER_DEFAULT_DB": str(root / "absent-default-kanban.db"),
                "VERDICT_ROUTER_STATE_DIR": str(root / "cron" / "state"),
                "VERDICT_ROUTER_VAULT_ROOT": str(root / "obsidian-fleet-vault"),
                "VERDICT_ROUTER_NOTE_DIR": str(root / "obsidian-fleet-vault" / "Orchestration" / "kanban-verdict-router"),
            }
        )
        proc = subprocess.run(
            [sys.executable, script_path, "--dry-run", "--json"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            env=env,
        )
        if proc.returncode != 0:
            raise AssertionError(f"router script exited {proc.returncode}: {proc.stderr.strip()}")
        try:
            summary = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise AssertionError(f"router script did not print JSON: {proc.stdout!r}") from exc
        plan = script_summary_to_plan(fixture, summary)
        logs = "\n".join(p.read_text(encoding="utf-8") for p in (root / "scripts" / "logs").glob("*.log"))
        plan["router_logs"] = logs
        return plan


def script_summary_to_plan(fixture: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    task = fixture["task"]
    decision = None
    for item in summary.get("decisions", []):
        if str(item.get("task_id")) == str(task["id"]):
            decision = item
            break
    if decision is None:
        return reference_plan(fixture) | {"action": "skip", "result": "skipped", "reason": "router script emitted no decision for fixture"}
    action = str(decision.get("action"))
    result_by_action = {"complete": "would_complete", "unblock_rework": "would_unblock", "needs_operator": "would_comment", "needs_pm": "would_comment"}
    mutations_by_action = {"complete": ["complete"], "unblock_rework": ["comment", "unblock"], "needs_operator": ["comment"], "needs_pm": ["comment"]}
    base = reference_plan(fixture)
    base.update(
        {
            "verdict_value": decision.get("verdict"),
            "scope_class": "operator_gated" if action == "needs_operator" else "ambiguous" if action == "needs_pm" else base.get("scope_class"),
            "action": action,
            "result": decision.get("result") or result_by_action.get(action, "skipped"),
            "reason": decision.get("reason"),
            "idempotency_key": decision.get("idempotency_key"),
            "mutations": mutations_by_action.get(action, []),
            "router_summary": summary,
        }
    )
    if action == "needs_operator":
        base["comment"] = "NEEDS-OPERATOR: verdict-router operator-gated\n" + str(decision.get("reason", ""))
    elif action == "needs_pm":
        base["comment"] = "NEEDS-PM: verdict-router fail-closed\n" + str(decision.get("reason", ""))
    elif action == "unblock_rework":
        base["comment"] = "verdict-router: REWORK_REQUIRED\n" + str(decision.get("reason", ""))
    return base


def fixture_from_local_inputs(
    *,
    board: str,
    task: dict[str, Any] | None = None,
    cards: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
    expect: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a fixture from explicit local card/comment data.

    This is the importable harness boundary used by tests that do not want to
    read fixture files. The first card is the target card; additional cards are
    written only into the isolated temp DB used by router-script mode.
    """
    if task is not None and cards is not None:
        raise ValueError("pass either task or cards, not both")
    local_cards = [dict(task)] if task is not None else [dict(card) for card in (cards or [])]
    if not local_cards:
        raise ValueError("run_harness requires a task or at least one card")
    if comments is not None:
        local_cards[0]["comments"] = [dict(comment) for comment in comments]
    local_cards[0].setdefault("comments", [])
    fixture: dict[str, Any] = {"name": str(local_cards[0].get("id", "local-card")), "board": board, "task": local_cards[0]}
    if len(local_cards) > 1:
        fixture["extra_tasks"] = local_cards[1:]
    if expect is not None:
        fixture["expect"] = expect
    return fixture


def plan_to_structured_result(fixture: dict[str, Any], plan: dict[str, Any] | None, errors: list[str] | None = None) -> dict[str, Any]:
    """Normalize planner/script output into the assertion surface callers need."""
    task = fixture["task"]
    parsed = parse_latest_verdict(task)
    plan = plan or {}
    action = str(plan.get("action") or "skip")
    mutations = list(plan.get("mutations") or [])
    comment_body = plan.get("comment")
    comments = []
    if comment_body:
        comments.append(
            {
                "task_id": str(task.get("id")),
                "body": str(comment_body),
                "idempotency_key": plan.get("idempotency_key"),
                "source_comment_id": plan.get("source_comment_id"),
            }
        )
    completion_actions = []
    if "complete" in mutations or action == "complete":
        completion_actions.append(
            {
                "task_id": str(task.get("id")),
                "summary": plan.get("reason"),
                "metadata": plan.get("metadata", {}),
                "idempotency_key": plan.get("idempotency_key"),
            }
        )
    unblock_actions = []
    if "unblock" in mutations or action == "unblock_rework":
        unblock_actions.append(
            {
                "task_id": str(task.get("id")),
                "reason": comment_body or plan.get("reason"),
                "idempotency_key": plan.get("idempotency_key"),
            }
        )
    ignored_noop_results = []
    if action == "skip" or not mutations:
        ignored_noop_results.append(
            {
                "task_id": str(task.get("id")),
                "action": action,
                "result": plan.get("result", "skipped"),
                "reason": plan.get("reason"),
            }
        )
    return {
        "name": fixture.get("name"),
        "task_id": str(task.get("id")),
        "parsed_verdict": {
            "raw_value": parsed.raw_value,
            "value": plan.get("verdict_value", parsed.verdict_value),
            "token_count": parsed.token_count,
            "source_comment_id": plan.get("source_comment_id", parsed.comment.get("id") if parsed.comment else None),
            "source_author": plan.get("source_author", parsed.comment.get("author") if parsed.comment else None),
            "target_validation": plan.get("target_validation", target_validation(str(fixture.get("board", "test")), task, parsed)),
        },
        "safety_classification": plan.get("scope_class"),
        "planned_mutations": mutations,
        "comments": comments,
        "unblock_actions": unblock_actions,
        "completion_actions": completion_actions,
        "ignored_noop_results": ignored_noop_results,
        "errors": list(errors or []),
        "plan": plan,
    }


def run_harness(
    *,
    board: str,
    task: dict[str, Any] | None = None,
    cards: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
    mode: str = "dry-run",
    router_script: str | None = None,
    router_command: str | None = None,
) -> dict[str, Any]:
    """Run the verdict router harness against local in-memory inputs only.

    `mode="dry-run"` and `mode="mutation-plan"` both record intended effects
    without applying them. `router_script` executes the real production router
    against a temporary fixture DB with all router paths redirected to temp dirs.
    """
    if mode not in {"dry-run", "mutation-plan"}:
        raise ValueError("mode must be 'dry-run' or 'mutation-plan'")
    if router_script and router_command:
        raise ValueError("router_script and router_command are mutually exclusive")
    fixture = fixture_from_local_inputs(board=board, task=task, cards=cards, comments=comments)
    errors: list[str] = []
    implementation = "reference"
    try:
        if router_command:
            implementation = "router-command"
            plan = run_external(router_command, fixture)
        elif router_script:
            implementation = "router-script"
            plan = run_router_script(router_script, fixture)
        else:
            plan = reference_plan(fixture, mode=mode)
    except Exception as exc:
        plan = None
        errors.append(str(exc))
    return {
        "ok": not errors,
        "mode": mode,
        "implementation": implementation,
        "board": board,
        "live_side_effects_possible": False,
        "results": [plan_to_structured_result(fixture, plan, errors)],
        "errors": errors,
    }


def assert_plan(fixture: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    expect = fixture["expect"]
    for field in ("verdict_value", "target_validation", "scope_class", "action", "result"):
        if plan.get(field) != expect.get(field):
            errors.append(f"{field}: expected {expect.get(field)!r}, got {plan.get(field)!r}")
    if not str(plan.get("reason") or "").strip():
        errors.append("structured plan/log entry missing non-empty reason")
    expected_mutations = expect.get("mutations")
    if expected_mutations is not None and plan.get("mutations") != expected_mutations:
        errors.append(f"mutations: expected {expected_mutations!r}, got {plan.get('mutations')!r}")
    for forbidden in expect.get("forbid_mutations", []):
        if forbidden in (plan.get("mutations") or []):
            errors.append(f"forbidden mutation planned: {forbidden}")
    prefix = expect.get("comment_prefix")
    comment = plan.get("comment")
    if prefix and not str(comment or "").startswith(prefix):
        errors.append(f"comment prefix: expected {prefix!r}, got {comment!r}")
    if expect.get("comment_contains") and expect["comment_contains"] not in str(comment or ""):
        errors.append(f"comment missing expected excerpt {expect['comment_contains']!r}")
    if comment and not comment_without_parseable_verdict(str(comment)):
        errors.append("router comment contains parseable REVIEW_VERDICT token")
    if "mode" in expect:
        expected_mode = expect["mode"]
        if plan.get("mode") != expected_mode:
            errors.append(f"mode: expected {expected_mode!r}, got {plan.get('mode')!r}")
    elif plan.get("mode") not in {"dry-run", "mutation-plan"}:
        errors.append(f"mode: expected dry-run or mutation-plan, got {plan.get('mode')!r}")
    action = plan.get("action")
    if action not in {"skip", None} and not plan.get("idempotency_key"):
        errors.append("mutation/comment action missing idempotency_key")
    for expected_log in expect.get("router_log_contains", []):
        if "router_logs" in plan and expected_log not in str(plan.get("router_logs") or ""):
            errors.append(f"router log missing expected excerpt {expected_log!r}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", default=str(Path(__file__).with_name("verdict-router.fixtures.json")))
    parser.add_argument("--router-command", help="External implementation command to verify. Receives fixture JSON on stdin, prints plan JSON.")
    parser.add_argument("--router-script", help="Path to verdict_router.py. Runs it against an isolated temp kanban DB per fixture.")
    parser.add_argument("--mutation-planning", action="store_true", help="Run the reference harness in mutation-planning mode while still avoiding live side effects.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable result JSON")
    args = parser.parse_args(argv)
    if args.router_command and args.router_script:
        parser.error("--router-command and --router-script are mutually exclusive")

    fixtures = json.loads(Path(args.fixtures).read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []
    failures = 0
    for fixture in fixtures:
        try:
            if args.router_command:
                plan = run_external(args.router_command, fixture)
            elif args.router_script:
                plan = run_router_script(args.router_script, fixture)
            else:
                plan = reference_plan(fixture, mode="mutation-plan" if args.mutation_planning else "dry-run")
            errors = assert_plan(fixture, plan)
        except Exception as exc:  # deliberately converted to harness failure output
            plan = None
            errors = [str(exc)]
        ok = not errors
        failures += 0 if ok else 1
        results.append({"name": fixture["name"], "ok": ok, "errors": errors, "plan": plan})
        if not args.json:
            if ok:
                assert plan is not None
                print(f"PASS {fixture['name']}: {plan.get('action')} {plan.get('result')}")
            else:
                print(f"FAIL {fixture['name']}: {'; '.join(errors)}")
    if args.json:
        print(json.dumps({"ok": failures == 0, "passed": len(fixtures) - failures, "failed": failures, "results": results}, indent=2, sort_keys=True))
    else:
        print(f"verdict-router harness {'PASS' if failures == 0 else 'FAIL'}: {len(fixtures) - failures}/{len(fixtures)} fixtures passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
