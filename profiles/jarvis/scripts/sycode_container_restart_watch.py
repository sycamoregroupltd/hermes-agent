#!/usr/bin/env python3
"""Sycode container restart watcher for t_0e94fe00.

No-agent watchdog semantics:
- Inspects Docker containers whose names match SYCODE_RESTART_WATCH_PREFIX.
- Emits stdout only when StartedAt or RestartCount changes after the baseline.
- Stores a local state file so unchanged containers stay silent.
- Includes attribution fields (status, restart count, exit code, oom flag, finishedAt, image).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

PREFIX = os.environ.get("SYCODE_RESTART_WATCH_PREFIX", "sycodetrading")
STATE_DIR = Path(os.environ.get("SYCODE_RESTART_WATCH_STATE_DIR", "/home/frank/.hermes/profiles/jarvis/state"))
STATE_PATH = STATE_DIR / "sycode_container_restart_watch_state.json"


def docker_json(args: list[str]) -> Any:
    out = subprocess.check_output(args, text=True, timeout=30)
    return json.loads(out)


def container_names() -> list[str]:
    out = subprocess.check_output(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        text=True,
        timeout=20,
    )
    return sorted(name for name in out.splitlines() if name.startswith(PREFIX))


def inspect_container(name: str) -> dict:
    data = docker_json(["docker", "inspect", name])[0]
    state = data.get("State") or {}
    config = data.get("Config") or {}
    return {
        "name": name,
        "id": (data.get("Id") or "")[:12],
        "image": config.get("Image") or data.get("Image") or "unknown",
        "status": state.get("Status"),
        "running": state.get("Running"),
        "restart_count": data.get("RestartCount", 0),
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
        "exit_code": state.get("ExitCode"),
        "oom_killed": state.get("OOMKilled"),
        "error": state.get("Error") or "",
    }


def load_state() -> dict[str, dict]:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state: dict[str, dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n")


def changed_reason(old: dict | None, new: dict) -> str | None:
    if old is None:
        return None
    parts = []
    if old.get("started_at") != new.get("started_at"):
        parts.append(f"StartedAt {old.get('started_at')} -> {new.get('started_at')}")
    if old.get("restart_count") != new.get("restart_count"):
        parts.append(f"RestartCount {old.get('restart_count')} -> {new.get('restart_count')}")
    return "; ".join(parts) if parts else None


def main() -> int:
    try:
        snapshots = {name: inspect_container(name) for name in container_names()}
    except Exception as exc:
        print(f"🔴 SYCODE CONTAINER RESTART WATCH ERROR — docker inspect failed: {type(exc).__name__}: {str(exc)[:160]}")
        return 0

    old = load_state()
    lines = []
    for name, snap in snapshots.items():
        reason = changed_reason(old.get(name), snap)
        if reason:
            lines.append(
                "- {name}: {reason}; status={status}; exit={exit_code}; oom={oom_killed}; "
                "finishedAt={finished_at}; image={image}; id={id}".format(reason=reason, **snap)
            )
    save_state(snapshots)

    if lines:
        print(f"🔴 SYCODE CONTAINER RESTART WATCH — {len(lines)} container start/restart change(s) detected (t_0e94fe00):")
        print("\n".join(lines[:30]))
        if len(lines) > 30:
            print(f"... {len(lines) - 30} more restart changes omitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
