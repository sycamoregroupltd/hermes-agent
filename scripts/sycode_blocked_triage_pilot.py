#!/usr/bin/env python3
"""Bounded blocked-task triage pilot (t_00a73790, multi-board since t_6240a616).

This is intentionally narrow:
- Board scope is an explicit allowlist (sycode-trading, jarvis-os).
- It never reassigns, unblocks, archives, edits status, or changes block_kind.
- Dry-run is the default. With --apply-comments it appends one idempotent
  routing comment per eligible blocked card.
- Frank/A3/capability cards are classified as frank_gate HOLD and are never
  recommended for delegated auto-routing.

Each board carries its own idempotency marker, so the already-applied
sycode-trading v1 comments are never re-applied or collided with when the
same classifier runs against another board.
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

# Board allowlist. Each entry pins the PM consumer and the idempotency marker.
# The sycode-trading marker is frozen at its original v1 value so the comments
# already applied by the pilot are recognised and never duplicated.
BOARD_POLICY: dict[str, dict[str, str]] = {
    "sycode-trading": {
        "pm": "sycode-trading-pm",
        "marker": "sycode-blocked-triage-pilot:v1:t_00a73790",
    },
    "jarvis-os": {
        "pm": "jarvis-os-pm",
        "marker": "jarvis-os-blocked-triage:v1:t_9377b6f0",
    },
}


def marker_for(board: str) -> str:
    """Idempotency marker for a board. Distinct per board by construction."""
    policy = BOARD_POLICY.get(board)
    if policy is None:
        raise ValueError(
            f"board {board!r} is not in the triage allowlist {sorted(BOARD_POLICY)}"
        )
    return policy["marker"]


def pm_consumer_for(board: str) -> str:
    policy = BOARD_POLICY.get(board)
    if policy is None:
        raise ValueError(
            f"board {board!r} is not in the triage allowlist {sorted(BOARD_POLICY)}"
        )
    return policy["pm"]


def age_bucket(age_hours: float) -> str:
    """Coarse age bucket for the machine-readable AGE_BUCKET field."""
    if age_hours < 24.0:
        return "<24h"
    if age_hours < 24.0 * 7:
        return "1-7d"
    if age_hours < 24.0 * 30:
        return "7-30d"
    return ">30d"


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
    if board not in BOARD_POLICY:
        raise ValueError(
            f"this pilot is scoped to {sorted(BOARD_POLICY)}, got {board!r}"
        )
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


def classify(
    task: Task, *, now_epoch: int, min_age_hours: float, board: str = BOARD
) -> dict[str, Any] | None:
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
        resume_gate = "frank-approval"
        evidence = frank_evidence
    elif REVIEW_RE.search(blob):
        route = "reviewer"
        consumer = "os-reviewer or trading-risk-reviewer"
        action = "Reviewer route: PM should inspect existing review verdict/handoff and request/confirm reviewer disposition."
        resume_gate = "reviewer-verdict"
        evidence = re.sub(r"\s+", " ", REVIEW_RE.search(blob).group(0))[:120]  # type: ignore[union-attr]
    else:
        route = "pm"
        consumer = pm_consumer_for(board)
        action = "PM route: inspect blocker, owner, and next safe delegated action; do not cross A3 gates."
        resume_gate = "pm-disposition"
        evidence = "default non-critical blocked/stalled card"

    return {
        "task_id": task.id,
        "board": board,
        "title": task.title[:140],
        "assignee": task.assignee,
        "block_kind": task.block_kind,
        "age_hours": round(age_hours, 2),
        "age_bucket": age_bucket(age_hours),
        "untriaged_block_kind": is_untriaged,
        "recommended_route": route,
        "consumer": consumer,
        "recommended_action": action,
        "resume_gate": resume_gate,
        "evidence": evidence,
    }


def comment_body(plan: dict[str, Any], *, board: str = BOARD) -> str:
    return (
        f"routing-comment {marker_for(board)}: "
        f"BLOCK_KIND={plan['block_kind'] or '(empty)'} "
        f"RESUME_GATE={plan['resume_gate']} "
        f"AGE_BUCKET={plan['age_bucket']} "
        f"route={plan['recommended_route']} consumer={plan['consumer']}; "
        f"action={plan['recommended_action']} evidence={plan['evidence']}; "
        "pilot is comment-only: no reassignment, status mutation, block_kind mutation, or Frank/A3 auto-route."
    )


def already_commented(con: sqlite3.Connection, task_id: str, *, board: str = BOARD) -> bool:
    if not table_exists(con, "task_comments"):
        return False
    return con.execute(
        "SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE ? LIMIT 1",
        (task_id, f"%{marker_for(board)}%"),
    ).fetchone() is not None


def apply_comment(
    con: sqlite3.Connection, plan: dict[str, Any], *, now_epoch: int, board: str = BOARD
) -> str:
    if already_commented(con, str(plan["task_id"]), board=board):
        return "already-present"
    body = comment_body(plan, board=board)
    con.execute(
        "INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?,?,?,?)",
        (plan["task_id"], AUTHOR, body, now_epoch),
    )
    if table_exists(con, "task_events"):
        payload = json.dumps(
            {"author": AUTHOR, "marker": marker_for(board), "route": plan["recommended_route"]}
        )
        con.execute(
            "INSERT INTO task_events(task_id, kind, payload, created_at, run_id) VALUES (?,?,?,?,NULL)",
            (plan["task_id"], "commented", payload, now_epoch),
        )
    return "comment-added"


def build_plans(
    *,
    boards_dir: Path = BOARDS_DIR,
    board: str = BOARD,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    limit: int | None = None,
    now_epoch: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    now = int(time.time()) if now_epoch is None else now_epoch
    db = board_db(boards_dir, board)
    with connect(db, writable=False) as con:
        tasks = fetch_blocked_tasks(con)
    plans: list[dict[str, Any]] = []
    for task in tasks:
        plan = classify(task, now_epoch=now, min_age_hours=min_age_hours, board=board)
        if plan is not None:
            plans.append(plan)
            if limit is not None and len(plans) >= limit:
                break
    metrics = summarize(plans, total_blocked=len(tasks), board=board)
    return plans, metrics


def summarize(
    plans: list[dict[str, Any]], *, total_blocked: int, board: str = BOARD
) -> dict[str, Any]:
    by_route: dict[str, int] = {}
    by_age_bucket: dict[str, int] = {}
    untriaged = 0
    for plan in plans:
        route = str(plan["recommended_route"])
        by_route[route] = by_route.get(route, 0) + 1
        bucket = str(plan.get("age_bucket", "unknown"))
        by_age_bucket[bucket] = by_age_bucket.get(bucket, 0) + 1
        if plan.get("untriaged_block_kind"):
            untriaged += 1
    return {
        "board": board,
        "total_blocked_seen": total_blocked,
        "eligible_plans": len(plans),
        "untriaged_empty_block_kind_plans": untriaged,
        "by_route": by_route,
        "by_age_bucket": by_age_bucket,
        "frank_gate_auto_routed": 0,
        "status_mutations": 0,
        "assignee_mutations": 0,
        "block_kind_mutations": 0,
        "marker": marker_for(board),
    }


def apply_comments(
    plans: list[dict[str, Any]],
    *,
    boards_dir: Path = BOARDS_DIR,
    board: str = BOARD,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    now = int(time.time()) if now_epoch is None else now_epoch
    db = board_db(boards_dir, board)
    counts: dict[str, int] = {}
    with connect(db, writable=True) as con:
        try:
            for plan in plans:
                result = apply_comment(con, plan, now_epoch=now, board=board)
                counts[result] = counts.get(result, 0) + 1
            con.commit()
        except Exception:
            con.rollback()
            raise
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--board",
        default=BOARD,
        choices=sorted(BOARD_POLICY),
        help="board to triage (allowlisted only)",
    )
    ap.add_argument("--boards-dir", type=Path, default=BOARDS_DIR)
    ap.add_argument("--min-age-hours", type=float, default=DEFAULT_MIN_AGE_HOURS)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--apply-comments", action="store_true", help="append idempotent routing comments; never mutates status/assignee/block_kind")
    ap.add_argument("--json", action="store_true", help="emit full JSON instead of concise text")
    args = ap.parse_args(argv)

    if args.board not in BOARD_POLICY:
        print(
            f"ERROR: this pilot is scoped to {sorted(BOARD_POLICY)}; refusing board={args.board!r}",
            file=sys.stderr,
        )
        return 2

    plans, metrics = build_plans(
        boards_dir=args.boards_dir,
        board=args.board,
        min_age_hours=args.min_age_hours,
        limit=args.limit,
    )
    apply_result = {"dry_run": True}
    if args.apply_comments:
        apply_result = apply_comments(plans, boards_dir=args.boards_dir, board=args.board)
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
            f"BLOCKED_TRIAGE_PILOT mode={payload['mode']} board={args.board} "
            f"eligible={metrics['eligible_plans']} routes={json.dumps(metrics['by_route'], sort_keys=True)} "
            f"ages={json.dumps(metrics['by_age_bucket'], sort_keys=True)} "
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
