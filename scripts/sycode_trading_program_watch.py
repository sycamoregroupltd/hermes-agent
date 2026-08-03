#!/usr/bin/env python3
"""Watch Sycode world-class trading program cards for state changes.
No-agent cron script: prints only on first baseline, state changes, completions, or blockers.
"""

import json
import time
from pathlib import Path
from hermes_cli import kanban_db as kb
DB = Path("/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db")
STATE = Path("/home/frank/.hermes/cron/state/sycode_trading_program_watch.json")
TASK_IDS = [
    "t_89e6db96",  # parent orchestration
    "t_73e1df2c",  # paper execution starvation review
    "t_52f92f47",  # data ledger
    "t_16e36b27",  # strategy workbench
    "t_07217d8d",  # risk sizing
    "t_a9fac9e4",  # Hyperliquid preflight/runbook
    "t_53b74570",  # guardrail monitor
]

now = int(time.time())
if not DB.exists():
    print(f"SYCODE_PROGRAM_WATCH ERROR: board DB missing at {DB}")
    raise SystemExit(0)

con = kb.connect(db_path=DB)
rows = con.execute(
    f"select id,title,status,assignee,worker_pid,last_heartbeat_at,last_failure_error,completed_at from tasks where id in ({','.join('?' for _ in TASK_IDS)}) order by priority desc",
    TASK_IDS,
).fetchall()
current = {}
for r in rows:
    pid = r["worker_pid"]
    alive = bool(pid and Path(f"/proc/{pid}").exists())
    hb = r["last_heartbeat_at"]
    current[r["id"]] = {
        "title": r["title"],
        "status": r["status"],
        "assignee": r["assignee"],
        "worker_pid": pid,
        "alive": alive,
        "heartbeat_age_s": (now - hb) if hb else None,
        "completed_at": r["completed_at"],
        "last_failure_error": r["last_failure_error"],
    }

STATE.parent.mkdir(parents=True, exist_ok=True)
old = {}
if STATE.exists():
    try:
        old = json.loads(STATE.read_text())
    except Exception:
        old = {}

lines = []
if not old:
    lines.append("SYCODE_PROGRAM_WATCH baseline recorded")

old_tasks = old.get("tasks", {}) if isinstance(old, dict) else {}
for tid, cur in current.items():
    prev = old_tasks.get(tid)
    if not prev:
        lines.append(f"NEW {tid}: {cur['status']} {cur['assignee']} — {cur['title']}")
        continue
    changed = []
    for k in ("status", "assignee", "worker_pid"):
        if prev.get(k) != cur.get(k):
            changed.append(f"{k}:{prev.get(k)}->{cur.get(k)}")
    if changed:
        lines.append(f"CHANGE {tid}: {'; '.join(changed)} — {cur['title']}")
    if cur["status"] == "blocked" and prev.get("status") != "blocked":
        err = (cur.get("last_failure_error") or "")[:180].replace("\n", " ")
        lines.append(f"BLOCKED {tid}: {cur['assignee']} — {cur['title']} {err}")
    if cur["status"] == "done" and prev.get("status") != "done":
        lines.append(f"DONE {tid}: {cur['title']}")
    if cur["status"] == "running" and cur.get("worker_pid") and not cur.get("alive"):
        lines.append(
            f"STALE_PID {tid}: pid {cur['worker_pid']} not alive — {cur['title']}"
        )

missing = sorted(set(TASK_IDS) - set(current))
for tid in missing:
    lines.append(f"MISSING {tid}: task not found in board DB")

STATE.write_text(
    json.dumps({"updated_at": now, "tasks": current}, indent=2, sort_keys=True)
)
if lines:
    print("\n".join(lines))
