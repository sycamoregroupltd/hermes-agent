#!/usr/bin/env python3
"""Bounded Sycode blocked-task triage pilot (t_00a73790).

This is intentionally narrow:
- Board scope is sycode-trading only.
- It never reassigns, unblocks, archives, edits status, or changes block_kind.
- Dry-run is the default. With --apply-comments it appends one idempotent
  routing comment per eligible blocked card.
- Frank/A3/capability cards are classified as frank_gate HOLD and are never
  recommended for delegated auto-routing.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BOARDS_DIR = Path("/home/frank/.hermes/kanban/boards")
BOARD = "sycode-trading"
AUTHOR = "sycode-blocked-triage-pilot"
MARKER = "sycode-blocked-triage-pilot:v1:t_00a73790"
DEFAULT_MIN_AGE_HOURS = 24.0

REVIEW_RE = re.compile(
    r"\b(REVIEW_VERDICT\s*[:=]\s*(APPROVED|CHANGES_REQUESTED)|review[- ]required|guardian review|independent review|os-reviewer|trading-risk-reviewer)\b",
    re.I,
)

EXPLICIT_FRANK_GATE_RE = re.compile(
    r"\b(FRANK[-_/ ]?GATE|Frank[- ]gated|needs Frank|requires Frank|pending Frank|DEPLOY[- ]GATED|GUARDIAN[- ]APPLY|critical[- ]list|DB[- ]DDL|DDL gate)\b",
    re.I,
)

POSITIVE_CRITICAL_TITLE_RE = re.compile(
    r"\b(FRANK[-_/ ]?GATE|Frank[- ]gated|A3\b|needs Frank|requires Frank|pending Frank|DEPLOY[- ]GATED|GUARDIAN[- ]APPLY|critical[- ]list|live[-_ ]trading|live mode|production deploy|prod deploy|deploy to prod|credentials?|secrets?|api[-_ ]?keys?|tokens?|oauth|new spend|paid tier|billing|drop table|truncate table|destructive migration|irreversible data|database migration|DB[- ]DDL|DDL)\b",
    re.I,
)

# Denial/boundary prose should not turn an otherwise safe card into a Frank gate
# solely because it says e.g. "no credentials, no live trading".
DENIAL_CUE_RE = re.compile(
    r"\b(no|not|without|do not|don't|never|safe|A3-safe|scope excludes|out of scope|unchanged|preserve[ds]?|read[- ]only|paper[- ]only)\b",
    re.I,
)
SENTENCE_RE = re.compile(r"[^.!?\n]+")


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    body: str
    assignee: str
    status: str
    block_kind: str
    created_at: int
    started_at: int | None
    result: str
    comments: tuple[str, ...]
    runs: tuple[str, ...]

    @property
    def since_epoch(self) -> int:
        return int(self.started_at or self.created_at or 0)

    @property
    def blob(self) -> str:
        return "\n".join(
            [self.title, self.body, self.result, *self.comments[-8:], *self.runs[-5:]]
        )


def board_db(boards_dir: Path = BOARDS_DIR, board: str = BOARD) -> Path:
    if board != BOARD:
        raise ValueError(f"this pilot is scoped to {BOARD!r}, got {board!r}")
    return boards_dir / board / "kanban.db"


def connect(db: Path, *, writable: bool = False) -> sqlite3.Connection:
    mode = "rw" if writable else "ro"
    con = sqlite3.connect(f"file:{db}?mode={mode}", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _rows(con: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    last_exc: sqlite3.OperationalError | None = None
    for _attempt in range(5):
        try:
            return list(con.execute(sql, params).fetchall())
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            last_exc = exc
            time.sleep(0.5)
    if last_exc is not None:
        raise last_exc
    return []


def fetch_blocked_tasks(con: sqlite3.Connection) -> list[Task]:
    rows = _rows(
        con,
        """
        SELECT id, title, COALESCE(body,'') AS body, COALESCE(assignee,'') AS assignee,
               status, COALESCE(block_kind,'') AS block_kind, COALESCE(created_at,0) AS created_at,
               started_at, COALESCE(result,'') AS result
        FROM tasks
        WHERE status='blocked'
        ORDER BY COALESCE(started_at, created_at), id
        """,
        (),
    )
    out: list[Task] = []
    has_comments = table_exists(con, "task_comments")
    has_runs = table_exists(con, "task_runs")
    for r in rows:
        comments: list[str] = []
        if has_comments:
            comments = [
                str(c["body"] or "")
                for c in _rows(
                    con,
                    "SELECT body FROM task_comments WHERE task_id=? ORDER BY created_at ASC",
                    (r["id"],),
                )
            ]
        runs: list[str] = []
        if has_runs:
            runs = [
                " ".join(str(x or "") for x in rr)
                for rr in _rows(
                    con,
                    """
                    SELECT COALESCE(outcome,''), COALESCE(summary,''), COALESCE(error,'')
                    FROM task_runs WHERE task_id=? ORDER BY started_at ASC
                    """,
                    (r["id"],),
                )
            ]
        out.append(
            Task(
                id=str(r["id"]),
                title=str(r["title"] or ""),
                body=str(r["body"] or ""),
                assignee=str(r["assignee"] or ""),
                status=str(r["status"] or ""),
                block_kind=str(r["block_kind"] or ""),
                created_at=int(r["created_at"] or 0),
                started_at=int(r["started_at"]) if r["started_at"] is not None else None,
                result=str(r["result"] or ""),
                comments=tuple(comments),
                runs=tuple(runs),
            )
        )
    return out


def _positive_frank_gate_hit(text: str) -> str | None:
    for sent_match in SENTENCE_RE.finditer(text):
        sent = sent_match.group(0)
        hit = EXPLICIT_FRANK_GATE_RE.search(sent)
        if not hit:
            continue
        if DENIAL_CUE_RE.search(sent) and not re.search(
            r"\b(requires Frank|needs Frank|pending Frank|FRANK[-_/ ]?GATE|Frank[- ]gated|DEPLOY[- ]GATED|GUARDIAN[- ]APPLY)\b",
            sent,
            re.I,
        ):
            continue
        return re.sub(r"\s+", " ", sent.strip())[:220]
    return None


def _positive_critical_title_hit(title: str) -> str | None:
    hit = POSITIVE_CRITICAL_TITLE_RE.search(title)
    if not hit:
        return None
    if DENIAL_CUE_RE.search(title) and not EXPLICIT_FRANK_GATE_RE.search(title):
        return None
    return re.sub(r"\s+", " ", title.strip())[:220]


def classify(task: Task, *, now_epoch: int, min_age_hours: float) -> dict[str, Any] | None:
    age_hours = max(0.0, (now_epoch - task.since_epoch) / 3600.0) if task.since_epoch else 0.0
    is_untriaged = task.block_kind.strip() == ""
    if age_hours < min_age_hours and not is_untriaged:
        return None

    blob = task.blob
    bk = task.block_kind.strip().lower()
    frank_evidence = None
    if bk in {"frank_gate", "capability"}:
        frank_evidence = f"block_kind={task.block_kind}"
    else:
        frank_evidence = _positive_frank_gate_hit(blob) or _positive_critical_title_hit(task.title)

    if frank_evidence:
        route = "frank_gate"
        consumer = "Frank/Jarvis gate batch"
        action = "HOLD: no auto-reassignment/status mutation; PM may batch evidence for Frank/A3 gate."
        evidence = frank_evidence
    elif REVIEW_RE.search(blob):
        route = "reviewer"
        consumer = "os-reviewer or trading-risk-reviewer"
        action = "Reviewer route: PM should inspect existing review verdict/handoff and request/confirm reviewer disposition."
        evidence = re.sub(r"\s+", " ", REVIEW_RE.search(blob).group(0))[:120]  # type: ignore[union-attr]
    else:
        route = "pm"
        consumer = "sycode-trading-pm"
        action = "PM route: inspect blocker, owner, and next safe delegated action; do not cross A3 gates."
        evidence = "default non-critical blocked/stalled card"

    return {
        "task_id": task.id,
        "title": task.title[:140],
        "assignee": task.assignee,
        "block_kind": task.block_kind,
        "age_hours": round(age_hours, 2),
        "untriaged_block_kind": is_untriaged,
        "recommended_route": route,
        "consumer": consumer,
        "recommended_action": action,
        "evidence": evidence,
    }


def comment_body(plan: dict[str, Any]) -> str:
    return (
        f"routing-comment {MARKER}: route={plan['recommended_route']} consumer={plan['consumer']}; "
        f"action={plan['recommended_action']} evidence={plan['evidence']}; "
        "pilot is comment-only: no reassignment, status mutation, block_kind mutation, or Frank/A3 auto-route."
    )


def already_commented(con: sqlite3.Connection, task_id: str) -> bool:
    if not table_exists(con, "task_comments"):
        return False
    return con.execute(
        "SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE ? LIMIT 1",
        (task_id, f"%{MARKER}%"),
    ).fetchone() is not None


def apply_comment(con: sqlite3.Connection, plan: dict[str, Any], *, now_epoch: int) -> str:
    if already_commented(con, str(plan["task_id"])):
        return "already-present"
    body = comment_body(plan)
    con.execute(
        "INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?,?,?,?)",
        (plan["task_id"], AUTHOR, body, now_epoch),
    )
    if table_exists(con, "task_events"):
        payload = json.dumps({"author": AUTHOR, "marker": MARKER, "route": plan["recommended_route"]})
        con.execute(
            "INSERT INTO task_events(task_id, kind, payload, created_at, run_id) VALUES (?,?,?,?,NULL)",
            (plan["task_id"], "commented", payload, now_epoch),
        )
    return "comment-added"


def build_plans(
    *, boards_dir: Path = BOARDS_DIR, min_age_hours: float = DEFAULT_MIN_AGE_HOURS, limit: int | None = None, now_epoch: int | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    now = int(time.time()) if now_epoch is None else now_epoch
    db = board_db(boards_dir, BOARD)
    with connect(db, writable=False) as con:
        tasks = fetch_blocked_tasks(con)
    plans: list[dict[str, Any]] = []
    for task in tasks:
        plan = classify(task, now_epoch=now, min_age_hours=min_age_hours)
        if plan is not None:
            plans.append(plan)
            if limit is not None and len(plans) >= limit:
                break
    metrics = summarize(plans, total_blocked=len(tasks))
    return plans, metrics


def summarize(plans: list[dict[str, Any]], *, total_blocked: int) -> dict[str, Any]:
    by_route: dict[str, int] = {}
    untriaged = 0
    for plan in plans:
        route = str(plan["recommended_route"])
        by_route[route] = by_route.get(route, 0) + 1
        if plan.get("untriaged_block_kind"):
            untriaged += 1
    return {
        "board": BOARD,
        "total_blocked_seen": total_blocked,
        "eligible_plans": len(plans),
        "untriaged_empty_block_kind_plans": untriaged,
        "by_route": by_route,
        "frank_gate_auto_routed": 0,
        "status_mutations": 0,
        "assignee_mutations": 0,
        "block_kind_mutations": 0,
        "marker": MARKER,
    }


def apply_comments(
    plans: list[dict[str, Any]], *, boards_dir: Path = BOARDS_DIR, now_epoch: int | None = None
) -> dict[str, Any]:
    now = int(time.time()) if now_epoch is None else now_epoch
    db = board_db(boards_dir, BOARD)
    counts: dict[str, int] = {}
    with connect(db, writable=True) as con:
        try:
            for plan in plans:
                result = apply_comment(con, plan, now_epoch=now)
                counts[result] = counts.get(result, 0) + 1
            con.commit()
        except Exception:
            con.rollback()
            raise
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", default=BOARD, help="Only sycode-trading is allowed")
    ap.add_argument("--boards-dir", type=Path, default=BOARDS_DIR)
    ap.add_argument("--min-age-hours", type=float, default=DEFAULT_MIN_AGE_HOURS)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--apply-comments", action="store_true", help="append idempotent routing comments; never mutates status/assignee/block_kind")
    ap.add_argument("--json", action="store_true", help="emit full JSON instead of concise text")
    args = ap.parse_args(argv)

    if args.board != BOARD:
        print(f"ERROR: this pilot is scoped to {BOARD!r}; refusing board={args.board!r}", file=sys.stderr)
        return 2

    plans, metrics = build_plans(
        boards_dir=args.boards_dir,
        min_age_hours=args.min_age_hours,
        limit=args.limit,
    )
    apply_result = {"dry_run": True}
    if args.apply_comments:
        apply_result = apply_comments(plans, boards_dir=args.boards_dir)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply-comments" if args.apply_comments else "dry-run",
        "metrics": metrics,
        "apply_result": apply_result,
        "plans": plans,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"SYCODE_BLOCKED_TRIAGE_PILOT mode={payload['mode']} board={BOARD} "
            f"eligible={metrics['eligible_plans']} routes={json.dumps(metrics['by_route'], sort_keys=True)} "
            f"comments={json.dumps(apply_result, sort_keys=True)}"
        )
        for plan in plans[:20]:
            print(
                f"- {plan['task_id']} route={plan['recommended_route']} assignee={plan['assignee']} "
                f"block_kind={plan['block_kind'] or '(empty)'} age_h={plan['age_hours']} title={plan['title']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
