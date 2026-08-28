#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Deterministic collector for the DGX Hermes fleet self-improvement loop.

Designed for two Hermes cron patterns:
- as a pre-run script on an LLM job: final line is {"wakeAgent": bool, "context": ...}
- as a no-agent watchdog: set SELF_IMPROVEMENT_COLLECT_MODE=no-agent; emits nothing unless material state changed.

No LLM calls, no board mutations, no cron creation.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/home/frank/.hermes")
STATE_DIR = Path("/home/frank/uaa-rules/self-improvement/state")
LATEST = STATE_DIR / "latest.json"
PREV = STATE_DIR / "previous.json"
EVENTS = STATE_DIR / "events.jsonl"
PROFILES = ROOT / "profiles"
BOARDS = ROOT / "kanban" / "boards"
AUDIT = ROOT / "scripts" / "fleet_reflection_audit.py"
SCORER = ROOT / "scripts" / "reflection_quality_score.py"
PLACEHOLDER = "First scheduled cycle should replace this placeholder"
ACTIVE_BOARDS = ["jarvis-os", "sycode-trading", "upero", "sycode-ai"]
CORE_PROFILE_HINTS = [
    "jarvis",
    "elon",
    "guardian",
    "jarvis-os-pm",
    "sycode-trading-pm",
    "upero-pm",
    "builder",
    "os-reviewer",
    "trading-strategy-dev",
    "trading-risk-reviewer",
]
DUE_SECONDS = int(os.environ.get("SELF_IMPROVEMENT_DUE_SECONDS", "21600"))  # 6h
MODE = os.environ.get("SELF_IMPROVEMENT_COLLECT_MODE", "precheck")


def run_json(
    cmd: list[str],
) -> tuple[dict[str, Any] | list[Any] | None, str | None, int]:
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=60)
        data = json.loads(p.stdout) if p.stdout.strip() else None
        return data, (p.stderr.strip() or None), p.returncode
    except Exception as exc:  # noqa: BLE001 - collector must fail visibly in payload
        return None, repr(exc), 99


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def board_counts(board: str) -> dict[str, Any]:
    db = BOARDS / board / "kanban.db"
    out: dict[str, Any] = {"board": board, "exists": db.exists()}
    if not db.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT status, COUNT(*) c FROM tasks GROUP BY status"
        ).fetchall()
        out["status_counts"] = {r["status"]: r["c"] for r in rows}
        blocked = con.execute(
            "SELECT id,title,assignee,priority FROM tasks WHERE status='blocked' ORDER BY priority DESC, id LIMIT 12"
        ).fetchall()
        running = con.execute(
            "SELECT id,title,assignee,priority FROM tasks WHERE status='running' ORDER BY priority DESC, id LIMIT 12"
        ).fetchall()
        done7 = con.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='done' AND completed_at > CAST(strftime('%s','now') AS INTEGER)-604800"
        ).fetchone()[0]
        out["blocked"] = [dict(r) for r in blocked]
        out["running"] = [dict(r) for r in running]
        out["done_7d"] = done7
        con.close()
    except Exception as exc:  # noqa: BLE001
        out["error"] = repr(exc)
    return out


def placeholder_profiles(limit: int = 30) -> list[str]:
    names: list[str] = []
    for prof in sorted(
        p for p in PROFILES.iterdir() if p.is_dir() and not p.name.startswith(".")
    ):
        ref = prof / "REFLECTION.md"
        try:
            if not ref.exists():
                continue
            text = ref.read_text(errors="replace")
            if PLACEHOLDER not in text:
                continue
            # The sentinel line is historical: a profile is only a genuine
            # placeholder if NO evidence-backed entry follows it. Mirror the
            # matured-entry check in fleet_reflection_audit.py so we do not
            # re-route reflection probes at already-matured profiles
            # (reflection-placeholder-history false positive).
            after = text.split(PLACEHOLDER, 1)[1]
            matured_markers = ("Evidence-backed", "t_", "verified", "probe")
            if not any(kw in after for kw in matured_markers):
                names.append(prof.name)
        except Exception:
            continue
    # prioritize active/core names first, then stable sorted order
    names.sort(key=lambda n: (0 if n in CORE_PROFILE_HINTS else 1, n))
    return names[:limit]


def quality_sample(names: list[str]) -> list[dict[str, Any]]:
    results = []
    for name in names[:12]:
        data, err, code = run_json([str(SCORER), name])
        if isinstance(data, dict):
            data["cmd_exit"] = code
            results.append(data)
        else:
            results.append({"profile": name, "error": err, "cmd_exit": code})
    return results


def digest_payload(payload: dict[str, Any]) -> str:
    important = {
        "audit": {
            "placeholder_reflection_count": payload.get("audit", {}).get(
                "placeholder_reflection_count"
            ),
            "missing_reflection_count": payload.get("audit", {}).get(
                "missing_reflection_count"
            ),
            "stale_reflection_count": payload.get("audit", {}).get(
                "stale_reflection_count"
            ),
        },
        "boards": {
            b["board"]: {
                "status_counts": b.get("status_counts"),
                "blocked_ids": [x.get("id") for x in b.get("blocked", [])],
            }
            for b in payload.get("boards", [])
        },
        "placeholder_head": payload.get("placeholder_profiles", [])[:12],
    }
    raw = json.dumps(important, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()

    audit_data, audit_err, audit_exit = run_json([str(AUDIT)])
    if not isinstance(audit_data, dict):
        audit_data = {"error": audit_err, "exit": audit_exit}

    placeholders = placeholder_profiles()
    boards = [board_counts(b) for b in ACTIVE_BOARDS]
    sample_names = [
        n for n in CORE_PROFILE_HINTS if (PROFILES / n / "REFLECTION.md").exists()
    ]
    sample_names += [n for n in placeholders if n not in sample_names]

    payload: dict[str, Any] = {
        "generated_at": now,
        "host": os.uname().nodename,
        "audit": audit_data,
        "placeholder_profiles": placeholders,
        "quality_sample": quality_sample(sample_names),
        "boards": boards,
        "scripts": {
            "audit": str(AUDIT),
            "quality_scorer": str(SCORER),
        },
    }
    payload["digest"] = digest_payload(payload)

    prev = read_json(LATEST)
    if LATEST.exists():
        PREV.write_text(json.dumps(prev, indent=2, sort_keys=True) + "\n")

    last_wake = float(prev.get("last_agent_wake_epoch", 0) or 0)
    changed = payload["digest"] != prev.get("digest")
    due = (time.time() - last_wake) > DUE_SECONDS
    placeholder_count = int(audit_data.get("placeholder_reflection_count", 0) or 0)
    missing_count = int(audit_data.get("missing_reflection_count", 0) or 0)
    stale_count = int(audit_data.get("stale_reflection_count", 0) or 0)
    blocked_total = sum(len(b.get("blocked", [])) for b in boards)

    reasons = []
    if changed:
        reasons.append("digest_changed")
    if due and (placeholder_count > 0 or stale_count > 0 or missing_count > 0):
        reasons.append("reflection_backlog_due")
    if blocked_total > 0 and changed:
        reasons.append("blocked_state_changed")

    wake = bool(reasons)
    payload["changed"] = changed
    payload["due"] = due
    payload["wakeAgent"] = wake
    payload["wake_reasons"] = reasons
    payload["last_agent_wake_epoch"] = time.time() if wake else last_wake

    LATEST.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    with EVENTS.open("a") as f:
        f.write(
            json.dumps(
                {
                    "ts": now,
                    "digest": payload["digest"],
                    "wake": wake,
                    "reasons": reasons,
                }
            )
            + "\n"
        )

    final_line = json.dumps({"wakeAgent": wake, "context": payload}, sort_keys=True)
    if MODE == "no-agent":
        if wake:
            print(
                f"SELF_IMPROVEMENT_COLLECT changed={changed} due={due} placeholders={placeholder_count} missing={missing_count} stale={stale_count} blocked={blocked_total} reasons={','.join(reasons)} state={LATEST}"
            )
        return 0

    print(json.dumps(payload, indent=2, sort_keys=True))
    print(final_line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
