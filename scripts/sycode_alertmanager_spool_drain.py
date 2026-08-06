#!/usr/bin/env python3
"""Drain Alertmanager OOB spool and print compact Discord-ready alerts.

No-agent cron delivers stdout directly to Discord. Empty stdout = silent.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("ALERT_SPOOL_ROOT", "/home/frank/.hermes/profiles/jarvis/state/alertmanager-spool"))
INCOMING = ROOT / "incoming"
ARCHIVE = ROOT / "archive"
DRAIN_LABEL = os.environ.get("ALERT_SPOOL_LABEL", "SYCODE ALERTMANAGER OOB RELAY")
DRAIN_TASK_TAG = os.environ.get("ALERT_SPOOL_TASK_TAG", "t_0e94fe00")

# --- Rate-limit-aware backoff config (kanban t_e656efdc) -----------------------
# This mechanism is LOCAL-ONLY (drains spooled Alertmanager JSON files to stdout
# for the no-agent cron delivery; it makes no provider/API call). The backoff
# envelope is therefore configured here for completeness/consistency with the
# other 4 critical mechanisms, but NO outbound call site exists to wrap, so the
# backoff is intentionally dormant. If a future change adds a provider send to
# this script, use rate_limit_backoff.run_subprocess_with_backoff exactly as the
# breaker/notifier/verdict-router now do. No routing/cred/schedule/spend change.
RLB_BASE = float(os.environ.get("RLB_BASE_SECONDS", "2.0"))
RLB_MAX = float(os.environ.get("RLB_MAX_SECONDS", "60.0"))
RLB_ATTEMPTS = int(os.environ.get("RLB_MAX_ATTEMPTS", "3"))


def labels_text(labels: dict[str, Any]) -> str:
    parts = []
    for key in ("alertname", "severity", "instance", "job", "container", "route"):
        if labels.get(key):
            parts.append(f"{key}={labels[key]}")
    return " ".join(parts) if parts else "labels=none"


def summarize_payload(payload: dict[str, Any]) -> list[str]:
    status = payload.get("status", "unknown")
    alerts = payload.get("alerts") or []
    lines = [f"Alertmanager OOB relay received {len(alerts)} alert(s), status={status}"]
    for alert in alerts[:12]:
        labels = alert.get("labels") or {}
        ann = alert.get("annotations") or {}
        starts = alert.get("startsAt") or alert.get("activeAt") or "unknown-start"
        summary = ann.get("summary") or ann.get("description") or "no summary"
        lines.append(f"- {labels_text(labels)} startsAt={starts}: {summary}")
    if len(alerts) > 12:
        lines.append(f"... {len(alerts) - 12} more alerts omitted")
    return lines


def _priority_key(path: Path) -> tuple[int, str]:
    # t_f32a3261: cronrelay-* files (DQSH self-healer alerts) must never be
    # starved by the alertmanager-* flood when the 20-slot window fills.
    return (0 if path.name.startswith("cronrelay-") else 1, path.name)


def main() -> int:
    if not INCOMING.exists():
        return 0
    files = sorted((p for p in INCOMING.glob("*.json") if p.is_file()), key=_priority_key)[:20]
    if not files:
        return 0
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    stuck: list[str] = []
    for path in files:
        # 2026-07-29 (opus5): CONSUME BEFORE EMITTING. This used to append the alert
        # summary and only then attempt the move, so when the move failed the same
        # payload was re-emitted on every run — forever. A root-owned /spool/incoming
        # (container mkdir, host drain) made every move raise PermissionError, and the
        # job re-posted the same 20 stale alerts to #critical-alerts every 60 seconds
        # from 07-28 15:38 until 07-29. An alert we cannot retire must NOT be re-sent.
        try:
            payload = json.loads(path.read_text())
        except Exception as exc:
            payload = None
            summary = f"Alertmanager OOB spool drain: unreadable {path.name}: {type(exc).__name__}: {str(exc)[:120]}"
        else:
            summary = "\n".join(summarize_payload(payload if isinstance(payload, dict) else {}))
        prefix = "" if payload is not None else "error-"
        try:
            shutil.move(str(path), str(ARCHIVE / f"{prefix}{int(time.time())}-{path.name}"))
        except Exception as exc:
            # Could not retire it — stay SILENT about its contents so we never loop.
            stuck.append(f"{path.name}: {type(exc).__name__}")
            continue
        blocks.append(summary)

    if stuck:
        # One throttled diagnostic, not one per file per minute.
        stamp = ROOT / ".drain-blocked-last-report"
        now = int(time.time())
        try:
            last = int(stamp.read_text().strip())
        except Exception:
            last = 0
        if now - last >= int(os.environ.get("DRAIN_BLOCKED_REALERT_SECONDS", "3600")):
            try:
                stamp.write_text(str(now))
            except Exception:
                pass
            print(
                "🚨 SYCODE ALERTMANAGER SPOOL DRAIN BLOCKED — "
                f"{len(stuck)} file(s) cannot be archived, so spooled alerts are NOT being delivered.\n"
                f"First: {stuck[0]}\n"
                f"Backlog in {INCOMING}: {len(list(INCOMING.glob('*.json')))} file(s).\n"
                "Usual cause: /spool/incoming recreated root-owned by the relay container; the host drain "
                "runs as frank and needs write permission on the DIRECTORY to unlink. Fix: "
                "docker exec sycode-alertmanager-oob-relay chmod 0777 /spool/incoming"
            )
        return 0 if blocks else 1

    if not blocks:
        return 0
    print(f"🔴 {DRAIN_LABEL} — spooled critical alert delivery ({DRAIN_TASK_TAG}):")
    print("\n\n".join(blocks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
