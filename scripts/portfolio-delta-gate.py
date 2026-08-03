#!/usr/bin/env python3
"""Delta gate for Elon portfolio delivery.

Reads the freshly-written PORTFOLIO-DECISION.md plus live board counts, computes a
stable digest for user-facing portfolio decisions, writes machine-readable
evidence, and returns JSON for the agent prompt to decide normal delivery vs
[SILENT].
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BOARDS = ["upero", "jarvis-os", "sycode-ai", "sycode-trading"]
PORTFOLIO_PATH = Path("/home/frank/uaa-rules/PORTFOLIO-DECISION.md")
STATE_PATH = Path(
    "/home/frank/jarvis/workspace/goals/jarvis-os/state/portfolio-decision-last-digest.json"
)
EVIDENCE_PATH = Path(
    "/home/frank/jarvis/workspace/goals/jarvis-os/state/portfolio-delta-evidence.jsonl"
)
BOARD_ROOT = Path("/home/frank/.hermes/kanban/boards")

TASK_RE = re.compile(r"\bt_[0-9a-f]{8,}\b")
PROPOSAL_RE = re.compile(r"\b([a-z0-9][a-z0-9-]*\.md)\b", re.I)


def stable_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def section(text: str, heading: str) -> str:
    # Match '## Heading' until the next H1/H2 section.
    pat = re.compile(
        rf"^##\s+{re.escape(heading)}\b.*?$(.*?)(?=^##\s+|^#\s+|\Z)", re.I | re.M | re.S
    )
    m = pat.search(text)
    return m.group(1) if m else ""


def priority_project(text: str) -> str:
    patterns = [
        r"priority(?:\s+call|\s+project)?\s*[:\-]\s*`?([A-Za-z0-9_-]+)`?",
        r"\bselected\s+priority\s*[:\-]\s*`?([A-Za-z0-9_-]+)`?",
        r"\bpush\s+this\s+week\s*[:\-]\s*`?([A-Za-z0-9_-]+)`?",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).lower()
    for line in text.splitlines():
        if "priority" in line.lower():
            for board in BOARDS:
                if board in line.lower():
                    return board
    return "unknown"


def reason_class(line: str) -> str:
    lower = line.lower()
    classes = [
        ("money_or_payment", ["money", "payment", "charge", "billing"]),
        ("live_trading", ["live trading", "trade_intent", "live mode", "live_capped"]),
        ("credential_or_secret", ["credential", "secret", "token", "oauth", "key"]),
        ("public_release", ["production", "deploy", "user-facing", "release"]),
        (
            "irreversible_data",
            ["irreversible", "drop", "mass delete", "destructive", "migration"],
        ),
        ("new_spend", ["new spend", "subscription", "api tier", "cost raise"]),
    ]
    for name, needles in classes:
        if any(n in lower for n in needles):
            return name
    return "other"


def needs_frank(text: str) -> list[dict[str, str]]:
    needs = section(text, "NEEDS FRANK") or "\n".join(
        line
        for line in text.splitlines()
        if "needs frank" in line.lower() or TASK_RE.search(line)
    )
    items: dict[str, str] = {}
    for line in needs.splitlines():
        ids = TASK_RE.findall(line)
        if not ids:
            continue
        cls = reason_class(line)
        for task_id in ids:
            items[task_id] = cls
    return [{"task_id": tid, "reason_class": items[tid]} for tid in sorted(items)]


def proposal_ids(text: str) -> list[str]:
    prop_sec = section(text, "Proposals routed") or section(text, "proposals") or text
    ids = {
        m.group(1).lower()
        for m in PROPOSAL_RE.finditer(prop_sec)
        if "proposal" in m.group(1).lower() or "productivity" in m.group(1).lower()
    }
    return sorted(ids)


def board_counts() -> dict[str, dict[str, int | str]]:
    out: dict[str, dict[str, int | str]] = {}
    for board in BOARDS:
        db = BOARD_ROOT / board / "kanban.db"
        if not db.exists():
            out[board] = {"error": "missing_db"}
            continue
        counts: dict[str, int | str] = {}
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
            try:
                for status in ("blocked", "ready", "running", "todo"):
                    counts[status] = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM tasks WHERE status=?", (status,)
                        ).fetchone()[0]
                    )
                # task_runs can include dead/stale rows; include task status above as canonical running count.
            finally:
                conn.close()
        except (
            Exception
        ) as exc:  # keep gate fail-visible but non-crashing for the agent
            counts = {"error": f"{type(exc).__name__}: {exc}"}
        out[board] = counts
    return out


def load_previous() -> dict[str, Any] | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception as exc:
        return {"digest": None, "error": f"unreadable previous state: {exc}"}


def main() -> int:
    text = PORTFOLIO_PATH.read_text() if PORTFOLIO_PATH.exists() else ""
    current_payload = {
        "selected_priority_project": priority_project(text),
        "needs_frank": needs_frank(text),
        "board_counts": board_counts(),
        "proposal_ids": proposal_ids(text),
    }
    current_digest = stable_hash(current_payload)
    previous = load_previous()
    previous_digest = previous.get("digest") if isinstance(previous, dict) else None
    previous_critical_ids = (
        set(previous.get("critical_task_ids", []))
        if isinstance(previous, dict)
        else set()
    )
    current_critical_ids = {item["task_id"] for item in current_payload["needs_frank"]}
    newly_critical = sorted(current_critical_ids - previous_critical_ids)

    changed = previous_digest != current_digest
    if previous is None:
        reason = "no previous digest"
    elif isinstance(previous, dict) and previous.get("error"):
        reason = previous["error"]
    elif changed:
        reason = "digest changed"
    else:
        reason = "digest unchanged"

    record = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "portfolio_path": str(PORTFOLIO_PATH),
        "artifact_written": PORTFOLIO_PATH.exists()
        and PORTFOLIO_PATH.stat().st_size > 0,
        "previous_digest": previous_digest,
        "current_digest": current_digest,
        "changed": changed,
        "reason": reason,
        "newly_critical_task_ids": newly_critical,
        "digest_payload": current_payload,
        "state_path": str(STATE_PATH),
        "evidence_path": str(EVIDENCE_PATH),
    }

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVIDENCE_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")

    if changed or newly_critical:
        STATE_PATH.write_text(
            json.dumps(
                {
                    "updated_at": record["checked_at"],
                    "digest": current_digest,
                    "critical_task_ids": sorted(current_critical_ids),
                    "digest_payload": current_payload,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
