#!/usr/bin/env python3
"""Deterministic kanban REVIEW_VERDICT router.

Dry-run by default. In dry-run it only appends shadow decisions to the fleet
Obsidian vault. Mutations require either VERDICT_ROUTER_APPLY=1 or the sentinel
/home/frank/.hermes/cron/state/verdict-router.apply-enabled.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(os.environ.get("VERDICT_ROUTER_ROOT", "/home/frank/.hermes"))
BOARDS_DIR = Path(os.environ.get("VERDICT_ROUTER_BOARDS_DIR", str(ROOT / "kanban" / "boards")))
DEFAULT_DB = Path(os.environ.get("VERDICT_ROUTER_DEFAULT_DB", str(ROOT / "kanban.db")))
STATE_DIR = Path(os.environ.get("VERDICT_ROUTER_STATE_DIR", str(ROOT / "cron" / "state")))
ENABLE_SENTINEL = STATE_DIR / "verdict-router.apply-enabled"
LOG_DIR = ROOT / "scripts" / "logs"
VAULT_ROOT = Path(os.environ.get("VERDICT_ROUTER_VAULT_ROOT", "/home/frank/obsidian-fleet-vault"))
VAULT_NOTE_DIR = Path(os.environ.get("VERDICT_ROUTER_NOTE_DIR", str(VAULT_ROOT / "Orchestration" / "kanban-verdict-router")))
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
AUTHOR = os.environ.get("VERDICT_ROUTER_AUTHOR", "verdict-router")
LOCK_PATH = STATE_DIR / "verdict-router.lock"
SCRIPT_VERSION = "v1"

VERDICT_RE = re.compile(r"\bREVIEW_VERDICT\s*[:=]\s*([A-Z0-9_]+)", re.I)
TASK_ID_RE = re.compile(r"\bt_[0-9a-f]{8,}\b", re.I)
ROUTER_AUTHORS = {AUTHOR, "cron:deterministic-verdict-router"}
VALID_VERDICTS = {"APPROVE", "APPROVED", "CHANGES_REQUESTED"}

FORBIDDEN_SCOPE_RE = re.compile(
    r"\b("
    r"deploy(?:ment)?|prod(?:uction)?|runtime|go[- ]?live|live[-_ ]?(?:mode|trading|capped)|"
    r"gateway\s+restart|service\s+restart|cron\s+activation|apply\s+sentinel|"
    r"database|\bdb\b|migration|schema|seed|delete|drop\s+table|truncate|mass\s+delete|irreversible|"
    r"credential|secret|token|api[-_ ]?key|auth|payment|pricing|checkout|refund|money|spend|billing|"
    r"a3|operator\s+approval|maintainer\s+approval|frank\s+approval|push/merge-to-trunk|workforce-scaler"
    r")\b",
    re.I,
)
SAFE_DELIVERABLE_RE = re.compile(
    r"\b(source|code|patch|diff|docs?|documentation|spec|test(?:s|ing)?|fixture|lint|typecheck|unit|build)\b",
    re.I,
)
REVIEW_REQUIRED_RE = re.compile(r"\breview[-_ ]required\b", re.I)
FRONTEND_APP_RE = re.compile(r"\b(apps/web/|frontend|page\.tsx|layout\.tsx|component|middleware|trpc)\b", re.I)
VERIFY_PASS_RE = re.compile(r"\bVERIFY_PASS\b")
NEGATED_VERIFY_PASS_RE = re.compile(r"\b(no|missing|without|absent)\b.*\bVERIFY_PASS\b|\bVERIFY_PASS\b.*\b(missing|absent|not\s+present)\b", re.I)


@dataclass(frozen=True)
class Board:
    slug: str
    db: Path


@dataclass(frozen=True)
class Candidate:
    board: Board
    task_id: str
    title: str
    body: str
    assignee: str | None
    latest_comment_id: int
    latest_comment_author: str
    latest_comment_body: str
    latest_comment_created_at: int


@dataclass(frozen=True)
class Decision:
    board: str
    task_id: str
    comment_id: int
    verdict: str | None
    action: str
    reason: str
    dry_run: bool
    source_author: str
    target_validation: str
    scope_class: str
    result: str
    idempotency_action: str | None = None

    @property
    def idempotency_key(self) -> str:
        action = self.idempotency_action or self.action
        return f"verdict-router:v1:{self.board}:{self.task_id}:comment:{self.comment_id}:action:{action}"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_note() -> Path:
    return VAULT_NOTE_DIR / f"{dt.datetime.now(dt.timezone.utc).date().isoformat()}-verdict-router-shadow-log.md"


def ensure_note(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    path.write_text(
        "---\n"
        f"created: {utc_now()}\n"
        "tags: [hermes, kanban, verdict-router, shadow-log]\n"
        "---\n"
        "# Kanban Verdict Router Shadow Log\n\n"
        "Logs deterministic decisions by `verdict_router.py` for REVIEW_VERDICT comments. "
        "Dry-run mode is the default until os-reviewer approves mutation enablement.\n\n"
        "Related: [[Learnings/2026-07-03 verdict-routing gap]]; kanban task t_2afc2c67.\n\n"
        "## Entries\n",
        encoding="utf-8",
    )


def append_note(entry: dict) -> None:
    note = today_note()
    ensure_note(note)
    with note.open("a", encoding="utf-8") as f:
        f.write(
            "\n"
            f"### {entry['timestamp']} — {entry['board']}/{entry['task_id']}\n"
            f"- script_version: `{SCRIPT_VERSION}`\n"
            f"- mode: `{entry['mode']}`\n"
            f"- action: `{entry['action']}`\n"
            f"- verdict_value: `{entry.get('verdict_value', entry.get('verdict'))}`\n"
            f"- source_comment_id: `{entry.get('source_comment_id', entry.get('comment_id'))}`\n"
            f"- source_author: `{entry.get('source_author', '')}`\n"
            f"- target_validation: `{entry.get('target_validation', 'not-applicable')}`\n"
            f"- scope_class: `{entry.get('scope_class', 'unknown')}`\n"
            f"- result: `{entry.get('result', 'skipped')}`\n"
            f"- idempotency_key: `{entry['idempotency_key']}`\n"
            f"- reason: {entry['reason']}\n"
        )
        if entry.get("command"):
            f.write(f"- command: `{entry['command']}`\n")
        if entry.get("stdout"):
            f.write(f"- stdout: `{entry['stdout'][:500]}`\n")
        if entry.get("stderr"):
            f.write(f"- stderr: `{entry['stderr'][:500]}`\n")


def append_run_log(line: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / f"verdict-router_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d')}.log").open("a", encoding="utf-8") as f:
        f.write(f"[{utc_now()}] {line}\n")


def boards() -> list[Board]:
    found: list[Board] = []
    if DEFAULT_DB.exists():
        found.append(Board("default", DEFAULT_DB))
    if BOARDS_DIR.exists():
        for db in sorted(BOARDS_DIR.glob("*/kanban.db")):
            if db.parent.name.startswith("_"):
                continue
            found.append(Board(db.parent.name, db))
    return found


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def latest_comment(con: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    if not table_exists(con, "task_comments"):
        return None
    placeholders = ",".join("?" for _ in ROUTER_AUTHORS)
    return con.execute(
        f"""
        SELECT id, author, body, created_at
          FROM task_comments
         WHERE task_id=?
           AND COALESCE(author, '') NOT IN ({placeholders})
         ORDER BY COALESCE(created_at, 0) DESC, id DESC
         LIMIT 1
        """,
        (task_id, *sorted(ROUTER_AUTHORS)),
    ).fetchone()


def prior_router_marker(con: sqlite3.Connection, task_id: str, marker: str) -> bool:
    if not table_exists(con, "task_comments"):
        return False
    row = con.execute(
        "SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE ? LIMIT 1",
        (task_id, f"%{marker}%"),
    ).fetchone()
    if row is not None:
        return True
    legacy_marker = marker.replace("verdict-router:v1:", "verdict-router:", 1)
    if legacy_marker != marker:
        row = con.execute(
            "SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE ? LIMIT 1",
            (task_id, f"%{legacy_marker}%"),
        ).fetchone()
        if row is not None:
            return True
    return False


def candidates_for_board(board: Board) -> list[Candidate]:
    con = sqlite3.connect(str(board.db))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT id, title, body, assignee
              FROM tasks
             WHERE status = 'blocked'
             ORDER BY priority DESC, created_at ASC
            """
        ).fetchall()
        out: list[Candidate] = []
        for row in rows:
            comment = latest_comment(con, row["id"])
            if comment is None:
                continue
            body = comment["body"] or ""
            if not VERDICT_RE.search(body):
                continue
            task_text = "\n".join([row["title"] or "", row["body"] or ""])
            if not (REVIEW_REQUIRED_RE.search(task_text) or REVIEW_REQUIRED_RE.search(body) or VERDICT_RE.search(body)):
                continue
            out.append(
                Candidate(
                    board=board,
                    task_id=row["id"],
                    title=row["title"] or "",
                    body=row["body"] or "",
                    assignee=row["assignee"],
                    latest_comment_id=int(comment["id"]),
                    latest_comment_author=comment["author"] or "",
                    latest_comment_body=body,
                    latest_comment_created_at=int(comment["created_at"] or 0),
                )
            )
        return out
    finally:
        con.close()


def parse_verdict(comment_body: str) -> str | None:
    matches = list(VERDICT_RE.finditer(comment_body))
    if len(matches) != 1:
        return None
    raw = matches[0].group(1).strip().upper()
    if raw == "APPROVE":
        return "APPROVED"
    if raw in VALID_VERDICTS:
        return raw
    return raw[:80] or "AMBIGUOUS"


def task_ids_in_comment(candidate: Candidate) -> list[str]:
    return [m.group(0) for m in TASK_ID_RE.finditer(candidate.latest_comment_body)]


def target_validation(candidate: Candidate, verdict: str | None) -> str:
    if verdict is None:
        return "not-applicable"
    ids = task_ids_in_comment(candidate)
    unique = set(ids)
    if verdict == "CHANGES_REQUESTED" and not unique:
        return "same-card"
    if not unique:
        return "missing-target"
    if len(unique) > 1:
        return "multi-target"
    return "same-card" if candidate.task_id in unique else "cross-target"


def comment_mentions_other_card(candidate: Candidate) -> bool:
    ids = set(task_ids_in_comment(candidate))
    return bool(ids and (ids - {candidate.task_id}))


def first_finding_excerpt(comment_body: str) -> str:
    for line in comment_body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("REVIEW_VERDICT") or stripped.lower().startswith("target"):
            continue
        if re.search(r"finding|block|fail", stripped, re.I):
            return stripped[:240]
    for line in comment_body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.upper().startswith("REVIEW_VERDICT"):
            return stripped[:240]
    return "reviewer requested changes"


def classify_scope(candidate: Candidate, verdict: str | None) -> tuple[str, str]:
    combined = "\n".join([candidate.title, candidate.body, candidate.latest_comment_body])
    forbidden = FORBIDDEN_SCOPE_RE.search(combined)
    if forbidden:
        return "operator_gated", f"operator-gated term: {forbidden.group(0)!r}"
    if verdict not in {"APPROVED", "CHANGES_REQUESTED"}:
        return "ambiguous", "unrecognized or ambiguous REVIEW_VERDICT value"
    if not SAFE_DELIVERABLE_RE.search(combined):
        return "ambiguous", "no source/docs/spec/test deliverable marker found"
    return "source_docs_spec_test_only", "source/docs/spec/test scope markers and no operator-gated keywords"


def frontend_app_without_verify_pass(candidate: Candidate) -> bool:
    combined = "\n".join([candidate.title, candidate.body, candidate.latest_comment_body])
    if not FRONTEND_APP_RE.search(combined):
        return False
    for line in combined.splitlines():
        if VERIFY_PASS_RE.search(line) and not NEGATED_VERIFY_PASS_RE.search(line):
            return False
    return True


def make_decision(candidate: Candidate, verdict: str | None, action: str, reason: str, dry_run: bool, validation: str, scope: str, result: str) -> Decision:
    return Decision(
        candidate.board.slug,
        candidate.task_id,
        candidate.latest_comment_id,
        verdict,
        action,
        reason,
        dry_run,
        candidate.latest_comment_author,
        validation,
        scope,
        result,
    )


def mark_idempotent(decision: Decision) -> Decision:
    return Decision(
        decision.board,
        decision.task_id,
        decision.comment_id,
        decision.verdict,
        "skip",
        "idempotency key already present",
        decision.dry_run,
        decision.source_author,
        decision.target_validation,
        decision.scope_class,
        "skipped_idempotent",
        decision.action,
    )


def decide(candidate: Candidate, dry_run: bool) -> Decision:
    verdict = parse_verdict(candidate.latest_comment_body)
    validation = target_validation(candidate, verdict)
    scope, scope_reason = classify_scope(candidate, verdict)

    if verdict == "APPROVED":
        if scope == "operator_gated":
            return make_decision(candidate, verdict, "needs_operator", scope_reason, dry_run, validation, scope, "would_comment")
        if validation != "same-card":
            return make_decision(candidate, verdict, "needs_pm", f"target validation {validation}", dry_run, validation, "ambiguous", "would_comment")
        if frontend_app_without_verify_pass(candidate):
            return make_decision(candidate, verdict, "needs_pm", "frontend/app work without VERIFY_PASS", dry_run, validation, "ambiguous", "would_comment")
        if scope != "source_docs_spec_test_only":
            return make_decision(candidate, verdict, "needs_pm", scope_reason, dry_run, validation, scope, "would_comment")
        return make_decision(candidate, verdict, "complete", "same-card approval for source/docs/spec/test-only scope", dry_run, validation, scope, "would_complete")

    if verdict == "CHANGES_REQUESTED":
        if scope == "operator_gated":
            return make_decision(candidate, verdict, "needs_operator", scope_reason, dry_run, validation, scope, "would_comment")
        if validation not in {"same-card"} or comment_mentions_other_card(candidate):
            return make_decision(candidate, verdict, "needs_pm", f"target validation {validation}", dry_run, validation, "ambiguous", "would_comment")
        finding = first_finding_excerpt(candidate.latest_comment_body)
        return make_decision(candidate, verdict, "unblock_rework", f"same-card changes requested verdict; blocking finding: {finding}", dry_run, validation, scope, "would_unblock")

    return make_decision(candidate, verdict, "needs_pm", "ambiguous or malformed verdict", dry_run, validation, "ambiguous", "would_comment")


def shell_cmd(args: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)


def run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("HERMES_PROFILE", AUTHOR)
    env.setdefault("HERMES_PROFILE_NAME", AUTHOR)
    return subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)


def board_cli_prefix(board: str) -> list[str]:
    return [HERMES_BIN, "kanban", "--board", board]


def add_comment(board: str, task_id: str, body: str) -> subprocess.CompletedProcess[str]:
    return run_cli(board_cli_prefix(board) + ["comment", task_id, body, "--author", AUTHOR])


def perform(decision: Decision, candidate: Candidate) -> tuple[str | None, subprocess.CompletedProcess[str] | None]:
    if decision.action == "skip":
        return "idempotent-skip", None
    marker = decision.idempotency_key
    con = sqlite3.connect(str(candidate.board.db))
    con.row_factory = sqlite3.Row
    try:
        if prior_router_marker(con, candidate.task_id, marker):
            return "already-marked", None
    finally:
        con.close()

    evidence = (
        f"verdict-router marker={marker}\n"
        f"verdict_value={decision.verdict}; latest_comment_id={decision.comment_id}; "
        f"review_comment_author={candidate.latest_comment_author}; reason={decision.reason}\n"
    )
    if decision.action == "complete":
        summary = (
            f"ACCEPTED by deterministic verdict-router: REVIEW_VERDICT={decision.verdict} "
            f"on comment {decision.comment_id}; {decision.reason}."
        )
        metadata = json.dumps(
            {
                "accepted_by": AUTHOR,
                "review_comment_id": decision.comment_id,
                "review_comment_author": candidate.latest_comment_author,
                "verdict": decision.verdict,
                "idempotency_key": marker,
                "safety_scope": decision.reason,
            },
            sort_keys=True,
        )
        args = board_cli_prefix(decision.board) + ["complete", decision.task_id, "--summary", summary, "--metadata", metadata]
        return shell_cmd(args), run_cli(args)
    if decision.action == "unblock_rework":
        finding = first_finding_excerpt(candidate.latest_comment_body)
        comment = (
            "verdict-router: REWORK_REQUIRED\n"
            f"source_comment_id={decision.comment_id} source_author={candidate.latest_comment_author} verdict_value=CHANGES_REQUESTED\n"
            f"blocking_finding={finding}\n"
            "Address the finding, then block again as review-required when ready.\n"
            f"idempotency_key={marker}"
        )
        args = board_cli_prefix(decision.board) + ["unblock", decision.task_id, "--reason", comment]
        return shell_cmd(args), run_cli(args)
    if decision.action == "needs_operator":
        comment = (
            "NEEDS-OPERATOR: verdict-router refused to auto-complete this approved card.\n"
            f"{evidence}"
            "Reason: scope appears deploy/runtime/DB/live/A3/operator-gated or otherwise outside source/docs/spec auto-complete."
        )
        args = board_cli_prefix(decision.board) + ["comment", decision.task_id, comment, "--author", AUTHOR]
        return shell_cmd(args), run_cli(args)
    comment = (
        "NEEDS-PM: verdict-router left this review verdict blocked for manual routing.\n"
        f"{evidence}"
        "Reason: ambiguous verdict or target/scope failed closed."
    )
    args = board_cli_prefix(decision.board) + ["comment", decision.task_id, comment, "--author", AUTHOR]
    return shell_cmd(args), run_cli(args)


def acquire_lock() -> int | None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, f"{os.getpid()} {utc_now()}\n".encode("utf-8"))
        return fd
    except FileExistsError:
        try:
            age = time.time() - LOCK_PATH.stat().st_mtime
        except OSError:
            age = 0
        if age > 900:
            LOCK_PATH.unlink(missing_ok=True)
            return acquire_lock()
        return None


def release_lock(fd: int | None) -> None:
    if fd is not None:
        os.close(fd)
        LOCK_PATH.unlink(missing_ok=True)


def apply_enabled(cli_apply: bool, cli_dry_run: bool) -> bool:
    if cli_dry_run:
        return False
    if cli_apply:
        return True
    return os.environ.get("VERDICT_ROUTER_APPLY") == "1" or ENABLE_SENTINEL.exists()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Route latest kanban REVIEW_VERDICT comments deterministically")
    parser.add_argument("--apply", action="store_true", help="Mutate board state; otherwise dry-run unless sentinel/env enables apply")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run even if sentinel/env enables apply")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    args = parser.parse_args(list(argv) if argv is not None else None)

    fd = acquire_lock()
    if fd is None:
        append_run_log("lock-held; skipping")
        return 0
    try:
        dry_run = not apply_enabled(args.apply, args.dry_run)
        mode = "dry-run" if dry_run else "apply"
        decisions: list[Decision] = []
        failures: list[dict] = []
        for board in boards():
            try:
                cands = candidates_for_board(board)
            except Exception as exc:
                failures.append({"board": board.slug, "error": str(exc)})
                append_note(
                    {
                        "timestamp": utc_now(),
                        "mode": mode,
                        "board": board.slug,
                        "task_id": "__board_scan_error__",
                        "action": "scan_error",
                        "verdict": "ERROR",
                        "comment_id": 0,
                        "idempotency_key": f"verdict-router:{board.slug}:scan-error:{int(time.time())}",
                        "reason": str(exc),
                    }
                )
                continue
            for cand in cands:
                decision = decide(cand, dry_run)
                con = sqlite3.connect(str(cand.board.db))
                try:
                    if prior_router_marker(con, cand.task_id, decision.idempotency_key):
                        decision = mark_idempotent(decision)
                finally:
                    con.close()
                decisions.append(decision)
                entry = {
                    "timestamp": utc_now(),
                    "mode": mode,
                    "board": decision.board,
                    "task_id": decision.task_id,
                    "action": decision.action,
                    "verdict": decision.verdict,
                    "verdict_value": decision.verdict,
                    "comment_id": decision.comment_id,
                    "source_comment_id": decision.comment_id,
                    "source_author": decision.source_author,
                    "target_validation": decision.target_validation,
                    "scope_class": decision.scope_class,
                    "result": decision.result,
                    "idempotency_key": decision.idempotency_key,
                    "reason": decision.reason,
                }
                if dry_run:
                    append_note(entry)
                    continue
                command, proc = perform(decision, cand)
                entry["command"] = command or "idempotent-skip"
                if proc is not None:
                    entry["stdout"] = (proc.stdout or "").strip()
                    entry["stderr"] = (proc.stderr or "").strip()
                    if proc.returncode != 0:
                        entry["action"] = f"{decision.action}_failed"
                        failures.append({"board": decision.board, "task_id": decision.task_id, "rc": proc.returncode, "stderr": proc.stderr})
                append_note(entry)

        summary = {
            "mode": mode,
            "boards_scanned": len(boards()),
            "decisions": [d.__dict__ | {"idempotency_key": d.idempotency_key} for d in decisions],
            "failures": failures,
            "note": str(today_note()),
        }
        append_run_log(json.dumps({"mode": mode, "decisions": len(decisions), "failures": len(failures), "note": str(today_note())}, sort_keys=True))
        if args.json:
            print(json.dumps(summary, sort_keys=True))
        elif failures:
            print(f"verdict-router: {len(failures)} failure(s); note={today_note()}")
        elif decisions:
            print(f"verdict-router: {len(decisions)} {mode} decision(s); note={today_note()}")
        # Empty stdout means silent no-op for no-agent cron.
        return 1 if failures else 0
    finally:
        release_lock(fd)


if __name__ == "__main__":
    raise SystemExit(main())
