#!/usr/bin/env python3
"""Interim Alertmanager firing-alert bridge for t_0e94fe00.

No-agent watchdog semantics:
- Polls Prometheus /api/v1/alerts.
- Emits stdout only for new/changed firing alerts; Hermes cron delivers stdout to Discord.
- Keeps state locally so unchanged firing alerts stay silent.
- Self-retires silently once kanban task t_577cd1cb is done (Grafana webhook fix landed).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

PROM_ALERTS_URL = os.environ.get("SYCODE_ALERT_BRIDGE_PROM_ALERTS_URL", "http://localhost:9090/api/v1/alerts")
STATE_DIR = Path(os.environ.get("SYCODE_ALERT_BRIDGE_STATE_DIR", "/home/frank/.hermes/profiles/jarvis/state"))
STATE_PATH = STATE_DIR / "sycode_alertmanager_firing_bridge_state.json"
RETIRE_TASK_ID = os.environ.get("SYCODE_ALERT_BRIDGE_RETIRE_TASK", "t_577cd1cb")
KANBAN_DB = Path(os.environ.get("SYCODE_ALERT_BRIDGE_KANBAN_DB", "/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db"))


def task_done(task_id: str) -> bool:
    if not task_id or not KANBAN_DB.exists():
        return False
    try:
        with sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=5) as db:
            row = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        return bool(row and row[0] == "done")
    except Exception:
        return False


def fetch_alerts() -> list[dict]:
    if os.environ.get("SYCODE_ALERT_BRIDGE_TEST") == "1":
        return [
            {
                "labels": {"alertname": "HermesBridgeTest", "severity": "critical", "job": "test"},
                "annotations": {"summary": "synthetic alert bridge test for t_0e94fe00"},
                "state": "firing",
                "activeAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        ]
    with urlopen(PROM_ALERTS_URL, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") not in {"success", "ok"}:
        raise RuntimeError(f"Prometheus alerts endpoint status={payload.get('status')!r}")
    return payload.get("data", {}).get("alerts", []) or []


def load_state() -> dict[str, str]:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n")


def alert_key(alert: dict) -> str:
    labels = alert.get("labels") or {}
    return "|".join(f"{k}={labels[k]}" for k in sorted(labels))


def alert_fingerprint(alert: dict) -> str:
    keep = {
        "labels": alert.get("labels") or {},
        "annotations": alert.get("annotations") or {},
        "state": alert.get("state"),
    }
    raw = json.dumps(keep, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def summarize(alert: dict) -> str:
    labels = alert.get("labels") or {}
    ann = alert.get("annotations") or {}
    name = labels.get("alertname", "unknown")
    severity = labels.get("severity", "unknown")
    instance = labels.get("instance") or labels.get("job") or "n/a"
    summary = ann.get("summary") or ann.get("description") or "no annotation summary"
    active = alert.get("activeAt") or "unknown-activeAt"
    return f"- {name} severity={severity} instance={instance} activeAt={active}: {summary}"


def main() -> int:
    if task_done(RETIRE_TASK_ID):
        save_state({"retired_after": RETIRE_TASK_ID})
        return 0
    try:
        alerts = [a for a in fetch_alerts() if a.get("state") == "firing"]
    except (URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"🔴 SYCODE ALERT BRIDGE ERROR — failed to poll Prometheus alerts: {type(exc).__name__}: {str(exc)[:160]}")
        return 0

    old = load_state()
    new = {}
    changed = []
    for alert in alerts:
        key = alert_key(alert)
        fp = alert_fingerprint(alert)
        new[key] = fp
        if old.get(key) != fp:
            changed.append(alert)
    save_state(new)

    if changed:
        print(f"🔴 SYCODE ALERTMANAGER BRIDGE — {len(changed)} new/changed firing alert(s) (interim OOB path, t_0e94fe00):")
        for alert in changed[:20]:
            print(summarize(alert))
        if len(changed) > 20:
            print(f"... {len(changed) - 20} more changed alerts omitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
