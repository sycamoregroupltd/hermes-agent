#!/usr/bin/env python3
"""phantom_assignee_probe.py — structural monitor for kanban cards assigned
to profiles that do not exist (t_96e0e791).

Why: the dispatcher only spawns cards whose assignee is a real profile
directory (or a registered external seat). A card assigned to a phantom
name (e.g. a schema example token copied verbatim by a weak model) is never
dispatched and never alerts — it just sits in ready/review forever.

What: opens every board DB read-only (``?mode=ro`` — NOT immutable=1, so the
WAL is honoured; see sqlite-immutable-ignores-wal), lists open cards whose
assignee is neither a directory under ~/.hermes/profiles, nor in a small
allowlist of known seats, nor prefixed ``external-``. One line per hit on
stdout. Exit code is ALWAYS 0: signal travels via stdout per the no-agent
cron doctrine. Named consumer: jarvis-os-pm.

Usage:
    phantom_assignee_probe.py            # live probe
    phantom_assignee_probe.py --selftest # fabricate a phantom row, assert hit
    phantom_assignee_probe.py --json     # machine-readable list

stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
# Cron runs under a profile-scoped HERMES_HOME, while Kanban boards and the
# profile roster remain fleet-global. Resolve that layout without changing the
# caller's environment; direct runs from the fleet root still take the first
# candidate.
def _fleet_home(home: Path) -> Path:
    for candidate in (home, home.parent.parent):
        if (candidate / "kanban" / "boards").is_dir() and (candidate / "profiles").is_dir():
            return candidate
    return home

FLEET_HOME = _fleet_home(HERMES_HOME)
BOARDS_DIR = FLEET_HOME / "kanban" / "boards"
PROFILES_DIR = FLEET_HOME / "profiles"
ALLOWLIST = frozenset(
    {"fable", "frank", "jarvis", "elon", "grok", "claude", "codex", "default"}
)
EXTERNAL_PREFIX = "external-"
CLOSED_STATUSES = ("done", "archived", "cancelled")


def known_profiles(profiles_dir: Path = PROFILES_DIR) -> set[str]:
    if not profiles_dir.is_dir():
        return set()
    out = set()
    for p in profiles_dir.iterdir():
        # symlinked profiles (sycode-trading -> sycode-trading-pm) count.
        if p.is_dir() and not (p / ".deleted").exists():
            out.add(p.name)
    return out


def is_phantom(assignee: str | None, profiles: set[str]) -> bool:
    if assignee is None:
        return False  # unassigned is a different class; not this probe's job
    name = assignee.strip()
    if not name:
        return False
    low = name.lower()
    if low in profiles or low in ALLOWLIST or low.startswith(EXTERNAL_PREFIX):
        return False
    return True


def scan_board(db_path: Path, board: str, profiles: set[str]) -> list[dict]:
    uri = f"file:{db_path}?mode=ro"
    hits: list[dict] = []
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as e:
        return [{"board": board, "error": f"open failed: {e}"}]
    try:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(CLOSED_STATUSES))
        rows = conn.execute(
            f"SELECT id, assignee, status, title, created_at FROM tasks "
            f"WHERE status NOT IN ({placeholders}) AND assignee IS NOT NULL",
            CLOSED_STATUSES,
        ).fetchall()
    except sqlite3.Error as e:
        return [{"board": board, "error": f"query failed: {e}"}]
    finally:
        conn.close()
    for r in rows:
        if is_phantom(r["assignee"], profiles):
            hits.append({
                "board": board,
                "id": r["id"],
                "assignee": r["assignee"],
                "status": r["status"],
                "created_at": r["created_at"],
                "title": (r["title"] or "")[:80],
            })
    return hits


def probe(boards_dir: Path = BOARDS_DIR, profiles_dir: Path = PROFILES_DIR) -> list[dict]:
    profiles = known_profiles(profiles_dir)
    hits: list[dict] = []
    if not boards_dir.is_dir():
        return [{"board": "*", "error": f"boards dir missing: {boards_dir}"}]
    for bdir in sorted(boards_dir.iterdir()):
        # skip _archived and dot-prefixed backup snapshots (.bak_*): not live
        if not bdir.is_dir() or bdir.name.startswith(("_", ".")):
            continue
        db = bdir / "kanban.db"
        if not db.is_file():
            continue
        hits.extend(scan_board(db, bdir.name, profiles))
    return hits


def emit(hits: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(hits))
        return
    for h in hits:
        if "error" in h:
            print(f"PHANTOM-ASSIGNEE-PROBE ERROR board={h['board']} {h['error']}")
        else:
            print(
                f"PHANTOM-ASSIGNEE board={h['board']} id={h['id']} "
                f"assignee={h['assignee']!r} status={h['status']} "
                f"created={h['created_at']} title={h['title']!r}"
            )
    if not hits:
        print("PHANTOM-ASSIGNEE-PROBE OK 0 hits")


def selftest() -> int:
    """Fabricate a temp HERMES_HOME with one real profile and a board that
    holds one phantom card, one real card, one external seat card and one
    closed phantom card; assert exactly the open phantom is reported."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "profiles" / "real-profile").mkdir(parents=True)
        bdir = root / "kanban" / "boards" / "selftest"
        bdir.mkdir(parents=True)
        db = bdir / "kanban.db"
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, "
            "status TEXT, created_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO tasks VALUES (?,?,?,?,?)",
            [
                ("t_phantom1", "stranded", "reviewer", "review", "2026-09-01"),
                ("t_real1", "fine", "real-profile", "ready", "2026-09-01"),
                ("t_ext1", "fine", "external-claude-x", "ready", "2026-09-01"),
                ("t_allow1", "fine", "fable", "ready", "2026-09-01"),
                ("t_closed1", "old", "writer", "done", "2026-09-01"),
            ],
        )
        conn.commit()  # stays in WAL until checkpoint -> mode=ro must see it
        conn.close()
        hits = probe(root / "kanban" / "boards", root / "profiles")
        ids = sorted(h.get("id") for h in hits)
        ok = ids == ["t_phantom1"]
        print(f"SELFTEST {'PASS' if ok else 'FAIL'} hits={ids} expected=['t_phantom1']")
        return 0 if ok else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        rc = selftest()
        # exit 0 always is the live-probe contract; selftest is the one place
        # a non-zero code is meaningful (it runs by hand / in CI, not cron).
        return rc
    emit(probe(), a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
