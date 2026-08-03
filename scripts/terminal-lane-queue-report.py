#!/usr/bin/env python3
"""terminal-lane-queue-report.py — surface non-spawnable kanban lanes.

Terminal lanes (fable/codex/grok, external-*, orion-*) are intentionally
NEVER dispatched by the Hermes dispatcher (kanban_db.py profile_exists /
skipped_nonspawnable guard). Cards parked there sit in `ready` forever
unless a human (Frank) or a seat drains them. This report enumerates those
parked cards per lane so the queue is visible instead of a silent black
hole.

See kanban task t_2e808b44 (GAP-HUNT 2026-08-02).

Modes:
  --md     Emit the Markdown section destined for FLEET-STATUS.md (stdout).
  (default) Emit a compact escalation summary for the cron Discord digest;
            silent when there are no cards older than ESCALATE_DAYS.

Read-only: SELECTs only. Never mutates any board or status file.
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
import time

KANBAN_HOME = os.environ.get("HERMES_KANBAN_HOME", "/home/frank/.hermes/kanban")
BOARDS_GLOB = os.path.join(KANBAN_HOME, "boards", "*", "kanban.db")

# Mirror the dispatcher's terminal-lane predicate exactly:
#   nonspawnable-fleet-alert-guard.py EXCLUDED_EXACT / EXCLUDED_PREFIX
#   + GAP task t_2e808b44 ("fable/codex/grok/external-*").
TERMINAL_EXACT = {"fable", "codex", "grok"}
TERMINAL_PREFIX = ("external-", "orion-")

ESCALATE_DAYS = float(os.environ.get("TERMINAL_LANE_ESCALATE_DAYS", "7"))


def is_terminal(assignee: str | None) -> bool:
    a = (assignee or "").lower().strip()
    if not a:
        return False
    if a in TERMINAL_EXACT:
        return True
    return any(a.startswith(p) for p in TERMINAL_PREFIX)


def board_db_paths() -> list[str]:
    return sorted(glob.glob(BOARDS_GLOB))


def age_days(created_at) -> float | None:
    if not created_at:
        return None
    try:
        return (time.time() - float(created_at)) / 86400.0
    except (TypeError, ValueError):
        return None


def collect() -> dict[str, list[dict]]:
    """Return {board: [ {id,lane,title,age,priority}, ... ]} for parked cards."""
    out: dict[str, list[dict]] = {}
    for db in board_db_paths():
        slug = os.path.basename(os.path.dirname(db))
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            rows = cur.execute(
                "SELECT id, assignee, title, created_at, priority "
                "FROM tasks WHERE status='ready' AND assignee IS NOT NULL"
            ).fetchall()
            con.close()
        except Exception as ex:  # noqa: BLE001 - report and skip a bad board
            print(f"  ! {db}: {ex}", file=sys.stderr)
            continue
        items: list[dict] = []
        for r in rows:
            if not is_terminal(r["assignee"]):
                continue
            items.append(
                {
                    "id": r["id"],
                    "lane": r["assignee"],
                    "title": (r["title"] or "").replace("\n", " ").strip(),
                    "age": age_days(r["created_at"]),
                    "priority": r["priority"] or 0,
                }
            )
        if items:
            out[slug] = items
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", action="store_true", help="emit Markdown section for FLEET-STATUS.md")
    ap.add_argument(
        "--boards-dir",
        default=None,
        help="override HERMES_KANBAN_HOME (test hook: dir containing boards/*/kanban.db)",
    )
    args = ap.parse_args()
    global BOARDS_GLOB
    if args.boards_dir:
        BOARDS_GLOB = os.path.join(args.boards_dir, "boards", "*", "kanban.db")

    data = collect()

    # (board, lane) -> items
    lanes: dict[tuple[str, str], list[dict]] = {}
    for board, items in data.items():
        for it in items:
            lanes.setdefault((board, it["lane"]), []).append(it)

    if args.md:
        emit_md(data, lanes)
    else:
        emit_digest(lanes)
    return 0


def emit_md(data: dict[str, list[dict]], lanes: dict[tuple[str, str], list[dict]]) -> None:
    total = sum(len(v) for v in data.values())
    print("## Terminal-lane queue (human/seat-drained only)")
    print(
        f"- These lanes are non-spawnable by design (fable/codex/grok, external-*, orion-*); "
        f"only Frank or a seat can drain them. {total} ready card(s) currently parked across "
        f"{len(data)} board(s). A lane only a human/seat can drain must tell the human it is filling."
    )
    if not data:
        print("- none parked")
        return
    for (board, lane) in sorted(lanes.keys()):
        items = sorted(
            lanes[(board, lane)],
            key=lambda x: (x["age"] is None, -(x["age"] or 0)),
        )
        oldest = max((i["age"] for i in items if i["age"] is not None), default=None)
        old_s = f"{oldest:.1f}d" if oldest is not None else "?"
        print(f"### {board}/{lane}: {len(items)} ready, oldest {old_s}")
        for it in items[:8]:
            age_s = f"{it['age']:.1f}d" if it["age"] is not None else "?"
            flag = "  [>7d]" if (it["age"] is not None and it["age"] > ESCALATE_DAYS) else ""
            title = it["title"][:60]
            print(f"- {it['id']} ({age_s}){flag} {title}")
        if len(items) > 8:
            print(f"- ... and {len(items) - 8} more")
    esc = [
        (b, it)
        for (b, _l), items in lanes.items()
        for it in items
        if it["age"] is not None and it["age"] > ESCALATE_DAYS
    ]
    print("### Terminal-lane escalations (>7d ready - Frank/seat action needed)")
    if not esc:
        print("- none")
    else:
        for b, it in sorted(esc, key=lambda x: -(x[1]["age"] or 0)):
            print(f"- [{b}] {it['id']} {it['lane']} {it['age']:.1f}d {it['title'][:60]}")


def emit_digest(lanes: dict[tuple[str, str], list[dict]]) -> None:
    all_items = [it for items in lanes.values() for it in items]
    esc = [it for it in all_items if it["age"] is not None and it["age"] > ESCALATE_DAYS]
    if not esc:
        return  # silent when green (no spam to the Discord digest)
    boards = sorted({b for (b, _l) in lanes})
    print(
        f"TERMINAL-LANE QUEUE: {len(all_items)} parked card(s) across "
        f"{len(lanes)} terminal lane(s) on {len(boards)} board(s); "
        f"{len(esc)} older than {ESCALATE_DAYS:.0f}d need Frank/seat action:"
    )
    for it in sorted(esc, key=lambda x: -(x["age"] or 0))[:25]:
        print(f"  - {it['id']} {it['lane']} {it['age']:.1f}d {it['title'][:55]}")


if __name__ == "__main__":
    raise SystemExit(main())
