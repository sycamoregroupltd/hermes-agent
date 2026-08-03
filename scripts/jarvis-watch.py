#!/usr/bin/env python3
"""Print a live Jarvis fleet summary from native Hermes logs + Kanban boards.

Read-only. Designed for the jarvis-watch skill used by Jarvis/Elon.
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(os.environ.get("JARVIS_HERMES_ROOT", "/home/frank/.hermes"))
FEED = ROOT / "logs" / "fleet-activity.log"
BOARDS = ROOT / "kanban" / "boards"


@dataclass
class Run:
    board: str
    task_id: str
    title: str
    profile: str
    status: str
    started_at: int
    last_heartbeat_at: int | None
    worker_pid: int | None


def age(ts: int | None, now: int | None = None) -> str:
    if not ts:
        return "?"
    now = now or int(time.time())
    s = max(0, now - int(ts))
    if s < 90:
        return f"{s}s"
    m = s // 60
    if m < 90:
        return f"{m}m"
    h = m // 60
    if h < 48:
        return f"{h}h{m%60:02d}m"
    d = h // 24
    return f"{d}d{h%24}h"


def read_feed(n: int = 80) -> list[str]:
    try:
        lines = FEED.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return []
    return lines[-n:]


def running_runs() -> list[Run]:
    out: list[Run] = []
    if not BOARDS.exists():
        return out
    for db in sorted(BOARDS.glob("*/kanban.db")):
        board = db.parent.name
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT r.task_id, COALESCE(r.profile,t.assignee,'-') AS profile,
                       r.status, r.started_at,
                       COALESCE(r.last_heartbeat_at,t.last_heartbeat_at) AS last_heartbeat_at,
                       COALESCE(r.worker_pid,t.worker_pid) AS worker_pid,
                       t.title
                FROM task_runs r
                LEFT JOIN tasks t ON t.id = r.task_id
                WHERE r.status='running' AND r.ended_at IS NULL
                ORDER BY r.started_at DESC
                """
            ).fetchall()
            con.close()
        except Exception:
            continue
        for r in rows:
            out.append(
                Run(
                    board=board,
                    task_id=r["task_id"],
                    title=(r["title"] or "(missing title)")[:96],
                    profile=r["profile"] or "-",
                    status=r["status"],
                    started_at=int(r["started_at"] or 0),
                    last_heartbeat_at=int(r["last_heartbeat_at"] or 0) if r["last_heartbeat_at"] else None,
                    worker_pid=int(r["worker_pid"] or 0) if r["worker_pid"] else None,
                )
            )
    return out


def recent_sessions(limit: int = 5) -> list[str]:
    cmd = ["hermes", "sessions", "list"]
    env = os.environ.copy()
    env.setdefault("HOME", "/home/frank")
    try:
        p = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=8)
    except Exception as exc:
        return [f"sessions list unavailable: {exc}"]
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "unknown error").strip().splitlines()
        return ["sessions list failed: " + (msg[-1] if msg else "unknown error")]
    lines = [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]
    # Keep useful non-header lines; the exact CLI format changes across versions.
    useful = [ln for ln in lines if not re.match(r"^(Recent sessions|Session ID|[-=]+)$", ln, re.I)]
    return useful[:limit] or ["no recent sessions shown"]


def main() -> int:
    now = int(time.time())
    runs = running_runs()
    feed = read_feed()
    print("FLEET LIVE")
    print(f"time={time.strftime('%Y-%m-%d %H:%M:%S %z')} feed={FEED}")
    print()
    print(f"RUNNING KANBAN AGENTS ({len(runs)})")
    if not runs:
        print("- none found in running task_runs")
    else:
        for r in sorted(runs, key=lambda x: x.started_at):
            hb = age(r.last_heartbeat_at, now) if r.last_heartbeat_at else "none"
            pid = r.worker_pid if r.worker_pid else "-"
            print(
                f"- {r.profile} board={r.board} task={r.task_id} pid={pid} "
                f"age={age(r.started_at, now)} heartbeat={hb} :: {r.title}"
            )
    print()
    print("RECENT FLEET ACTIVITY")
    if not feed:
        print("- no fleet-activity.log lines found")
    else:
        for ln in feed[-12:]:
            print(f"- {ln}")
    print()
    print("RECENT SESSIONS (use: hermes sessions export - --session-id <id>)")
    for ln in recent_sessions():
        print(f"- {ln}")
    print()
    print("DEEPEN")
    print("- Tail a task: hermes kanban --board <board> tail <task_id>")
    print("- Read a session: hermes sessions export - --session-id <session_id>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
