#!/usr/bin/env python3
"""Generate a deterministic Hermes fleet SLO report.

Read-only evidence collector. It scans kanban board SQLite DBs, the global Hermes
cron store, PM/reflection status artifacts, and writes a markdown report with
PASS/WARN/FAIL per firm-grade SLO.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("HOME", "/home/frank"))
HERMES = Path(os.environ.get("HERMES_ROOT", "/home/frank/.hermes"))
BOARDS = HERMES / "kanban" / "boards"
CRON_JOBS = HERMES / "cron" / "jobs.json"
OUT_DEFAULT = Path("/home/frank/uaa-rules/FLEET-SLO-REPORT.md")
PM_STATUS = Path("/home/frank/uaa-rules/PM-ORCHESTRATOR-STATUS.md")
TRIAGE_STATUS = Path("/home/frank/uaa-rules/PENDING-FRANK-TRIAGE.md")
SELF_IMPROVEMENT_LATEST = Path(
    "/home/frank/uaa-rules/self-improvement/state/latest.json"
)
REFLECTION_AUDIT = HERMES / "scripts" / "fleet_reflection_audit.py"
NOW = int(time.time())
WEEK = 7 * 24 * 3600

ACTIVE_BOARDS = {
    "jarvis-os",
    "sycode-trading",
    "upero",
    "sycode-ai",
    "yorkstone-supplies",
}
URGENT_BOARDS = {"jarvis-os", "sycode-trading"}
FAIL_OUTCOMES = {"crashed", "timed_out", "spawn_failed", "failed"}

THRESHOLDS = {
    "task_latency_p90_hours": 24,
    "urgent_task_latency_p50_hours": 2,
    "noncritical_blocked_age_hours": 48,
    "retry_crash_rate": 0.10,
    "review_latency_hours": 12,
    "pm_status_fresh_hours": 1,
    "cron_stale_factor": 2,
    "placeholder_reflections": 0,
    "orphan_ready_todo": 0,
    "delivery_errors": 0,
    "false_pending_frank_after": 0,
    "meta_work_budget": 0.35,
}

META_WORK_PATTERNS = [
    ("review", re.compile(r"\b(review|required-review|guardian|approve|approval|verdict)\b", re.I)),
    ("escalation", re.compile(r"\b(escalat(?:e|ion)|pending[- ]frank|frank[- ]gate|frank[- ]gated|blocker|blocked|unblock)\b", re.I)),
    ("reflection", re.compile(r"\b(reflect(?:ion)?|self[- ]improvement|boris|soul\b)\b", re.I)),
    ("process", re.compile(r"\b(process|governance|hygiene|triage|audit|observability|slo|report|cron|scheduler|kanban|dispatcher|protocol[- ]violation|provider|crash(?:ed)?|hook|completion[- ]gate|classifier|workflow|runbook)\b", re.I)),
]


def utc(ts: int | float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def parse_dt(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def hours_since(ts: int | float | None) -> float | None:
    if not ts:
        return None
    return max(0.0, (NOW - float(ts)) / 3600.0)


def fmt_hours(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "-"
    return f"{value:.2f}h"


def fmt_pct(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "-"
    return f"{value * 100:.1f}%"


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def status(pass_condition: bool, warn_condition: bool = False) -> str:
    if pass_condition:
        return "PASS"
    if warn_condition:
        return "WARN"
    return "FAIL"


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


@dataclass
class BoardMetrics:
    board: str
    exists: bool
    error: str | None = None
    status_counts: dict[str, int] = field(default_factory=dict)
    done7d: int = 0
    latency_count: int = 0
    p50_hours: float | None = None
    p90_hours: float | None = None
    max_hours: float | None = None
    blocked_count: int = 0
    max_blocked_age_hours: float | None = None
    blocked_over_threshold: list[dict[str, Any]] = field(default_factory=list)
    runs7d: int = 0
    failed_runs7d: int = 0
    failure_rate: float | None = None
    review_blocked_count: int = 0
    review_max_age_hours: float | None = None
    review_over_threshold: list[dict[str, Any]] = field(default_factory=list)
    orphan_ready_todo: int = 0
    goal_mode_active: int = 0
    work_mix_goal_product_7d: int = 0
    work_mix_meta_7d: int = 0
    work_mix_meta_ratio: float | None = None
    work_mix_meta_by_type: dict[str, int] = field(default_factory=dict)
    work_mix_examples: list[dict[str, Any]] = field(default_factory=list)


def work_mix_identity_text(title: str | None, assignee: str | None = None) -> str:
    """Return the task-identity surface used for work-mix classification.

    The metric intentionally ignores body/result/comment/run boilerplate. Normal
    implementation tasks often mention guardian review, cron/runtime boundaries,
    or approval gates during closeout; scanning that prose overclassifies product
    work as meta-work. Title plus assignee keeps the signal tied to the card's
    declared work type rather than its lifecycle evidence.
    """
    return "\n".join(part for part in (title or "", assignee or "") if part)


def classify_work_mix(text: str) -> str | None:
    """Return the meta-work subtype for a task identity, or None for goal/product.

    The report is intentionally deterministic and tied to task identity: explicit
    review, escalation, reflection, process, observability, scheduler, cron,
    kanban, SLO, and governance terms in the title/assignee surface count toward
    meta-work. Everything else is treated as direct goal/product work so this
    metric warns on obvious drift without altering dispatch behavior.
    """
    for label, pattern in META_WORK_PATTERNS:
        if pattern.search(text):
            return label
    return None


def work_mix_regression_checks() -> list[dict[str, Any]]:
    """Focused fixtures for reviewer-requested work-mix classification behavior."""
    fixtures = [
        {
            "id": "upero/t_26a14c7b",
            "title": "IMPLEMENT Wave 24 static modal-trigger Yorkstone CMS alias",
            "assignee": "ai-dev",
            "body": "Guardian closeout includes REVIEW_VERDICT and approval evidence.",
            "expected": None,
        },
        {
            "id": "sycode-trading/t_44124ed6",
            "title": "P0 Diagnose Arena paper-intent output without paper strategy registration",
            "assignee": "trading-strategy-dev",
            "body": "Boundary text mentions cron/runtime evidence in the result.",
            "expected": None,
        },
        {
            "id": "example/review",
            "title": "REVIEW: P0 Arena paper-intent diagnosis handoff",
            "assignee": "os-reviewer",
            "body": "Product implementation details are intentionally ignored.",
            "expected": "review",
        },
        {
            "id": "example/governance",
            "title": "GOVERNANCE: install weekly north-star alignment report",
            "assignee": "system-optimizer",
            "body": "No body scan needed.",
            "expected": "process",
        },
        {
            "id": "example/north-star",
            "title": "NORTH-STAR AUDIT: weekly fleet alignment baseline",
            "assignee": "jarvis-os-pm",
            "body": "No body scan needed.",
            "expected": "process",
        },
    ]
    results = []
    for fixture in fixtures:
        identity = work_mix_identity_text(fixture["title"], fixture["assignee"])
        got = classify_work_mix(identity)
        results.append(
            {
                "id": fixture["id"],
                "expected": fixture["expected"],
                "got": got,
                "pass": got == fixture["expected"],
            }
        )
    return results


def blocked_since(
    con: sqlite3.Connection, task_id: str, fallback: int | None
) -> int | None:
    row = con.execute(
        "SELECT created_at FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return int(row[0]) if row and row[0] else fallback


def text_has_review_required(con: sqlite3.Connection, task: sqlite3.Row) -> bool:
    parts = [
        task["title"] or "",
        task["body"] or "",
        task["result"] or "",
        task["last_failure_error"] or "",
    ]
    try:
        parts += [
            r[0] or ""
            for r in con.execute(
                "SELECT body FROM task_comments WHERE task_id=?", (task["id"],)
            )
        ]
        parts += [
            r[0] or ""
            for r in con.execute(
                "SELECT summary FROM task_runs WHERE task_id=?", (task["id"],)
            )
        ]
        parts += [
            r[0] or ""
            for r in con.execute(
                "SELECT error FROM task_runs WHERE task_id=?", (task["id"],)
            )
        ]
    except Exception:
        pass
    return "review-required" in "\n".join(parts).lower()


def board_metrics(db: Path) -> BoardMetrics:
    board = db.parent.name
    if not db.exists():
        return BoardMetrics(board=board, exists=False, status_counts={})
    m = BoardMetrics(
        board=board,
        exists=True,
        status_counts={},
        blocked_over_threshold=[],
        review_over_threshold=[],
    )
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        m.status_counts = {
            r["status"]: int(r["n"])
            for r in con.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status")
        }
        m.done7d = int(
            con.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='done' AND completed_at > ?",
                (NOW - WEEK,),
            ).fetchone()[0]
        )
        latencies = []
        for r in con.execute(
            "SELECT created_at, started_at, completed_at FROM tasks WHERE status='done' AND completed_at > ? AND completed_at IS NOT NULL",
            (NOW - WEEK,),
        ):
            start = r["started_at"] or r["created_at"]
            if start and r["completed_at"] and r["completed_at"] >= start:
                latencies.append((r["completed_at"] - start) / 3600.0)
        m.latency_count = len(latencies)
        m.p50_hours = percentile(latencies, 0.50)
        m.p90_hours = percentile(latencies, 0.90)
        m.max_hours = max(latencies) if latencies else None

        meta_by_type = {label: 0 for label, _ in META_WORK_PATTERNS}
        meta_examples: list[dict[str, Any]] = []
        for t in con.execute(
            "SELECT id,title,assignee FROM tasks WHERE status='done' AND completed_at > ? ORDER BY completed_at DESC",
            (NOW - WEEK,),
        ):
            text = work_mix_identity_text(t["title"], t["assignee"])
            meta_type = classify_work_mix(text)
            if meta_type:
                m.work_mix_meta_7d += 1
                meta_by_type[meta_type] = meta_by_type.get(meta_type, 0) + 1
                if len(meta_examples) < 5:
                    meta_examples.append(
                        {
                            "id": t["id"],
                            "type": meta_type,
                            "title": t["title"],
                        }
                    )
            else:
                m.work_mix_goal_product_7d += 1
        m.work_mix_meta_by_type = {k: v for k, v in meta_by_type.items() if v}
        m.work_mix_examples = meta_examples
        if m.done7d:
            m.work_mix_meta_ratio = m.work_mix_meta_7d / m.done7d

        blocked_ages = []
        for t in con.execute(
            "SELECT * FROM tasks WHERE status='blocked' ORDER BY created_at"
        ):
            since = blocked_since(con, t["id"], t["started_at"] or t["created_at"])
            age = hours_since(since)
            if age is not None:
                blocked_ages.append(age)
                if age > THRESHOLDS["noncritical_blocked_age_hours"]:
                    m.blocked_over_threshold.append(
                        {
                            "id": t["id"],
                            "title": t["title"],
                            "assignee": t["assignee"],
                            "age_hours": age,
                        }
                    )
            if text_has_review_required(con, t):
                m.review_blocked_count += 1
                if age is not None:
                    m.review_max_age_hours = max(m.review_max_age_hours or 0.0, age)
                    if age > THRESHOLDS["review_latency_hours"]:
                        m.review_over_threshold.append(
                            {
                                "id": t["id"],
                                "title": t["title"],
                                "assignee": t["assignee"],
                                "age_hours": age,
                            }
                        )
        m.blocked_count = len(blocked_ages)
        m.max_blocked_age_hours = max(blocked_ages) if blocked_ages else None

        run_rows = list(
            con.execute(
                "SELECT status,outcome,started_at,ended_at,error FROM task_runs WHERE started_at > ? OR ended_at > ?",
                (NOW - WEEK, NOW - WEEK),
            )
        )
        m.runs7d = len(run_rows)
        failed = 0
        for r in run_rows:
            outcome = (r["outcome"] or "").lower()
            st = (r["status"] or "").lower()
            err = (r["error"] or "").lower()
            if (
                outcome in FAIL_OUTCOMES
                or st in FAIL_OUTCOMES
                or "spawn" in outcome
                or "spawn_failed" in err
            ):
                failed += 1
        m.failed_runs7d = failed
        m.failure_rate = (failed / m.runs7d) if m.runs7d else 0.0
        m.orphan_ready_todo = int(
            con.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('ready','todo') AND (assignee IS NULL OR assignee='')"
            ).fetchone()[0]
        )
        m.goal_mode_active = int(
            con.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('ready','todo','running','blocked') AND goal_mode=1"
            ).fetchone()[0]
        )
        con.close()
    except Exception as exc:
        m.error = repr(exc)
    return m


def collect_boards() -> list[BoardMetrics]:
    out = []
    for db in sorted(BOARDS.glob("*/kanban.db")):
        out.append(board_metrics(db))
    return out


def schedule_period_hours(job: dict[str, Any]) -> float | None:
    sched = job.get("schedule") or {}
    if sched.get("kind") == "interval" and sched.get("minutes"):
        return float(sched["minutes"]) / 60.0
    expr = sched.get("expr") or ""
    display = job.get("schedule_display") or sched.get("display") or ""
    fields = expr.split()
    if len(fields) == 5:
        minute, hour, day, month, weekday = fields
        if hour.startswith("*/"):
            return float(hour[2:])
        if minute.startswith("*/"):
            return float(minute[2:]) / 60.0
        if day != "*" or month != "*" or weekday != "*":
            return 24.0 * 7.0
        return 24.0
    text = f"{expr} {display}"
    if "*/" in text:
        m = re.search(r"\*/(\d+)", text)
        if m:
            return float(m.group(1)) / 60.0
    # Conservative approximations for stale detection.
    return None


def cron_stores() -> list[Path]:
    stores = [CRON_JOBS]
    stores.extend(sorted((HERMES / "profiles").glob("*/cron/jobs.json")))
    return [p for p in stores if p.exists()]


def collect_cron() -> dict[str, Any]:
    jobs: list[dict[str, Any]] = []
    stores = cron_stores()
    for store in stores:
        data = load_json(store)
        store_jobs = data.get("jobs", []) if isinstance(data.get("jobs", []), list) else []
        for job in store_jobs:
            if isinstance(job, dict):
                copied = dict(job)
                copied["_store"] = str(store)
                jobs.append(copied)
    enabled = [j for j in jobs if j.get("enabled", True) and j.get("state") != "paused"]
    stale = []
    bad_status = []
    delivery_errors = []
    for j in enabled:
        name = j.get("name") or j.get("id")
        last_ts = parse_dt(j.get("last_run_at"))
        last_age = hours_since(last_ts)
        period = schedule_period_hours(j)
        stale_limit = period * THRESHOLDS["cron_stale_factor"] if period else None
        if (j.get("last_status") or "").lower() not in {"ok", "success"}:
            bad_status.append(
                {
                    "id": j.get("id"),
                    "name": name,
                    "store": j.get("_store"),
                    "last_status": j.get("last_status"),
                    "last_error": j.get("last_error"),
                }
            )
        if j.get("last_delivery_error"):
            delivery_errors.append(
                {
                    "id": j.get("id"),
                    "name": name,
                    "store": j.get("_store"),
                    "last_delivery_error": str(j.get("last_delivery_error"))[:160],
                }
            )
        if last_age is None or (stale_limit is not None and last_age > stale_limit):
            stale.append(
                {
                    "id": j.get("id"),
                    "name": name,
                    "store": j.get("_store"),
                    "last_run_age_hours": last_age,
                    "stale_limit_hours": stale_limit,
                }
            )
    return {
        "stores": [str(p) for p in stores],
        "jobs_total": len(jobs),
        "enabled": len(enabled),
        "bad_status": bad_status,
        "delivery_errors": delivery_errors,
        "stale": stale,
    }


def collect_reflection() -> dict[str, Any]:
    latest = load_json(SELF_IMPROVEMENT_LATEST)
    audit_obj = latest.get("audit")
    audit: dict[str, Any] = audit_obj if isinstance(audit_obj, dict) else {}
    if not audit and REFLECTION_AUDIT.exists():
        try:
            p = subprocess.run(
                [str(REFLECTION_AUDIT)], text=True, capture_output=True, timeout=60
            )
            if p.stdout.strip():
                loaded = json.loads(p.stdout)
                audit = loaded if isinstance(loaded, dict) else {"error": "audit output was not a JSON object", "exit": p.returncode}
            else:
                audit = {"error": p.stderr.strip(), "exit": p.returncode}
        except Exception as exc:
            audit = {"error": repr(exc)}
    return audit


def parse_pm_status() -> dict[str, Any]:
    text = ""
    try:
        text = PM_STATUS.read_text(errors="replace")
    except Exception:
        return {
            "exists": False,
            "age_hours": None,
            "orphans": None,
            "failures": ["missing PM status artifact"],
        }
    m = re.search(r"Updated:\s*([^\n]+)", text)
    ts = parse_dt(m.group(1).strip()) if m else None
    orphan_values = [
        int(x) for x in re.findall(r"\|\s*(\d+)\s*\|\s*$", text, flags=re.M)
    ]
    failures = []
    if "## Failures\n- none" not in text and "## Failure Log\n- none" not in text:
        failures.append("PM status artifact reports failures or unknown")
    if "watchdog cron" in text.lower() and "last_run stale" in text.lower():
        failures.append("PM watchdog cron last_run is stale")
    return {
        "exists": True,
        "age_hours": hours_since(ts),
        "orphans": sum(orphan_values),
        "failures": failures,
    }


def parse_triage() -> dict[str, Any]:
    values: dict[str, Any] = {"critical_ids": set()}
    try:
        text = TRIAGE_STATUS.read_text(errors="replace")
    except Exception:
        return values
    keys = {
        "Pending Frank before": "pending_before",
        "Classified critical-list": "critical",
        "Classified delegated-review": "delegated_review",
        "Classified ambiguous": "ambiguous",
        "False Pending Frank after delegated routing (report metric)": "false_pending_after",
        "Orphan ready/todo assignee=null count": "orphans",
    }
    for label, key in keys.items():
        m = re.search(re.escape(label) + r":\s*(\d+)", text)
        if m:
            values[key] = int(m.group(1))

    # The blocked-age SLO threshold explicitly exempts cards that are
    # critical-classified in the Pending Frank triage artifact. Parse the
    # critical-list section so the SLO report does not fail on intentionally
    # human-gated critical items while still surfacing them in signal/noise.
    section = re.search(
        r"## critical-list\n(?P<body>.*?)(?:\n## |\Z)",
        text,
        flags=re.S,
    )
    if section:
        values["critical_ids"] = set(
            re.findall(r"^-\s+[^|\n]+\|\s*(t_[a-f0-9]+)\s*\|", section.group("body"), flags=re.M)
        )
    return values


def section_statuses(
    boards: list[BoardMetrics],
    cron: dict[str, Any],
    reflection: dict[str, Any],
    pm: dict[str, Any],
    triage: dict[str, int],
) -> dict[str, str]:
    active = [b for b in boards if b.board in ACTIVE_BOARDS]
    task_failures = [
        b
        for b in active
        if b.p90_hours is not None
        and b.p90_hours > THRESHOLDS["task_latency_p90_hours"]
    ]
    urgent_failures = [
        b
        for b in active
        if b.board in URGENT_BOARDS
        and b.p50_hours is not None
        and b.p50_hours > THRESHOLDS["urgent_task_latency_p50_hours"]
    ]
    fail_rate_boards = [
        b
        for b in active
        if b.runs7d and (b.failure_rate or 0) > THRESHOLDS["retry_crash_rate"]
    ]
    critical_raw = triage.get("critical_ids")
    critical_ids = critical_raw if isinstance(critical_raw, set) else set()
    blocked_over = sum(
        1
        for b in active
        for item in (b.blocked_over_threshold or [])
        if item.get("id") not in critical_ids
    )
    review_over = sum(len(b.review_over_threshold or []) for b in active)
    work_mix_over = [
        b
        for b in active
        if b.done7d and (b.work_mix_meta_ratio or 0) > THRESHOLDS["meta_work_budget"]
    ]
    placeholder_count = int(reflection.get("placeholder_reflection_count", 0) or 0)
    total_orphans = sum(b.orphan_ready_todo for b in boards)
    false_pending = triage.get("false_pending_after", 0)
    return {
        "task_latency": status(
            not task_failures and not urgent_failures,
            bool(task_failures or urgent_failures),
        ),
        "blocked_age": status(blocked_over == 0, False),
        "retry_crash_rate": status(not fail_rate_boards, False),
        "review_latency_proxy": status(
            review_over == 0, any(b.review_blocked_count for b in active)
        ),
        "work_mix": status(not work_mix_over, bool(work_mix_over)),
        "cron_health": status(
            not cron["bad_status"]
            and not cron["stale"]
            and not cron["delivery_errors"],
            bool(
                cron["stale"] and not cron["bad_status"] and not cron["delivery_errors"]
            ),
        ),
        "placeholder_reflection": status(
            placeholder_count <= THRESHOLDS["placeholder_reflections"], False
        ),
        "pm_activity": status(
            bool(pm.get("exists"))
            and (
                pm.get("age_hours") is not None
                and pm["age_hours"] <= THRESHOLDS["pm_status_fresh_hours"]
            )
            and total_orphans == 0
            and not pm.get("failures"),
            bool(total_orphans == 0),
        ),
        "signal_noise": status(
            false_pending <= THRESHOLDS["false_pending_frank_after"]
            and not cron["delivery_errors"],
            bool(false_pending > 0 and not cron["delivery_errors"]),
        ),
    }


def render_report(
    boards: list[BoardMetrics],
    cron: dict[str, Any],
    reflection: dict[str, Any],
    pm: dict[str, Any],
    triage: dict[str, int],
) -> str:
    statuses = section_statuses(boards, cron, reflection, pm, triage)
    overall = (
        "FAIL"
        if "FAIL" in statuses.values()
        else ("WARN" if "WARN" in statuses.values() else "PASS")
    )
    active = [b for b in boards if b.board in ACTIVE_BOARDS]
    total_orphans = sum(b.orphan_ready_todo for b in boards)
    lines: list[str] = []
    lines.append("# Fleet SLO Report\n\n")
    lines.append(f"Generated: {utc(NOW)}\n")
    lines.append(f"Host: {os.uname().nodename}\n")
    lines.append(f"Overall: {overall}\n\n")
    lines.append("## Thresholds\n")
    lines.append(
        f"- Task latency: active-board p90 <= {THRESHOLDS['task_latency_p90_hours']}h; urgent-board p50 <= {THRESHOLDS['urgent_task_latency_p50_hours']}h.\n"
    )
    lines.append(
        f"- Blocked age: no blocked card older than {THRESHOLDS['noncritical_blocked_age_hours']}h unless explicitly critical-classified.\n"
    )
    lines.append(
        f"- Retry/crash rate: failed run outcomes < {THRESHOLDS['retry_crash_rate']:.0%} of 7d task runs.\n"
    )
    lines.append(
        f"- Review latency proxy: review-required blocked cards age <= {THRESHOLDS['review_latency_hours']}h.\n"
    )
    lines.append(
        f"- Work-mix: active-board 7d heuristic meta-work completions (review/escalation/reflection/process) <= {THRESHOLDS['meta_work_budget']:.0%}; classification uses task title + assignee only; over-budget boards WARN only.\n"
    )
    lines.append(
        "- Cron health: enabled jobs last_status ok, last run within 2x schedule where parseable, no delivery error.\n"
    )
    lines.append(
        f"- Placeholder reflections: {THRESHOLDS['placeholder_reflections']}. PM artifact freshness <= {THRESHOLDS['pm_status_fresh_hours']}h. Orphan ready/todo cards: {THRESHOLDS['orphan_ready_todo']}.\n\n"
    )

    lines.append("## SLO Summary\n")
    labels = [
        ("Task latency", "task_latency"),
        ("Blocked age", "blocked_age"),
        ("Retry/crash rate", "retry_crash_rate"),
        ("Review latency proxy", "review_latency_proxy"),
        ("Work-mix meta budget", "work_mix"),
        ("Cron execution + delivery", "cron_health"),
        ("Placeholder reflections", "placeholder_reflection"),
        ("PM activity/orphans", "pm_activity"),
        ("Signal/noise", "signal_noise"),
    ]
    lines.append("| Area | Status | Live signal |\n|---|---:|---|\n")
    placeholder_count = int(reflection.get("placeholder_reflection_count", 0) or 0)
    live = {
        "task_latency": "; ".join(
            f"{b.board} p50={fmt_hours(b.p50_hours)} p90={fmt_hours(b.p90_hours)} max={fmt_hours(b.max_hours)}"
            for b in active
        ),
        "blocked_age": "; ".join(
            f"{b.board} blocked={b.blocked_count} max_age={fmt_hours(b.max_blocked_age_hours)}"
            for b in active
        ),
        "retry_crash_rate": "; ".join(
            f"{b.board} {b.failed_runs7d}/{b.runs7d}={((b.failure_rate or 0) * 100):.1f}%"
            for b in active
        ),
        "review_latency_proxy": "; ".join(
            f"{b.board} review_blocked={b.review_blocked_count} max_age={fmt_hours(b.review_max_age_hours)}"
            for b in active
            if b.review_blocked_count
        )
        or "no review-required blocked cards detected",
        "work_mix": "; ".join(
            f"{b.board} meta={b.work_mix_meta_7d}/{b.done7d}={fmt_pct(b.work_mix_meta_ratio)} goal/product={b.work_mix_goal_product_7d}"
            for b in active
        ),
        "cron_health": f"stores={len(cron['stores'])} enabled={cron['enabled']} bad_status={len(cron['bad_status'])} stale={len(cron['stale'])} delivery_errors={len(cron['delivery_errors'])}",
        "placeholder_reflection": f"placeholder_reflection_count={placeholder_count}; profiles={reflection.get('profiles', '-')}",
        "pm_activity": f"PM artifact age={fmt_hours(pm.get('age_hours'))}; orphan_ready_todo={total_orphans}; pm_failures={len(pm.get('failures') or [])}",
        "signal_noise": f"Pending Frank before={triage.get('pending_before', '-')}; critical={triage.get('critical', '-')}; delegated={triage.get('delegated_review', '-')}; ambiguous={triage.get('ambiguous', '-')}; false_pending_after={triage.get('false_pending_after', '-')}; delivery_errors={len(cron['delivery_errors'])}",
    }
    for label, key in labels:
        lines.append(f"| {label} | {statuses[key]} | {live[key]} |\n")

    lines.append("\n## Board Detail\n")
    lines.append(
        "| Board | Done 7d | Latency p50/p90/max | Blocked max age | Runs failed/total | Orphan ready/todo | Goal-mode active |\n|---|---:|---:|---:|---:|---:|---:|\n"
    )
    for b in boards:
        lines.append(
            f"| {b.board} | {b.done7d} | {fmt_hours(b.p50_hours)} / {fmt_hours(b.p90_hours)} / {fmt_hours(b.max_hours)} | {fmt_hours(b.max_blocked_age_hours)} | {b.failed_runs7d}/{b.runs7d} | {b.orphan_ready_todo} | {b.goal_mode_active} |\n"
        )

    lines.append("\n## Work-mix Detail\n")
    lines.append(
        "| Board | Status | Done 7d | Goal/product | Meta-work | Meta ratio | Meta breakdown |\n|---|---:|---:|---:|---:|---:|---|\n"
    )
    for b in boards:
        mix_status = status(
            not b.done7d or (b.work_mix_meta_ratio or 0) <= THRESHOLDS["meta_work_budget"],
            bool(b.done7d),
        )
        breakdown = ", ".join(
            f"{k}={v}" for k, v in sorted(b.work_mix_meta_by_type.items())
        ) or "-"
        lines.append(
            f"| {b.board} | {mix_status} | {b.done7d} | {b.work_mix_goal_product_7d} | {b.work_mix_meta_7d} | {fmt_pct(b.work_mix_meta_ratio)} | {breakdown} |\n"
        )

    over_budget = [
        b
        for b in active
        if b.done7d and (b.work_mix_meta_ratio or 0) > THRESHOLDS["meta_work_budget"]
    ]
    if over_budget:
        lines.append("\n### Work-mix WARN examples\n")
        for b in over_budget:
            lines.append(
                f"- WORK_MIX_WARN {b.board}: meta={b.work_mix_meta_7d}/{b.done7d} ({fmt_pct(b.work_mix_meta_ratio)}) exceeds {THRESHOLDS['meta_work_budget']:.0%} budget.\n"
            )
            for item in b.work_mix_examples:
                lines.append(
                    f"  - {item['type']} {b.board}/{item['id']}: {item['title']}\n"
                )

    lines.append("\n## Exceptions requiring action\n")
    wrote_any = False
    critical_raw = triage.get("critical_ids")
    critical_ids = critical_raw if isinstance(critical_raw, set) else set()
    for b in active:
        for item in b.blocked_over_threshold or []:
            wrote_any = True
            prefix = "CRITICAL_BLOCKED_AGE_EXEMPT" if item.get("id") in critical_ids else "BLOCKED_AGE"
            lines.append(
                f"- {prefix} {b.board}/{item['id']} {fmt_hours(item['age_hours'])}: {item['title']} (@{item.get('assignee') or '-'})\n"
            )
        for item in b.review_over_threshold or []:
            wrote_any = True
            lines.append(
                f"- REVIEW_LATENCY {b.board}/{item['id']} {fmt_hours(item['age_hours'])}: {item['title']} (@{item.get('assignee') or '-'})\n"
            )
        if b.runs7d and (b.failure_rate or 0) > THRESHOLDS["retry_crash_rate"]:
            wrote_any = True
            lines.append(
                f"- RETRY_CRASH_RATE {b.board}: {b.failed_runs7d}/{b.runs7d} = {(b.failure_rate or 0) * 100:.1f}%\n"
            )
    for item in cron["bad_status"]:
        wrote_any = True
        lines.append(
            f"- CRON_STATUS {item['name']} ({item['id']}): store={item.get('store')} last_status={item.get('last_status')} last_error={(item.get('last_error') or '-')[:160]}\n"
        )
    for item in cron["stale"]:
        wrote_any = True
        lines.append(
            f"- CRON_STALE {item['name']} ({item['id']}): store={item.get('store')} last_run_age={fmt_hours(item.get('last_run_age_hours'))} stale_limit={fmt_hours(item.get('stale_limit_hours'))}\n"
        )
    for item in cron["delivery_errors"]:
        wrote_any = True
        lines.append(
            f"- CRON_DELIVERY {item['name']} ({item['id']}): store={item.get('store')} {item['last_delivery_error']}\n"
        )
    if placeholder_count > 0:
        wrote_any = True
        sample = reflection.get("placeholder_reflections") or []
        lines.append(
            f"- PLACEHOLDER_REFLECTIONS count={placeholder_count}; sample={', '.join(sample[:10])}\n"
        )
    if total_orphans:
        wrote_any = True
        lines.append(f"- PM_ORPHANS orphan_ready_todo={total_orphans}\n")
    if not wrote_any:
        lines.append("- none\n")

    lines.append("\n## Evidence sources\n")
    lines.append(f"- Board DBs: {BOARDS}/*/kanban.db\n")
    lines.append(f"- Cron stores: {', '.join(cron['stores'])}\n")
    lines.append(f"- PM status artifact: {PM_STATUS}\n")
    lines.append(f"- Pending-Frank triage artifact: {TRIAGE_STATUS}\n")
    lines.append(
        f"- Reflection state/audit: {SELF_IMPROVEMENT_LATEST}; {REFLECTION_AUDIT}\n"
    )
    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate deterministic fleet SLO markdown report"
    )
    parser.add_argument("--output", default=str(OUT_DEFAULT), help="Report path")
    parser.add_argument(
        "--print-summary", action="store_true", help="Print one-line status summary"
    )
    parser.add_argument(
        "--self-test-work-mix",
        action="store_true",
        help="Run focused work-mix classifier regression fixtures and exit",
    )
    args = parser.parse_args()

    if args.self_test_work_mix:
        checks = work_mix_regression_checks()
        failed = [item for item in checks if not item["pass"]]
        print(json.dumps({"checks": checks, "failed": len(failed)}, indent=2))
        return 1 if failed else 0

    boards = collect_boards()
    cron = collect_cron()
    reflection = collect_reflection()
    pm = parse_pm_status()
    triage = parse_triage()
    report = render_report(boards, cron, reflection, pm, triage)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(report)
    tmp.replace(out)
    if args.print_summary:
        overall = re.search(r"^Overall: (\w+)$", report, re.M)
        print(
            f"FLEET_SLO_REPORT {overall.group(1) if overall else 'UNKNOWN'} output={out}"
        )
    else:
        print(str(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
