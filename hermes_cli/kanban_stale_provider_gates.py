"""Read-only scanner for stale provider-capacity kanban blockers.

This module deliberately does **not** mutate task state, provider routing,
credentials, cron jobs, or fallback chains.  It gives governors/operators a
deterministic report for the safe pattern:

* capacity-style blocker is older than a reset window;
* the same assignee/profile has completed other tasks since the blocker;
* dependent safe/read-only cards can be marked eligible only when their other
  parent dependencies are already done.

Credential/auth failures are kept separate from temporary 429/quota/capacity
windows so they never get auto-expired by this diagnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Iterable, Optional


CAPACITY_RE = re.compile(
    r"\b(429|rate[- ]?limit(?:ed)?|quota|usage limit|too many requests|"
    r"capacity|temporar(?:y|ily)|cooldown|reset window|provider[- ]capacity)\b",
    re.IGNORECASE,
)

AUTH_RE = re.compile(
    r"\b(401|403|unauthori[sz]ed|forbidden|not logged in|not logged into|"
    r"missing access[_ -]?token|invalid api key|invalid token|token refresh failed|"
    r"revoked|auth(?:entication|orization)? failed|primary auth failed|"
    r"missing credential|missing credentials)\b",
    re.IGNORECASE,
)

SAFE_READ_ONLY_RE = re.compile(
    r"\b(read[- ]?only|diagnostic|report|research|audit|scan|docs?|runbook|"
    r"governance|review|triage)\b",
    re.IGNORECASE,
)


@dataclass
class CompletionEvidence:
    task_id: str
    title: str
    completed_at: int

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "completed_at": self.completed_at,
        }


@dataclass
class DependentEvidence:
    board: str
    task_id: str
    title: str
    status: str
    assignee: Optional[str]
    safe_read_only_signal: bool
    eligible: bool
    open_parent_ids: list[str] = field(default_factory=list)
    mention_source: str = "mention"

    def to_dict(self) -> dict:
        return {
            "board": self.board,
            "task_id": self.task_id,
            "title": self.title,
            "status": self.status,
            "assignee": self.assignee,
            "safe_read_only_signal": self.safe_read_only_signal,
            "eligible": self.eligible,
            "open_parent_ids": self.open_parent_ids,
            "mention_source": self.mention_source,
        }


@dataclass
class ProviderGateFinding:
    board: str
    task_id: str
    title: str
    assignee: Optional[str]
    status: str
    classification: str
    age_seconds: int
    stale: bool
    reason: str
    same_profile_successes: list[CompletionEvidence] = field(default_factory=list)
    dependents: list[DependentEvidence] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "board": self.board,
            "task_id": self.task_id,
            "title": self.title,
            "assignee": self.assignee,
            "status": self.status,
            "classification": self.classification,
            "age_seconds": self.age_seconds,
            "stale": self.stale,
            "reason": self.reason,
            "same_profile_successes": [s.to_dict() for s in self.same_profile_successes],
            "dependents": [d.to_dict() for d in self.dependents],
        }


def _row_text(row: sqlite3.Row, comments: Iterable[str] = ()) -> str:
    parts = [
        row["title"] or "",
        row["body"] or "",
        row["last_failure_error"] or "",
    ]
    parts.extend(c or "" for c in comments)
    return "\n".join(parts)


def classify_provider_gate(text: str) -> Optional[str]:
    """Classify provider-related blocker text.

    Returns ``provider_capacity`` for temporary quota/rate-limit/capacity
    blockers, ``active_credential_auth_failure`` for credential/auth blockers,
    or ``None`` when the text is not provider related.  Auth wins over capacity
    because credential failures must stay operator-gated even if they also
    mention a provider or quota.
    """
    if not text:
        return None
    if AUTH_RE.search(text):
        return "active_credential_auth_failure"
    if CAPACITY_RE.search(text):
        return "provider_capacity"
    return None


def _latest_block_timestamp(conn: sqlite3.Connection, task_id: str, fallback: int) -> int:
    row = conn.execute(
        "SELECT created_at FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked', 'gave_up') "
        "ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is not None and row["created_at"] is not None:
        return int(row["created_at"])
    return int(fallback or 0)


def _task_comments(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return [
        r["body"] or ""
        for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    ]


def _same_profile_successes(
    conn: sqlite3.Connection,
    *,
    assignee: Optional[str],
    since: int,
    exclude_task_id: str,
    limit: int = 5,
) -> list[CompletionEvidence]:
    if not assignee:
        return []
    rows = conn.execute(
        "SELECT id, title, completed_at FROM tasks "
        "WHERE assignee = ? AND status = 'done' "
        "  AND completed_at IS NOT NULL AND completed_at >= ? "
        "  AND id != ? AND consecutive_failures = 0 "
        "ORDER BY completed_at DESC LIMIT ?",
        (assignee, int(since), exclude_task_id, int(limit)),
    ).fetchall()
    return [
        CompletionEvidence(
            task_id=r["id"],
            title=r["title"] or "",
            completed_at=int(r["completed_at"] or 0),
        )
        for r in rows
    ]


def _open_parents(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT p.id FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? AND p.status != 'done' "
        "ORDER BY p.id",
        (task_id,),
    ).fetchall()
    return [r["id"] for r in rows]


def _linked_child_ids(conn: sqlite3.Connection, task_id: str) -> set[str]:
    return {
        r["child_id"]
        for r in conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?",
            (task_id,),
        ).fetchall()
    }


def _mentioning_dependents(
    *,
    board_paths: dict[str, Path],
    blocker_id: str,
    linked_children_by_board: dict[str, set[str]],
) -> list[DependentEvidence]:
    dependents: list[DependentEvidence] = []
    needle = blocker_id.lower()
    for board, path in board_paths.items():
        if not path.exists():
            continue
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, title, body, status, assignee, last_failure_error "
                "FROM tasks WHERE status IN ('blocked', 'todo', 'ready', 'triage', 'review') "
                "ORDER BY priority DESC, created_at ASC"
            ).fetchall()
            linked_children = linked_children_by_board.get(board, set())
            for row in rows:
                comments = _task_comments(conn, row["id"])
                haystack = _row_text(row, comments).lower()
                linked = row["id"] in linked_children
                mentioned = needle in haystack
                if not linked and not mentioned:
                    continue
                open_parent_ids = [
                    parent_id
                    for parent_id in _open_parents(conn, row["id"])
                    if parent_id != blocker_id
                ]
                text = _row_text(row, comments)
                safe_read_only = bool(SAFE_READ_ONLY_RE.search(text))
                dependents.append(
                    DependentEvidence(
                        board=board,
                        task_id=row["id"],
                        title=row["title"] or "",
                        status=row["status"] or "",
                        assignee=row["assignee"],
                        safe_read_only_signal=safe_read_only,
                        eligible=safe_read_only and not open_parent_ids,
                        open_parent_ids=open_parent_ids,
                        mention_source="link" if linked else "mention",
                    )
                )
        finally:
            conn.close()
    return dependents


def discover_board_paths(home: Path, boards: Optional[Iterable[str]] = None) -> dict[str, Path]:
    """Return ``board_slug -> kanban.db`` paths under a Hermes home/root."""
    home = Path(home).expanduser()
    selected = list(boards or [])
    if selected:
        out: dict[str, Path] = {}
        for board in selected:
            if board == "default":
                out[board] = home / "kanban.db"
            else:
                out[board] = home / "kanban" / "boards" / board / "kanban.db"
        return out

    out = {}
    default_db = home / "kanban.db"
    if default_db.exists():
        out["default"] = default_db
    boards_root = home / "kanban" / "boards"
    if boards_root.exists():
        for child in sorted(boards_root.iterdir()):
            db = child / "kanban.db"
            if db.exists():
                out[child.name] = db
    return out


def scan_board(
    board: str,
    db_path: Path,
    *,
    all_board_paths: Optional[dict[str, Path]] = None,
    reset_window_seconds: int = 24 * 60 * 60,
    now: Optional[int] = None,
) -> list[ProviderGateFinding]:
    """Scan one board DB and return provider blocker findings."""
    now = int(time.time() if now is None else now)
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, title, body, assignee, status, created_at, "
            "       last_failure_error, consecutive_failures "
            "FROM tasks WHERE status = 'blocked' "
            "ORDER BY priority DESC, created_at ASC"
        ).fetchall()
        findings: list[ProviderGateFinding] = []
        linked_children = {row["id"]: _linked_child_ids(conn, row["id"]) for row in rows}
        for row in rows:
            comments = _task_comments(conn, row["id"])
            text = _row_text(row, comments)
            classification = classify_provider_gate(text)
            if classification is None:
                continue

            blocked_since = _latest_block_timestamp(conn, row["id"], row["created_at"])
            age = max(0, now - int(blocked_since or now))
            successes = _same_profile_successes(
                conn,
                assignee=row["assignee"],
                since=blocked_since,
                exclude_task_id=row["id"],
            )
            stale = (
                classification == "provider_capacity"
                and age >= reset_window_seconds
                and bool(successes)
            )
            if classification == "active_credential_auth_failure":
                reason = "credential/auth marker present; keep operator-gated"
            elif stale:
                reason = "capacity blocker older than reset window and same-profile tasks completed successfully"
            elif age < reset_window_seconds:
                reason = "capacity blocker is still inside reset window"
            else:
                reason = "capacity blocker lacks same-profile completion evidence after the block"

            board_paths = all_board_paths or {board: db_path}
            child_map = {board: linked_children.get(row["id"], set())}
            dependents = _mentioning_dependents(
                board_paths=board_paths,
                blocker_id=row["id"],
                linked_children_by_board=child_map,
            )
            dependents = [d for d in dependents if not (d.board == board and d.task_id == row["id"])]

            findings.append(
                ProviderGateFinding(
                    board=board,
                    task_id=row["id"],
                    title=row["title"] or "",
                    assignee=row["assignee"],
                    status=row["status"] or "",
                    classification=classification,
                    age_seconds=age,
                    stale=stale,
                    reason=reason,
                    same_profile_successes=successes,
                    dependents=dependents,
                )
            )
        return findings
    finally:
        conn.close()


def scan_all_boards(
    home: Path,
    *,
    boards: Optional[Iterable[str]] = None,
    reset_window_seconds: int = 24 * 60 * 60,
    now: Optional[int] = None,
) -> list[ProviderGateFinding]:
    board_paths = discover_board_paths(home, boards)
    findings: list[ProviderGateFinding] = []
    for board, path in board_paths.items():
        findings.extend(
            scan_board(
                board,
                path,
                all_board_paths=board_paths,
                reset_window_seconds=reset_window_seconds,
                now=now,
            )
        )
    return findings


def findings_to_json(findings: Iterable[ProviderGateFinding]) -> str:
    return json.dumps([f.to_dict() for f in findings], indent=2, sort_keys=True)
