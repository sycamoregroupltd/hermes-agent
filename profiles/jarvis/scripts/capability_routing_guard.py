#!/usr/bin/env python3
"""capability_routing_guard.py — flag cards routed to profiles that cannot do the work.

WHY (2026-08-04): the sycode-trading fleet sat at 2 running / 31 ready while six
profiles idled. Two routing faults, both invisible:

  1. UNROUTABLE — 8 cards assigned to profiles with no directory on disk. The
     dispatcher drops them silently; they can never run. (board-unroutable-assignee-sweep
     covers this one, when it exists — it had been wiped.)

  2. CAPABILITY MISMATCH — cards assigned to profiles whose TOOLSET cannot perform
     the work. `devops` held 21 cards with toolsets ['hermes-cli','kanban'] — no
     terminal, no file. `platform-db-migrator`, whose entire job is running
     migrations, has only ['hermes-cli']. Workers pick these up, discover the wall,
     and block with "no terminal/shell tools in this session". The card then looks
     blocked-on-a-gate when it is really blocked-on-a-typo-in-routing.

     This is worse than unroutable: the card DOES dispatch, burns a worker slot and
     a provider call, then blocks. It looks like progress.

This reports both, plus which capable profiles are idle, so rerouting is one step.
Read-only: it never reassigns. Routing is a judgement call — the card's nature has
to match the profile, and only a human/orchestrator should decide that.

FAIL-CLOSED: probe errors exit non-zero. Healthy = empty stdout, exit 0.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import yaml

BOARDS_DIR = Path("/home/frank/.hermes/kanban/boards")
PROFILES = Path("/home/frank/.hermes/profiles")
BOARDS = ["sycode-trading", "jarvis-os", "upero", "ai-restaurant"]

# Phrases workers emit when they hit a toolset wall.
CAPABILITY_WALL = (
    "no terminal", "no file read/write", "capability wall", "terminal execution capability",
    "no terminal/shell", "does not have terminal", "no shell tools",
)


class ProbeError(RuntimeError):
    pass


def toolsets(profile: str) -> list[str] | None:
    """Declared toolsets, or None when the profile has no directory at all."""
    d = PROFILES / profile
    if not d.is_dir():
        return None
    cfg = d / "config.yaml"
    if not cfg.is_file():
        return []
    try:
        c = yaml.safe_load(cfg.read_text(errors="replace")) or {}
    except yaml.YAMLError:
        return []
    ts = c.get("toolsets") or c.get("tools") or []
    return list(ts) if isinstance(ts, list) else []


def main() -> int:
    unroutable: dict[str, int] = {}
    walled: dict[str, list[str]] = {}
    no_terminal_load: dict[str, int] = {}

    for board in BOARDS:
        db = BOARDS_DIR / board / "kanban.db"
        if not db.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as e:
            raise ProbeError(f"cannot open {db}: {e}")
        try:
            rows = conn.execute(
                "SELECT id, assignee, status FROM tasks "
                "WHERE status IN ('ready','todo','blocked','running') "
                "AND assignee IS NOT NULL AND assignee <> ''"
            ).fetchall()
            for r in rows:
                ts = toolsets(r["assignee"])
                if ts is None:
                    unroutable[f"{board}:{r['assignee']}"] = unroutable.get(f"{board}:{r['assignee']}", 0) + 1
                    continue
                if "terminal" not in ts:
                    no_terminal_load[f"{board}:{r['assignee']}"] = no_terminal_load.get(f"{board}:{r['assignee']}", 0) + 1

            # cards that already hit the wall and said so
            walls = conn.execute(
                "SELECT t.id, t.assignee, e.payload FROM tasks t "
                "JOIN task_events e ON e.task_id=t.id AND e.kind='blocked' "
                "WHERE t.status='blocked'"
            ).fetchall()
            for w in walls:
                try:
                    reason = (json.loads(w["payload"]) or {}).get("reason") or ""
                except (json.JSONDecodeError, TypeError):
                    continue
                if any(p in reason.lower() for p in CAPABILITY_WALL):
                    walled.setdefault(f"{board}:{w['assignee']}", []).append(w["id"])
        finally:
            conn.close()

    idle_capable = [p.name for p in sorted(PROFILES.iterdir())
                    if p.is_dir() and "terminal" in (toolsets(p.name) or [])][:12]

    if not unroutable and not walled:
        return 0  # healthy -> silent

    print("CAPABILITY ROUTING")
    if unroutable:
        print("  UNROUTABLE (assignee has no profile directory — dispatcher drops silently):")
        for k, v in sorted(unroutable.items(), key=lambda x: -x[1]):
            print(f"    {v:4d}  {k}")
    if walled:
        print("  CAPABILITY WALL (worker dispatched, hit a toolset limit, then blocked):")
        for k, ids in sorted(walled.items(), key=lambda x: -len(x[1])):
            print(f"    {len(ids):4d}  {k}   e.g. {', '.join(ids[:3])}")
    if no_terminal_load:
        top = sorted(no_terminal_load.items(), key=lambda x: -x[1])[:5]
        print("  LOAD ON NON-TERMINAL PROFILES (may be fine — many cards need no shell):")
        for k, v in top:
            print(f"    {v:4d}  {k}")
    print(f"  terminal-capable profiles available: {', '.join(idle_capable)}")
    print("  Reroute with: hermes kanban --board <board> assign <task> <profile>")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProbeError as e:
        print(f"capability_routing_guard: PROBE FAILED (not 'healthy'): {e}", file=sys.stderr)
        sys.exit(1)
