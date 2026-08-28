#!/usr/bin/env python3
"""Deterministic, governed daily digest of verdict-router REJECT decisions."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from second_brain_writer import append_markdown_event, write_markdown_atomic


ROOT = Path(os.environ.get("PM_REJECT_ROOT", "/home/frank/.hermes"))
BOARDS_DIR = ROOT / "kanban" / "boards"
DEFAULT_DB = ROOT / "kanban.db"
VAULT_ROOT = Path(os.environ.get("PM_REJECT_VAULT_ROOT", "/home/frank/obsidian-fleet-vault"))
VAULT_DIR = VAULT_ROOT / "Orchestration" / "kanban-verdict-router"
EXCLUDED_BOARDS = {"orchestrator-sync"}
REJECT_PREFIX = "REJECTED: verdict-router processed REVIEW_VERDICT=REJECT."
A3_REJECT_MARKER = "A3-REJECT: operator-gated scope"


@dataclass(frozen=True)
class RejectEntry:
    board: str
    task_id: str
    task_title: str
    task_status: str
    is_a3: bool
    comment_id: int
    source_author: str


def all_boards() -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    if DEFAULT_DB.exists():
        found.append(("default", DEFAULT_DB))
    if BOARDS_DIR.exists():
        for database in sorted(BOARDS_DIR.glob("*/kanban.db")):
            slug = database.parent.name
            if not slug.startswith("_") and slug not in EXCLUDED_BOARDS:
                found.append((slug, database))
    return found


def scan_board(slug: str, database: Path) -> list[RejectEntry]:
    if not database.exists():
        return []
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_comments'"
        ).fetchone():
            return []
        rows = connection.execute(
            """
            SELECT tc.id AS comment_id, tc.task_id, tc.body,
                   t.title AS task_title, t.status AS task_status
              FROM task_comments tc
              LEFT JOIN tasks t ON t.id = tc.task_id
             WHERE tc.author = 'verdict-router'
               AND tc.body LIKE ?
             ORDER BY tc.id DESC
            """,
            (f"{REJECT_PREFIX}%",),
        ).fetchall()
        entries = []
        for row in rows:
            body = row["body"] or ""
            source_author = ""
            for line in body.splitlines():
                if line.startswith("review_comment_author="):
                    source_author = line.split("=", 1)[1].strip()
                    break
            entries.append(
                RejectEntry(
                    board=slug,
                    task_id=row["task_id"],
                    task_title=row["task_title"] or "(no title)",
                    task_status=row["task_status"] or "unknown",
                    is_a3=A3_REJECT_MARKER in body,
                    comment_id=int(row["comment_id"]),
                    source_author=source_author,
                )
            )
        return entries
    finally:
        connection.close()


def deduplicate(entries: list[RejectEntry]) -> list[RejectEntry]:
    """Keep the newest REJECT marker for each board/task pair."""
    latest: dict[tuple[str, str], RejectEntry] = {}
    for entry in entries:
        key = (entry.board, entry.task_id)
        current = latest.get(key)
        if current is None or entry.comment_id > current.comment_id:
            latest[key] = entry
    return sorted(latest.values(), key=lambda item: (item.board, item.task_id))


def format_digest(entries: list[RejectEntry], *, today: str | None = None) -> str:
    report_date = today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    a3 = [entry for entry in entries if entry.is_a3]
    standard = [entry for entry in entries if not entry.is_a3]
    lines = [f"### Verdict Router: REJECT Summary — {report_date}", ""]
    if not entries:
        lines.append("No REJECT verdicts found.")
        return "\n".join(lines)
    for heading, values in (
        ("A3-REJECT (needs Frank)", a3),
        ("Standard REJECT (PM triage)", standard),
    ):
        if not values:
            continue
        if len(lines) > 2:
            lines.append("")
        lines.append(f"**{heading}:** ({len(values)})")
        for entry in values:
            state = f" [{entry.task_status}]" if entry.task_status != "blocked" else ""
            lines.append(f"- `{entry.board}/{entry.task_id}`{state} — {entry.task_title}")
            if entry.source_author:
                lines.append(f"  - Source: {entry.source_author}")
    return "\n".join(lines)


def persist_digest(digest: str, *, vault_dir: Path = VAULT_DIR, now: dt.datetime | None = None) -> None:
    timestamp = now or dt.datetime.now(dt.timezone.utc)
    today = timestamp.date().isoformat()
    iso_time = timestamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    common = {
        "type": "task-evidence",
        "status": "active",
        "created": today,
        "updated": today,
        "confidence": "high",
        "tags": ["hermes", "kanban", "pm", "reject-monitor", "digest"],
        "sources": ["/home/frank/.hermes/kanban/boards"],
        "project": "control-plane",
        "owners": ["jarvis-os-pm"],
        "knowledge_tier": "evidence",
        "generated": True,
        "generator": "pm_reject_monitor.py",
    }
    append_markdown_event(
        vault_dir / f"{today}-verdict-router-shadow-log.md",
        f"## PM REJECT Monitor — {iso_time}\n\n{digest}",
        initial_body=(
            "# Kanban Verdict Router Shadow Log\n\n"
            "Logs deterministic decisions by `verdict_router.py` and the PM REJECT monitor."
        ),
        title=f"Kanban Verdict Router Shadow Log — {today}",
        **common,
    )
    write_markdown_atomic(
        vault_dir / f"pm-reject-digest-{today}.md",
        digest,
        title=f"Verdict Router REJECT Digest — {today}",
        **common,
    )


def self_test() -> None:
    older = RejectEntry("jarvis-os", "t_12345678", "Old", "blocked", True, 1, "reviewer")
    newer = RejectEntry("jarvis-os", "t_12345678", "Current", "blocked", True, 2, "reviewer")
    standard = RejectEntry("upero", "t_87654321", "Standard", "archived", False, 3, "reviewer")
    values = deduplicate([older, newer, standard])
    assert [entry.comment_id for entry in values] == [2, 3]
    digest = format_digest(values, today="2026-07-13")
    assert digest.count("t_12345678") == 1 and "A3-REJECT" in digest and "Standard REJECT" in digest
    with tempfile.TemporaryDirectory(prefix="pm-reject-monitor-test-") as temporary:
        root = Path(temporary)
        persist_digest(
            digest,
            vault_dir=root,
            now=dt.datetime(2026, 7, 13, 7, 0, tzinfo=dt.timezone.utc),
        )
        notes = sorted(root.glob("*.md"))
        assert len(notes) == 2
        for note in notes:
            text = note.read_text(encoding="utf-8")
            assert text.startswith("---\n")
            assert 'type: "task-evidence"' in text
            assert 'generator: "pm_reject_monitor.py"' in text
        assert not list(root.glob(".*.incoming-*"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", action="store_true", help="persist the canonical digest notes")
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    parser.add_argument("--self-test", action="store_true", help="run deterministic canaries")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print(json.dumps({"status": "pass", "component": "pm-reject-monitor"}, sort_keys=True))
        return 0

    entries: list[RejectEntry] = []
    failures: list[dict[str, str]] = []
    for slug, database in all_boards():
        try:
            entries.extend(scan_board(slug, database))
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            failures.append({"board": slug, "error": str(exc)})
    entries = deduplicate(entries)
    digest = format_digest(entries)
    if args.json:
        print(
            json.dumps(
                {
                    "digest": digest,
                    "entries": [asdict(entry) for entry in entries],
                    "counts": {
                        "total": len(entries),
                        "a3": sum(entry.is_a3 for entry in entries),
                        "standard": sum(not entry.is_a3 for entry in entries),
                    },
                    "failures": failures,
                },
                sort_keys=True,
            )
        )
    else:
        print(digest)
    if args.vault and entries:
        persist_digest(digest)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
