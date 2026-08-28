#!/home/frank/.hermes/hermes-agent/venv/bin/python3
"""Kanban blocker -> voice escalation cron for DGX Jarvis.

Scans configured kanban boards for recently blocked tasks that require Frank-level
input, deduplicates by task/comment evidence, and triggers the jarvis-voice
outbound-call helper. Designed for Hermes cron no-agent mode: silent when there
is nothing new to report.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv("/home/frank/.hermes/.env")
load_dotenv("/home/frank/.hermes/profiles/jarvis-voice/.env")

BOARDS = [
    b.strip()
    for b in os.environ.get(
        "VOICE_ESCALATION_BOARDS", "upero,sycode-ai,sycode-trading,jarvis-os"
    ).split(",")
    if b.strip()
]
BOARD_ROOT = Path("/home/frank/.hermes/kanban/boards")
STATE_PATH = Path(
    os.environ.get(
        "VOICE_ESCALATION_STATE",
        "/home/frank/.hermes/profiles/jarvis-voice/state/kanban_voice_escalation_state.json",
    )
)
MAX_AGE_MINUTES = int(os.environ.get("VOICE_ESCALATION_MAX_AGE_MIN", "60"))
CALL_SCRIPT = "/home/frank/.hermes/profiles/jarvis-voice/bin/outbound-call.py"
ESCALATION_AUTHOR = os.environ.get("VOICE_ESCALATION_AUTHOR", "kanban-voice-escalation")

# Frank-critical / owner-decision language. The script is read-only until it
# invokes the outbound-call helper; it does not mutate policy or task state.
FRANK_LEVEL_KEYWORDS = [
    "frank approval",
    "needs-approval",
    "needs approval",
    "owner approval",
    "owner policy-boundary",
    "policy boundary",
    "standing rule",
    "your call",
    "live capital",
    "stage 5",
    "real-money",
    "payment",
    "credential",
    "credentials & secrets",
    "secrets blocker",
    "ssh key",
    "authorized_keys",
    "production deploy",
    "irreversible",
    "new spend",
    "risk-rail",
    "proof-boundary runtime",
    "deploy/activation",
]

RECENT_ESCALATION_MARKERS = [
    "voice escalation placed",
    "voice-escalation: sent",
    "call sid",
    "twilio call sid",
]


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def utc_now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def latest_block_time(
    conn: sqlite3.Connection, task_id: str, fallback: int | None
) -> int | None:
    row = conn.execute(
        "SELECT MAX(created_at) AS ts FROM task_events WHERE task_id=? AND kind='blocked'",
        (task_id,),
    ).fetchone()
    if row and row["ts"] is not None:
        return int(row["ts"])
    return int(fallback) if fallback is not None else None


def fetch_recent_comments(
    conn: sqlite3.Connection, task_id: str, limit: int = 6
) -> list[dict]:
    rows = conn.execute(
        "SELECT author, body, created_at FROM task_comments WHERE task_id=? ORDER BY created_at DESC LIMIT ?",
        (task_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def already_escalated(comments: list[dict]) -> bool:
    for c in comments:
        text = f"{c.get('author', '')} {c.get('body', '')}".lower()
        if "dry-run" in text or "attempting outbound call" in text:
            continue
        if "call sid" in text or "twilio call sid" in text or "call initiated" in text:
            return True
        if "voice escalation placed" in text:
            return True
    return False


def fetch_blocked_tasks(board: str, now_epoch: int) -> list[dict]:
    db = BOARD_ROOT / board / "kanban.db"
    if not db.exists():
        return []
    cutoff = now_epoch - MAX_AGE_MINUTES * 60
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, title, body, assignee, status, created_at, started_at,
                   last_heartbeat_at, last_failure_error, result
            FROM tasks
            WHERE status='blocked'
            """
        ).fetchall()
        tasks: list[dict] = []
        for row in rows:
            item = dict(row)
            block_ts = latest_block_time(
                conn, item["id"], item.get("started_at") or item.get("created_at")
            )
            if block_ts is None or block_ts < cutoff:
                continue
            comments = fetch_recent_comments(conn, item["id"])
            item["board"] = board
            item["blocked_at"] = block_ts
            item["comments"] = comments
            item["latest_comment_text"] = "\n".join(
                c.get("body") or "" for c in comments[:3]
            )
            tasks.append(item)
        conn.close()
        return tasks
    except Exception as exc:
        print(f"ERROR reading {board}: {exc}")
        return []


def is_frank_level(task: dict) -> bool:
    text = " ".join(
        str(task.get(k) or "")
        for k in [
            "title",
            "body",
            "last_failure_error",
            "result",
            "latest_comment_text",
        ]
    ).lower()
    return any(keyword in text for keyword in FRANK_LEVEL_KEYWORDS)


def make_message(task: dict) -> str:
    title = (task.get("title") or "untitled")[:90]
    return (
        f"Frank, Jarvis here. A kanban blocker needs your decision: "
        f"{task['board']} {task['id']} — {title}."
    )


def call_voice(message: str, dry_run: bool) -> tuple[bool, str]:
    cmd = [CALL_SCRIPT]
    if dry_run:
        cmd.append("--dry-run")
    cmd.append(message)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    output = (result.stdout + result.stderr).strip()
    success = result.returncode == 0 and (
        dry_run
        or "Call initiated:" in result.stdout
        or "Call initiated successfully" in result.stdout
    )
    return success, output


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Escalate recent Frank-level kanban blockers by voice call."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not place calls; invoke outbound helper in dry-run mode.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print no-op diagnostics instead of staying silent.",
    )
    args = parser.parse_args()

    # Dry-runs must not suppress the real escalation job. Use an empty in-memory
    # state and skip persistence so verification probes never mark blockers as handled.
    state = {} if args.dry_run else load_state()
    now_epoch = utc_now_epoch()
    now_iso = datetime.now(timezone.utc).isoformat()
    escalations: list[dict] = []

    for board in BOARDS:
        for task in fetch_blocked_tasks(board, now_epoch):
            key = f"{board}:{task['id']}:{task.get('blocked_at')}"
            if key in state:
                continue
            if already_escalated(task.get("comments", [])):
                state[key] = {
                    "skipped_at": now_iso,
                    "reason": "already_escalated_comment",
                }
                continue
            if not is_frank_level(task):
                continue

            message = make_message(task)
            try:
                success, output = call_voice(message, args.dry_run)
            except Exception as exc:
                success, output = False, f"ERROR calling for {task['id']}: {exc}"

            state[key] = {
                "called_at": now_iso,
                "board": board,
                "task": task["id"],
                "blocked_at": task.get("blocked_at"),
                "success": success,
                "dry_run": args.dry_run,
                "output": output[-500:],
            }
            escalations.append(
                {
                    "board": board,
                    "task": task["id"],
                    "title": task.get("title") or "",
                    "success": success,
                    "dry_run": args.dry_run,
                    "output": output,
                }
            )

    if not args.dry_run:
        save_state(state)

    if escalations:
        prefix = "DRY-RUN " if args.dry_run else ""
        print(f"{prefix}ESCALATED {len(escalations)} Frank-level blocker(s):")
        for e in escalations:
            print(
                f"  {e['board']} {e['task']} | success={e['success']} | {e['title'][:90]}"
            )
            if e.get("output"):
                print(f"    helper: {e['output'][:300]}")
    elif args.verbose:
        print("No new recent Frank-level blockers to escalate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
