#!/usr/bin/env python3
"""Deterministic Pending-Frank triage and orphan-router report.

Default mode is report-only. With --apply, the script only adds idempotent
`delegated:` comments to tasks classified delegated-review; it never unblocks,
reassigns, dispatches, archives, or mutates critical-list / ambiguous tasks.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3  # type-only: sqlite3.Connection annotations
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from hermes_cli import kanban_db as kb

BOARDS_DIR = Path(
    os.environ.get("KANBAN_BOARDS_DIR", "/home/frank/.hermes/kanban/boards")
)
DEFAULT_STATUS = Path(
    os.environ.get("FLEET_STATUS", "/home/frank/uaa-rules/FLEET-STATUS.md")
)
AUTHOR = os.environ.get("PENDING_FRANK_TRIAGE_AUTHOR", "pending-frank-triage")

APPROVAL_SCAN_SCRIPT = Path(
    os.environ.get(
        "APPROVAL_BLOCKER_SCAN",
        "/home/frank/obsidian-fleet-vault/Orchestration/sessions/bin/approval-blocker-scan.py",
    )
)

CRITICAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "explicit-frank-gate",
        re.compile(r"\bFrank[- ]gated\b", re.I),
    ),
    (
        "credential-approval-gap",
        re.compile(r"\b(unauthorized .*MCP|secret reference|no matching approval)\b", re.I),
    ),
    (
        "real-money/payment",
        re.compile(
            r"\b(real[- ]?money|payment flow|payments?|charge[s]?|billing|invoice|refund|stripe|checkout)\b",
            re.I,
        ),
    ),
    (
        "live-trading",
        re.compile(
            r"\b(live[-_ ]?trading|trade_intents?|live_capped|live mode|place orders?|order execution|broker|exchange)\b",
            re.I,
        ),
    ),
    (
        "credentials/secrets",
        re.compile(
            r"\b(credentials?|secrets?|api[-_ ]?keys?|tokens?|oauth|auth token|rotate|rotation|copying? secrets?|deleting? secrets?|creating? credentials?)\b",
            re.I,
        ),
    ),
    (
        "production-deploy",
        re.compile(
            r"\b(prod(uction)? deploy|deploy to prod|user[- ]facing .*live|go live|release to production)\b",
            re.I,
        ),
    ),
    (
        "irreversible-data",
        re.compile(
            r"\b(drop table|drop database|mass delete|truncate table|destructive migration|schema[- ]destructive|irreversible data)\b",
            re.I,
        ),
    ),
    (
        "new-spend",
        re.compile(
            r"\b(new spend|spending commitment|subscription|paid tier|api tier|increase[d]? concurrency|cost raise|buy(ing)?|purchase)\b",
            re.I,
        ),
    ),
]

DELEGATED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "review-required",
        re.compile(
            r"\breview[- ]required\b|\bneeds review\b|\bguardian review\b|\bREVIEW_VERDICT=APPROVED\b|\bAPPROVED\b",
            re.I,
        ),
    ),
    (
        "green-checks/approved-work",
        re.compile(
            r"\b(checks? green|tests? pass(?:ed)?|lint pass(?:ed)?|type[- ]?check pass(?:ed)?|approved todo app slice|merge approved)\b",
            re.I,
        ),
    ),
    (
        "task-hygiene",
        re.compile(
            r"\b(task hygiene|archive|redispatch|requeue|unblock|stale|orphan|assignee=null|false[- ]?positive|classifier|router)\b",
            re.I,
        ),
    ),
    (
        "internal/refactor",
        re.compile(
            r"\b(internal|refactor|architecture phase|reversible|read[- ]only|proof[- ]safe|data[- ]health|baseline|audit|inventory)\b",
            re.I,
        ),
    ),
    (
        "worker-failure",
        re.compile(
            r"\b(crash(?:ed)?|timed out|timeout|spawn_failed|pid .*not alive|skill[- ]load|Unknown skill|collision|completion gate false-positive)\b",
            re.I,
        ),
    ),
]

NEGATED_CRITICAL = re.compile(
    r"\b(no|not|without)\b.{0,40}\b(money|live trading|credentials?|secrets?|prod(?:uction)? deploy|irreversible|new spend)\b",
    re.I,
)
CRITICAL_BOUNDARY_CONTEXT = re.compile(
    r"\b(no|not|without|avoid|do not|don't|never|block instead of|unless|scope excludes|boundary|boundaries|NOT:)\b|\b(read[- ]only|paper[- ]only|paper[- ]safety|proof[- ]safe|no runtime service action|without secret handling|direct[- ]container SELECT|direct DB|SELECT last)\b|\bdesign tokens?\b",
    re.I,
)
CRITICAL_POSITIVE_OVERRIDE = re.compile(
    r"\b(unauthorized|secret reference|no matching approval|rotate|copying? secrets?|creating? credentials?)\b",
    re.I,
)
# --- Seventh Frank-only shape: authority-reserved decision cards ---------------
# The six-critical-list keyword set (money / credentials / live-trading /
# irreversible-data / prod-deploy / new-spend) cannot see a card whose Frank-only
# character comes from *authority reservation* rather than blast radius: e.g.
# "unpark authority is reserved to the owning seat or Frank". Those cards aged
# silently in the delegated/report tail (fixture: sycode-trading/t_d2b2dbbc).
#
# The rule is deliberately COMPOUND — a card qualifies only when it BOTH
#   (a) states that some authority is reserved/restricted to a named principal
#       (Frank or an owning terminal seat), and
#   (b) explicitly requests a decision (DECISION title prefix, a
#       DECISION-REQUESTED-BY marker, or a "Frank to decide / options for Frank"
#       phrasing).
# (a) alone would sweep in every terminal-lane parked card whose park comment
# merely restates the lane rule; (b) alone would sweep in ordinary review cards.
AUTHORITY_RESERVATION_RE = re.compile(
    r"\b(?:unpark|un-park|park|release|unblock|remap|reassign|disposition|dispatch|resolution|override|approval)?\s*"
    r"authority\b[^.\n]{0,90}?\b(?:reserved|restricted|exclusive|belongs solely|rests solely|held solely|sole)\b"
    r"|\b(?:reserved|restricted|limited)\s+(?:solely\s+|exclusively\s+)?(?:to|by)\b[^.\n]{0,60}?\bauthority\b"
    r"|\b(?:only|solely)\s+(?:Frank|the owning seat|the seat)\s+(?:may|can|is able to)\b",
    re.I,
)
AUTHORITY_PRINCIPAL_RE = re.compile(
    r"\bFrank\b|\bowning (?:terminal )?seat\b|\bterminal[- ]lane seat\b|\bthe seat\b|\brepo owner\b",
    re.I,
)
DECISION_REQUEST_TITLE_RE = re.compile(
    r"^\s*(?:\[[^\]]{0,24}\]\s*)?(?:CEO\s+)?DECISION\s*[(:\-\u2014]",
    re.I,
)
DECISION_REQUEST_BODY_RE = re.compile(
    r"\bDECISION[-_ ]REQUESTED[-_ ]BY\b"
    r"|\bOptions? for (?:Frank|the (?:owning )?seat)\b"
    r"|\bFrank(?:\s*/\s*\w+)?\s+to\s+(?:confirm|decide|choose|call|rule)\b"
    r"|\bawaiting (?:a )?(?:Frank|seat)(?:'s)?\s+decision\b"
    r"|\bneeds? (?:a |an )?(?:explicit )?(?:Frank|seat|owner)[- ]?(?:only )?decision\b",
    re.I,
)
# Captures the by-when date so Elon's batch shows the deadline, not just the shape.
DECISION_DUE_RE = re.compile(
    r"\bDECISION[-_ ]REQUESTED[-_ ]BY\b\s*[:\-\u2014]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2}[^\n)]{0,40})",
    re.I,
)


AUTHORITY_RULE_ENABLED = os.environ.get("PENDING_FRANK_AUTHORITY_RULE", "1") != "0"


def authority_reserved_match(title: str, blob: str) -> tuple[str, str] | None:
    """Return (label, snippet) when a card is an authority-reserved Frank-only decision.

    Requires an authority-reservation statement whose *own line* also names the
    reserving principal, PLUS an explicit decision request. Line-scoped principal
    matching stops an unrelated "Frank" mention elsewhere in a long thread from
    manufacturing a match.

    Set PENDING_FRANK_AUTHORITY_RULE=0 to disable (A/B comparison + rollback).
    """
    if not AUTHORITY_RULE_ENABLED:
        return None
    reservation: str | None = None
    for line in blob.splitlines():
        if AUTHORITY_RESERVATION_RE.search(line) and AUTHORITY_PRINCIPAL_RE.search(line):
            reservation = re.sub(r"\s+", " ", line).strip()
            break
    if not reservation:
        return None
    requested = bool(DECISION_REQUEST_TITLE_RE.search(title or ""))
    request_ev = "title prefix DECISION(...)" if requested else ""
    if not requested:
        m = DECISION_REQUEST_BODY_RE.search(blob)
        if not m:
            return None
        request_ev = re.sub(r"\s+", " ", m.group(0)).strip()
    due = DECISION_DUE_RE.search(blob)
    due_ev = ""
    if due:
        due_ev = " || DECISION-REQUESTED-BY: " + re.sub(r"\s+", " ", due.group(1)).strip()
    return (
        "authority-reserved-decision",
        f"{reservation[:220]} || decision-request: {request_ev}{due_ev}",
    )


AMBIGUOUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "human-approval-mentioned-but-not-six-critical",
        re.compile(
            r"\b(after Frank review|requires Frank approval|HOLD for Frank|Needs Frank or repo owner|Frank approval before)\b",
            re.I,
        ),
    ),
]


@dataclass
class TaskEvidence:
    board: str
    db: Path
    task_id: str
    title: str
    body: str
    assignee: str | None
    status: str
    created_at: int | None
    result: str
    comments: list[tuple[str, str, int]]
    runs: list[tuple[str, str, str, str]]

    @property
    def blob(self) -> str:
        parts = [self.title or "", self.body or "", self.result or ""]
        parts.extend(body for _author, body, _ts in self.comments[-8:])
        for status, outcome, summary, error in self.runs[-5:]:
            parts.extend([status or "", outcome or "", summary or "", error or ""])
        return "\n".join(parts)


def safe_connect(db: Path) -> sqlite3.Connection | None:
    """Open a board DB via kanban_db.connect() — handles WAL, init, integrity checks."""
    if not db.is_file():
        return None
    try:
        return kb.connect(db_path=db)
    except Exception as exc:
        print(f"WARN: cannot open {db}: {exc}", file=sys.stderr)
        return None


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def rows_for_board(
    board: str, db: Path, only_ids: set[str] | None = None
) -> list[TaskEvidence]:
    con = safe_connect(db)
    if con is None:
        return []
    try:
        where = "status='blocked'"
        params: list[str] = []
        if only_ids:
            placeholders = ",".join("?" for _ in only_ids)
            where += f" AND id IN ({placeholders})"
            params.extend(sorted(only_ids))
        tasks = con.execute(
            f"SELECT id,title,COALESCE(body,''),assignee,status,created_at,COALESCE(result,'') FROM tasks WHERE {where} ORDER BY created_at ASC",
            params,
        ).fetchall()
        out: list[TaskEvidence] = []
        for tid, title, body, assignee, status, created_at, result in tasks:
            comments: list[tuple[str, str, int]] = []
            if table_exists(con, "task_comments"):
                comments = con.execute(
                    "SELECT author, body, created_at FROM task_comments WHERE task_id=? ORDER BY created_at ASC",
                    (tid,),
                ).fetchall()
            runs: list[tuple[str, str, str, str]] = []
            if table_exists(con, "task_runs"):
                runs = con.execute(
                    "SELECT COALESCE(status,''),COALESCE(outcome,''),COALESCE(summary,''),COALESCE(error,'') FROM task_runs WHERE task_id=? ORDER BY started_at ASC",
                    (tid,),
                ).fetchall()
            out.append(
                TaskEvidence(
                    board,
                    db,
                    tid,
                    title or "",
                    body or "",
                    assignee,
                    status,
                    created_at,
                    result or "",
                    comments,
                    runs,
                )
            )
        return out
    finally:
        con.close()


def fleet_status_ids(path: Path) -> dict[str, set[str]]:
    if not path.exists():
        return {}
    ids: dict[str, set[str]] = {}
    line_re = re.compile(r"^-\s+([^|]+)\|\s*(t_[0-9a-fA-F]+)\s*\|")
    for line in path.read_text(errors="replace").splitlines():
        m = line_re.match(line)
        if m:
            board = m.group(1).strip()
            ids.setdefault(board, set()).add(m.group(2))
    return ids


def a3_queue_ids_from_scan() -> dict[str, set[str]]:
    """Union the approval-blocker-scan candidate set (complete, no per-board
    LIMIT) into the digest so A3/A2 gated cards that FLEET-STATUS.md's top-N
    'Pending Frank' section truncates are not dropped.

    We feed the FULL scan queue (minus NON_APPROVAL_BLOCKER / SUPERSEDED_BLOCKED)
    and let the existing CRITICAL_PATTERNS re-classify, because the scan itself
    splits some genuinely-Frank-gated cards into A2_DELEGATED_CANDIDATE.
    """
    import json as _json
    import subprocess as _subprocess

    if not APPROVAL_SCAN_SCRIPT.is_file():
        print(
            f"WARN: approval-blocker-scan not found at {APPROVAL_SCAN_SCRIPT}; skipping scan feed",
            file=sys.stderr,
        )
        return {}
    try:
        out = _subprocess.run(
            [sys.executable, str(APPROVAL_SCAN_SCRIPT), "--json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        print(f"WARN: approval-blocker-scan failed: {exc}", file=sys.stderr)
        return {}
    if out.returncode != 0:
        print(
            f"WARN: approval-blocker-scan rc={out.returncode}: {out.stderr[:200]}",
            file=sys.stderr,
        )
        return {}
    try:
        payload = _json.loads(out.stdout)
    except Exception as exc:
        print(f"WARN: approval-blocker-scan json parse failed: {exc}", file=sys.stderr)
        return {}
    skip = {"NON_APPROVAL_BLOCKER", "SUPERSEDED_BLOCKED"}
    ids: dict[str, set[str]] = {}
    for item in payload.get("items", []):
        if item.get("classification") in skip:
            continue
        ids.setdefault(item.get("board", ""), set()).add(item.get("task_id", ""))
    return ids


def first_matches(
    patterns: list[tuple[str, re.Pattern[str]]], text: str, *, skip_boundary_context: bool = False
) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for label, pat in patterns:
        m = pat.search(text)
        if m:
            snippet = text[max(0, m.start() - 200) : min(len(text), m.end() + 100)]
            snippet = re.sub(r"\s+", " ", snippet).strip()
            if (
                skip_boundary_context
                and label != "explicit-frank-gate"
                and CRITICAL_BOUNDARY_CONTEXT.search(snippet)
                and not CRITICAL_POSITIVE_OVERRIDE.search(snippet)
            ):
                continue
            matches.append((label, snippet))
    return matches


def classify(t: TaskEvidence) -> tuple[str, list[str]]:
    blob = t.blob
    critical = first_matches(CRITICAL_PATTERNS, blob, skip_boundary_context=True)
    # Explicit negated boundaries in proposals/bodies are useful context, but do not
    # erase real task/comment evidence elsewhere; they just get reported as evidence.
    if critical:
        return "critical-list", [
            f"{label}: {snippet}" for label, snippet in critical[:3]
        ]
    # Seventh Frank-only shape: authority reserved to Frank / an owning seat AND an
    # explicit decision request. Evaluated after the six-critical list (which wins on
    # blast radius) but before delegated-review, so these stop aging in the tail.
    authority = authority_reserved_match(t.title, blob)
    if authority:
        return "critical-list", [f"{authority[0]}: {authority[1]}"]
    ambiguous = first_matches(AMBIGUOUS_PATTERNS, blob)
    if ambiguous:
        return "ambiguous", [f"{label}: {snippet}" for label, snippet in ambiguous[:3]]
    delegated = first_matches(DELEGATED_PATTERNS, blob)
    if delegated:
        evidence = [f"{label}: {snippet}" for label, snippet in delegated[:3]]
        neg = NEGATED_CRITICAL.search(blob)
        if neg:
            evidence.append(
                "critical-boundary-negated: "
                + re.sub(r"\s+", " ", neg.group(0)).strip()
            )
        return "delegated-review", evidence
    return "ambiguous", [
        "No six-critical-list marker and no deterministic delegated-review/task-hygiene marker found in title/body/latest comments/runs."
    ]


def orphan_rows(db: Path, board: str, limit: int = 50) -> list[tuple[str, str, str]]:
    con = safe_connect(db)
    if con is None:
        return []
    try:
        return con.execute(
            "SELECT id,status,substr(replace(title,char(10),' '),1,90) FROM tasks WHERE status IN ('ready','todo') AND (assignee IS NULL OR trim(assignee)='') ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()


def already_commented(con: sqlite3.Connection, task_id: str, marker: str) -> bool:
    # Stable idempotency: any prior pending-frank-triage marker on this task
    # means the route comment already exists. Do not hash evidence because the
    # latest comments become future evidence and would cause duplicates.
    return (
        con.execute(
            "SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE ? LIMIT 1",
            (task_id, "%pending-frank-triage:%"),
        ).fetchone()
        is not None
    )


def apply_comment(t: TaskEvidence, classification: str, evidence: list[str]) -> str:
    if classification != "delegated-review":
        return "skipped"
    marker = "pending-frank-triage:v1"
    body = (
        f"delegated: {marker} classified as delegated-review by deterministic Pending-Frank triage; "
        "route to guardian/PM under delegated-authority.md, not Frank, unless a six-critical-list boundary appears. "
        "Evidence: " + " ; ".join(evidence[:3])
    )
    con = safe_connect(t.db)
    if con is None:
        return "error:db-open"
    try:
        if already_commented(con, t.task_id, marker):
            return "already-present"
        con.execute(
            "INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?,?,?,?)",
            (t.task_id, AUTHOR, body, int(time.time())),
        )
        con.execute(
            "INSERT INTO task_events(task_id, kind, payload, created_at, run_id) VALUES (?,?,?,?,NULL)",
            (
                t.task_id,
                "commented",
                '{"author":"pending-frank-triage","delegated":true}',
                int(time.time()),
            ),
        )
        con.commit()
        return "comment-added"
    except Exception as exc:
        con.rollback()
        return f"error:{exc}"
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--status-file",
        type=Path,
        default=DEFAULT_STATUS,
        help="FLEET-STATUS.md to parse for Pending Frank rows",
    )
    ap.add_argument(
        "--all-blocked",
        action="store_true",
        help="Classify all blocked tasks instead of only rows present in FLEET-STATUS.md",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Add idempotent delegated: comments to delegated-review tasks only",
    )
    ap.add_argument(
        "--no-orphans",
        action="store_true",
        help="Suppress assignee=null ready/todo report lane",
    )
    ap.add_argument(
        "--a3-queue-from-scan",
        action="store_true",
        help="Union the approval-blocker-scan candidate set (complete, no per-board LIMIT) into the digest so A3/A2 gated cards truncated by FLEET-STATUS.md's Pending Frank section are captured.",
    )
    args = ap.parse_args()

    ids_by_board = {} if args.all_blocked else fleet_status_ids(args.status_file)
    if args.a3_queue_from_scan and not args.all_blocked:
        scan_ids = a3_queue_ids_from_scan()
        for board, s in scan_ids.items():
            ids_by_board.setdefault(board, set()).update(s)
    if not args.all_blocked and not ids_by_board:
        print("# Pending Frank delegated triage")
        print("Pending Frank before: 0")
        print(
            "No Pending Frank rows found in status file; use --all-blocked to scan all blocked tasks."
        )
        return 0

    print("# Pending Frank delegated triage")
    print(f"Mode: {'APPLY delegated comments only' if args.apply else 'report-only'}")
    print(
        f"Source: {'all blocked tasks' if args.all_blocked else str(args.status_file)}"
    )
    print()

    boards = sorted(p for p in BOARDS_DIR.glob("*/kanban.db") if p.is_file())
    tasks: list[TaskEvidence] = []
    for db in boards:
        board = db.parent.name
        if not args.all_blocked and board not in ids_by_board:
            continue
        tasks.extend(
            rows_for_board(
                board, db, None if args.all_blocked else ids_by_board.get(board, set())
            )
        )

    before = len(tasks)
    buckets: dict[str, list[tuple[TaskEvidence, list[str], str]]] = {
        "critical-list": [],
        "delegated-review": [],
        "ambiguous": [],
    }
    for t in tasks:
        cls, evidence = classify(t)
        action = apply_comment(t, cls, evidence) if args.apply else "report-only"
        buckets[cls].append((t, evidence, action))

    print(f"Pending Frank before: {before}")
    print(f"Classified critical-list: {len(buckets['critical-list'])}")
    print(f"Classified delegated-review: {len(buckets['delegated-review'])}")
    print(f"Classified ambiguous: {len(buckets['ambiguous'])}")
    print(
        f"False Pending Frank after delegated routing (report metric): {len(buckets['critical-list']) + len(buckets['ambiguous'])}"
    )
    print()

    for cls in ("critical-list", "delegated-review", "ambiguous"):
        print(f"## {cls}")
        if not buckets[cls]:
            print("- none")
        for t, evidence, action in buckets[cls]:
            print(f"- {t.board} | {t.task_id} | {t.title[:90]}")
            print(f"  action: {action}")
            for ev in evidence[:4]:
                print(f"  evidence: {ev}")
        print()

    if not args.no_orphans:
        print("## Orphan router debt (ready/todo assignee=null; report-only)")
        orphan_count = 0
        for db in boards:
            board = db.parent.name
            rows = orphan_rows(db, board)
            if not rows:
                continue
            orphan_count += len(rows)
            print(f"### {board} ({len(rows)})")
            for tid, status, title in rows:
                print(f"- {board} | {tid} | {status} | {title}")
        if orphan_count == 0:
            print("- none")
        print(f"Orphan ready/todo assignee=null count: {orphan_count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
