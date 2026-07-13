#!/usr/bin/env python3
"""Monthly deterministic Hermes succession maturity metrics collector.

No-agent cron script: reads kanban SQLite boards, writes a dated baseline/report
under the fleet Obsidian vault, updates latest JSON/markdown pointers, and prints
a compact digest for the scheduled SUCCESSION REVIEW agent cycle / humans.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, "/home/frank/.hermes/scripts")
from second_brain_writer import write_json_atomic, write_markdown_atomic

BOARD_ROOT = Path("/home/frank/.hermes/kanban/boards")
BOARD_SLUGS = ("jarvis-os", "sycode-trading", "sycode-ai", "upero", "yorkstone-supplies")
VAULT_DIR = Path("/home/frank/obsidian-fleet-vault/Governance/Succession")
TASK_ID = "t_e5ef4d22"
ACTION_COMMENT_RE = re.compile(
    r"\b(unblock(?:ed|ing)?|accept(?:ed|ance)?|approve(?:d)?|review_verdict|verdict|"
    r"route(?:d|ing)?|assign(?:ed|ing)?|changes_requested|reject(?:ed)?|delegated:)\b",
    re.IGNORECASE,
)
GAP_RE = re.compile(r"\b(gap[- ]?hunt|gap[- ]?fill|gap|detector|regression|watchdog|breaker)\b", re.IGNORECASE)
ESCALATION_RE = re.compile(r"\b(NEEDS FRANK|FRANK ESCALATION|approval|critical-list|critical list|blocked for Frank)\b", re.IGNORECASE)
ACK_RE = re.compile(r"\b(ack(?:nowledged)?|delegated:|route(?:d)?|review_verdict|approve(?:d)?|left blocked|accepted)\b", re.IGNORECASE)
BOARD_ACTION_KINDS = {"unblocked", "promoted", "claimed", "completed", "blocked", "created", "linked", "commented"}


@dataclass
class BoardData:
    slug: str
    db_path: Path
    stats: dict[str, int]
    running: int
    backlog: int
    seat_actions_24h: int
    seat_actions_7d: int
    seat_all_24h: int
    first_run_success_30d: dict[str, Any]
    gap_hunt_30d: int
    verdict_latency_seconds: list[int]
    escalation_ack: dict[str, int]


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def date_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0)


def percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = (len(ordered) - 1) * pct
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[lo]
    return round(ordered[lo] * (hi - idx) + ordered[hi] * (idx - lo))


def seat_actions(conn: sqlite3.Connection, since: int) -> tuple[int, int]:
    rows = conn.execute(
        """
        SELECT author, body
        FROM task_comments
        WHERE created_at >= ? AND author LIKE 'claude-%'
        """,
        (since,),
    ).fetchall()
    all_comments = len(rows)
    action_comments = sum(1 for row in rows if ACTION_COMMENT_RE.search(row["body"] or ""))
    return action_comments, all_comments


def first_run_success(conn: sqlite3.Connection, since: int) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT t.id AS task_id,
               COUNT(r.id) AS run_count,
               MAX(CASE WHEN r.outcome = 'completed' OR r.status IN ('done', 'completed') THEN 1 ELSE 0 END) AS completed_once
        FROM tasks t
        LEFT JOIN task_runs r ON r.task_id = t.id
        WHERE t.created_at >= ?
        GROUP BY t.id
        """,
        (since,),
    ).fetchall()
    eligible = [row for row in rows if int(row["run_count"] or 0) == 1]
    successes = sum(1 for row in eligible if int(row["completed_once"] or 0) == 1)
    return {
        "single_run_tasks": len(eligible),
        "single_run_completed": successes,
        "rate": round(successes / len(eligible), 4) if eligible else None,
    }


def gap_hunt_count(conn: sqlite3.Connection, since: int) -> int:
    task_rows = conn.execute(
        "SELECT title, body FROM tasks WHERE created_at >= ?",
        (since,),
    ).fetchall()
    comment_rows = conn.execute(
        "SELECT body FROM task_comments WHERE created_at >= ?",
        (since,),
    ).fetchall()
    return sum(1 for r in task_rows if GAP_RE.search((r["title"] or "") + "\n" + (r["body"] or ""))) + sum(
        1 for r in comment_rows if GAP_RE.search(r["body"] or "")
    )


def verdict_latencies(conn: sqlite3.Connection, since: int) -> list[int]:
    verdicts = conn.execute(
        """
        SELECT id, task_id, created_at
        FROM task_comments
        WHERE created_at >= ? AND body LIKE '%REVIEW_VERDICT%'
        ORDER BY created_at ASC
        """,
        (since,),
    ).fetchall()
    latencies: list[int] = []
    for verdict in verdicts:
        action = conn.execute(
            """
            SELECT kind, created_at
            FROM task_events
            WHERE task_id = ? AND created_at > ?
              AND kind IN ('unblocked','promoted','claimed','completed','blocked','created','linked','commented')
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (verdict["task_id"], verdict["created_at"]),
        ).fetchone()
        if action:
            latencies.append(max(0, int(action["created_at"]) - int(verdict["created_at"])))
    return latencies


def escalation_ack(conn: sqlite3.Connection, since: int) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT c.task_id, c.created_at, c.body
        FROM task_comments c
        WHERE c.created_at >= ?
        ORDER BY c.created_at ASC
        """,
        (since,),
    ).fetchall()
    escalation_comments = [r for r in rows if ESCALATION_RE.search(r["body"] or "")]
    acked = 0
    for esc in escalation_comments:
        later = conn.execute(
            """
            SELECT body FROM task_comments
            WHERE task_id = ? AND created_at > ?
            ORDER BY created_at ASC
            """,
            (esc["task_id"], esc["created_at"]),
        ).fetchall()
        if any(ACK_RE.search(r["body"] or "") for r in later):
            acked += 1
    return {"escalation_comments": len(escalation_comments), "acked_later": acked, "rate": round(acked / len(escalation_comments), 4) if escalation_comments else None}


def collect_board(slug: str, now: int) -> BoardData:
    db_path = BOARD_ROOT / slug / "kanban.db"
    if not db_path.exists():
        raise FileNotFoundError(f"missing board db: {db_path}")
    with connect_ro(db_path) as conn:
        stats = {row["status"]: int(row["n"]) for row in conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")}
        running = stats.get("running", 0)
        backlog = sum(stats.get(s, 0) for s in ("todo", "ready", "scheduled", "blocked", "running"))
        actions_24, all_24 = seat_actions(conn, now - 86400)
        actions_7d, _all_7d = seat_actions(conn, now - 7 * 86400)
        return BoardData(
            slug=slug,
            db_path=db_path,
            stats=stats,
            running=running,
            backlog=backlog,
            seat_actions_24h=actions_24,
            seat_actions_7d=actions_7d,
            seat_all_24h=all_24,
            first_run_success_30d=first_run_success(conn, now - 30 * 86400),
            gap_hunt_30d=gap_hunt_count(conn, now - 30 * 86400),
            verdict_latency_seconds=verdict_latencies(conn, now - 30 * 86400),
            escalation_ack=escalation_ack(conn, now - 30 * 86400),
        )


def render_markdown(payload: dict[str, Any]) -> str:
    boards = payload["boards"]
    lines = [
        f"# Hermes Succession Maturity Baseline — {payload['date']}",
        "",
        f"Source task: [[{TASK_ID}]]",
        "Ladder: [[Hermes-Succession-Maturity-Ladder]]",
        "",
        "## Executive scorecard",
        "",
        f"- Seat intervention action comments/day (strict `claude-*` comments): **{payload['totals']['seat_action_comments_24h']}** (7d total {payload['totals']['seat_action_comments_7d']}, avg/day {payload['totals']['seat_action_comments_7d_avg_per_day']}).",
        f"- Board self-feed ratio: **{payload['totals']['self_feed_ratio']}** ({payload['totals']['boards_with_backlog_and_running']}/{payload['totals']['boards_with_backlog']} boards with backlog also have at least one running worker).",
        f"- First-run success rate, 30d single-run tasks: **{payload['totals']['first_run_success_rate_30d']}** ({payload['totals']['first_run_success_30d']['single_run_completed']}/{payload['totals']['first_run_success_30d']['single_run_tasks']}).",
        f"- Verdict-consumption latency, 30d: median **{payload['totals']['verdict_latency_p50_seconds']}s**, p90 **{payload['totals']['verdict_latency_p90_seconds']}s**, sample {payload['totals']['verdict_latency_samples']} verdicts with a later board action.",
        f"- Gap-hunt find-rate, 30d: **{payload['totals']['gap_hunt_30d']}** gap/detector/regression hits ({payload['totals']['gap_hunt_30d_per_day']} per day).",
        f"- Escalation-ladder acknowledgment rate, 30d: **{payload['totals']['escalation_ack_rate_30d']}** ({payload['totals']['escalation_ack_30d']['acked_later']}/{payload['totals']['escalation_ack_30d']['escalation_comments']}).",
        "",
        "## Board detail",
        "",
        "| Board | Status counts | Running | Backlog | Seat actions 24h / 7d | First-run success 30d | Gap-hunt hits 30d | Verdict p50/p90 | Escalation ack |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for b in boards:
        fr = b["first_run_success_30d"]
        esc = b["escalation_ack"]
        lines.append(
            f"| {b['slug']} | `{json.dumps(b['stats'], sort_keys=True)}` | {b['running']} | {b['backlog']} | {b['seat_actions_24h']} / {b['seat_actions_7d']} | {fr['single_run_completed']}/{fr['single_run_tasks']} ({fr['rate']}) | {b['gap_hunt_30d']} | {b['verdict_latency_p50_seconds']}s/{b['verdict_latency_p90_seconds']}s | {esc['acked_later']}/{esc['escalation_comments']} ({esc['rate']}) |"
        )
    lines += [
        "",
        "## Metric definitions used by the collector",
        "",
        "- **Seat interventions/day:** strict count of `task_comments.author LIKE 'claude-%'` whose body matches unblock/accept/approve/verdict/route/assign/change-request/reject/delegated action terms. The report also records all `claude-*` comments for audit in JSON.",
        "- **Verdict-consumption latency:** seconds from a `REVIEW_VERDICT` comment to the next same-task board event in `{unblocked,promoted,claimed,completed,blocked,created,linked,commented}`. This is router/board-consumption evidence, not code quality.",
        "- **Board self-feed ratio:** current boards with backlog (`todo+ready+scheduled+blocked+running > 0`) that also have `running > 0`. Target trend is toward every non-empty board keeping itself fed without seat nudges.",
        "- **First-run success rate:** tasks created in the last 30 days that had exactly one worker run and that run completed.",
        "- **Gap-hunt find-rate:** count of last-30-day task/comment text hits for gap-hunt/gap-fill/gap/detector/regression/watchdog/breaker language. Healthy maturity should move from high discovery to lower repeated-regression discovery while preserving meaningful detector output.",
        "- **Escalation-ladder acknowledgment rate:** comments mentioning Frank/approval/critical-list escalation with a later same-task comment containing acknowledgment/delegated/route/review/approval wording.",
        "",
        "## Collector artifacts",
        "",
        f"- JSON: `{payload['json_path']}`",
        f"- Markdown: `{payload['markdown_path']}`",
        "- Latest JSON pointer: `/home/frank/obsidian-fleet-vault/Governance/Succession/succession-maturity-latest.json`",
        "- Latest Markdown pointer: `/home/frank/obsidian-fleet-vault/Governance/Succession/succession-maturity-latest.md`",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    now = int(time.time())
    generated_at = utc_ts()
    stamp = date_stamp()
    VAULT_DIR.mkdir(parents=True, exist_ok=True)

    board_data = [collect_board(slug, now) for slug in BOARD_SLUGS]
    boards: list[dict[str, Any]] = []
    all_latencies: list[int] = []
    first_run_total = {"single_run_tasks": 0, "single_run_completed": 0}
    escalation_total = {"escalation_comments": 0, "acked_later": 0}
    for b in board_data:
        all_latencies.extend(b.verdict_latency_seconds)
        first_run_total["single_run_tasks"] += b.first_run_success_30d["single_run_tasks"]
        first_run_total["single_run_completed"] += b.first_run_success_30d["single_run_completed"]
        escalation_total["escalation_comments"] += b.escalation_ack["escalation_comments"]
        escalation_total["acked_later"] += b.escalation_ack["acked_later"]
        boards.append({
            "slug": b.slug,
            "db_path": str(b.db_path),
            "stats": b.stats,
            "running": b.running,
            "backlog": b.backlog,
            "seat_actions_24h": b.seat_actions_24h,
            "seat_actions_7d": b.seat_actions_7d,
            "seat_all_comments_24h": b.seat_all_24h,
            "first_run_success_30d": b.first_run_success_30d,
            "gap_hunt_30d": b.gap_hunt_30d,
            "verdict_latency_samples": len(b.verdict_latency_seconds),
            "verdict_latency_p50_seconds": percentile(b.verdict_latency_seconds, 0.5),
            "verdict_latency_p90_seconds": percentile(b.verdict_latency_seconds, 0.9),
            "escalation_ack": b.escalation_ack,
        })

    boards_with_backlog = sum(1 for b in board_data if b.backlog > 0)
    boards_with_backlog_and_running = sum(1 for b in board_data if b.backlog > 0 and b.running > 0)
    gap_total = sum(b.gap_hunt_30d for b in board_data)
    seat_24 = sum(b.seat_actions_24h for b in board_data)
    seat_7d = sum(b.seat_actions_7d for b in board_data)
    first_rate = round(first_run_total["single_run_completed"] / first_run_total["single_run_tasks"], 4) if first_run_total["single_run_tasks"] else None
    esc_rate = round(escalation_total["acked_later"] / escalation_total["escalation_comments"], 4) if escalation_total["escalation_comments"] else None

    json_path = VAULT_DIR / f"{stamp}-succession-maturity-baseline.json"
    md_path = VAULT_DIR / f"{stamp}-succession-maturity-baseline.md"
    payload: dict[str, Any] = {
        "date": stamp,
        "generated_at": generated_at,
        "task_id": TASK_ID,
        "boards": boards,
        "totals": {
            "seat_action_comments_24h": seat_24,
            "seat_action_comments_7d": seat_7d,
            "seat_action_comments_7d_avg_per_day": round(seat_7d / 7, 2),
            "boards_with_backlog": boards_with_backlog,
            "boards_with_backlog_and_running": boards_with_backlog_and_running,
            "self_feed_ratio": round(boards_with_backlog_and_running / boards_with_backlog, 4) if boards_with_backlog else None,
            "first_run_success_30d": first_run_total,
            "first_run_success_rate_30d": first_rate,
            "gap_hunt_30d": gap_total,
            "gap_hunt_30d_per_day": round(gap_total / 30, 2),
            "verdict_latency_samples": len(all_latencies),
            "verdict_latency_p50_seconds": percentile(all_latencies, 0.5),
            "verdict_latency_p90_seconds": percentile(all_latencies, 0.9),
            "verdict_latency_mean_seconds": round(statistics.mean(all_latencies), 2) if all_latencies else None,
            "escalation_ack_30d": escalation_total,
            "escalation_ack_rate_30d": esc_rate,
        },
    }
    payload["json_path"] = str(json_path)
    payload["markdown_path"] = str(md_path)

    write_json_atomic(json_path, payload)
    write_json_atomic(VAULT_DIR / "succession-maturity-latest.json", payload)
    md_text = render_markdown(payload)
    properties = {
        "title": f"Hermes Succession Maturity Baseline — {stamp}",
        "type": "task-evidence",
        "status": "active",
        "created": stamp,
        "updated": stamp,
        "confidence": "high",
        "tags": ["fleet", "governance", "succession", "maturity", "metrics"],
        "sources": ["/home/frank/.hermes/kanban/boards"],
        "project": "control-plane",
        "owners": ["jarvis"],
        "knowledge_tier": "evidence",
        "generated": True,
        "generator": "succession_maturity_metrics.py",
        "kanban_task": TASK_ID,
        "generated_at": generated_at,
    }
    write_markdown_atomic(md_path, md_text, **properties)
    write_markdown_atomic(VAULT_DIR / "succession-maturity-latest.md", md_text, **properties)

    print(
        "SUCCESSION_MATURITY_BASELINE "
        + json.dumps(
            {
                "generated_at": generated_at,
                "json": str(json_path),
                "markdown": str(md_path),
                "seat_actions_24h": seat_24,
                "seat_actions_7d_avg_per_day": round(seat_7d / 7, 2),
                "self_feed_ratio": payload["totals"]["self_feed_ratio"],
                "first_run_success_rate_30d": first_rate,
                "verdict_latency_p50_seconds": payload["totals"]["verdict_latency_p50_seconds"],
                "gap_hunt_30d_per_day": payload["totals"]["gap_hunt_30d_per_day"],
                "escalation_ack_rate_30d": esc_rate,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - cron must fail visibly
        print("SUCCESSION_MATURITY_COLLECTOR_FAIL " + json.dumps({"error": type(exc).__name__ + ": " + str(exc)}, sort_keys=True), file=sys.stderr)
        raise
