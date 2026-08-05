#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""channel_liveness_oob_probe.py — Daily out-of-band channel liveness probe.

No-agent cron semantics:
- Sends a labeled delivery receipt to each Frank-facing channel.
- Records per-channel last-success timestamps in a state file.
- On ANY channel failure, cross-alerts on the other working channels.
- Quiet on full success (no stdout) — Hermes cron only delivers output on failure.

Channels probed:
  discord:#critical-alerts, telegram:506972405, whatsapp:Frank
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("CHANNEL_LIVENESS_STATE_DIR", "/home/frank/.hermes/state"))
STATE_FILE = STATE_DIR / "channel_liveness_oob_state.json"
HERMES = os.environ.get("CHANNEL_LIVENESS_HERMES_BIN", "/home/frank/.local/bin/hermes")
HERMES_HOME = os.environ.get("CHANNEL_LIVENESS_HERMES_HOME", "/home/frank/.hermes")

# Frank-facing channels: (name, target, fallback_weight)
CHANNELS = [
    ("discord-critical-alerts", "discord:#critical-alerts", 0),
    ("telegram", "telegram:506972405", 1),
    ("whatsapp-frank", "whatsapp:Frank", 2),
]

CROSS_ALERT_TARGETS = os.environ.get(
    "CHANNEL_LIVENESS_CROSS_TARGETS",
    "discord:#critical-alerts,whatsapp:Frank",
).split(",")


def _target_channel_name(target: str) -> str | None:
    """Map a cross-alert target string back to a probed channel name, if any."""
    for name, t, _ in CHANNELS:
        if t == target:
            return name
    return None


def _target_works(target: str, results: dict) -> bool:
    """True if the cross-alert target is not a channel that failed this probe run.

    Dead channels must be excluded from their own cross-alert route, else the
    outage notice fans out THROUGH the dead channel and escalation silently halves.
    Targets that do not correspond to a probed channel are sent anyway.
    """
    name = _target_channel_name(target)
    if name is None:
        return True
    r = results.get(name)
    if r is None:
        return True
    return r["success"]



def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.rename(STATE_FILE)


def send_probe(target: str, receipt_id: str) -> tuple[bool, str]:
    """Send a labeled receipt to target. Returns (success, detail)."""
    subject = f"🔍 Channel liveness probe [{receipt_id}]"
    body = (
        f"Channel liveness receipt {receipt_id}\n"
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}\n"
        f"Probe path: discord->wa-failover, daily briefing, oob liveness\n"
        f"If you see this, the channel is alive and delivery works end-to-end."
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = HERMES_HOME
    try:
        result = subprocess.run(
            [HERMES, "send", "-q", "-t", target, "-s", subject, body],
            capture_output=True, text=True, timeout=60, env=env,
        )
        detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")[:200]
        success = result.returncode == 0
        return success, f"rc={result.returncode} {detail}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    now_epoch = int(time.time())
    now_iso = datetime.now(timezone.utc).isoformat()
    receipt_id = f"liveness-{now_epoch}"
    state = load_state()
    state.setdefault("channels", {})

    results = {}
    for name, target, _ in CHANNELS:
        ok, detail = send_probe(target, receipt_id)
        prev = state["channels"].get(name, {})
        results[name] = {
            "target": target,
            "success": ok,
            "detail": detail,
            "checked_at_epoch": now_epoch,
            "checked_at": now_iso,
            "last_success_epoch": prev.get("last_success_epoch") if not ok else now_epoch,
            "last_success": prev.get("last_success") if not ok else now_iso,
            "consecutive_failures": (prev.get("consecutive_failures", 0) + 1) if not ok else 0,
        }
        state["channels"][name] = results[name]

    state["last_run_epoch"] = now_epoch
    state["last_run"] = now_iso
    state["receipt_id"] = receipt_id
    save_state(state)

    failed = {n: r for n, r in results.items() if not r["success"]}
    if not failed:
        # All channels healthy — silent exit (no-agent cron delivers nothing)
        return 0

    # One or more channels failed — cross-alert on working channels
    working = [n for n, r in results.items() if r["success"]]
    # Build cross-alert message
    lines = [
        f"🚨 CHANNEL LIVENESS FAILURE [{receipt_id}]",
        f"Timestamp: {now_iso}",
        "",
        "Failed channels:",
    ]
    for name, r in failed.items():
        cf = r.get("consecutive_failures", 1)
        ls = r.get("last_success", "never")
        lines.append(f"  ❌ {name} ({r['target']}) — {r['detail']} (consecutive={cf}, last_success={ls})")
    lines.append("")
    lines.append("Working channels (probe result):")
    for name in working:
        lines.append(f"  ✅ {name}")
    lines.append("")
    # Only targets that are NOT themselves failing get the cross-alert.
    delivered_targets = []
    skipped_targets = []
    for target in CROSS_ALERT_TARGETS:
        target = target.strip()
        if not target:
            continue
        if _target_works(target, results):
            delivered_targets.append(target)
        else:
            skipped_targets.append(target)
    lines.append("Cross-alert delivered to: " + (", ".join(delivered_targets) if delivered_targets else "(none)"))
    if skipped_targets:
        lines.append("Skipped (failed this run): " + ", ".join(skipped_targets))
    body = "\n".join(lines)

    # Send cross-alert to all working cross-alert targets
    env = os.environ.copy()
    env["HERMES_HOME"] = HERMES_HOME
    for target in delivered_targets:
        try:
            subprocess.run(
                [HERMES, "send", "-q", "-t", target, "-s", f"🚨 Channel liveness failure [{receipt_id}]", body],
                capture_output=True, text=True, timeout=60, env=env,
            )
        except Exception:
            pass
    if not delivered_targets:
        print("⚠️  WARNING: all cross-alert targets failed this run — no working channel remains to escalate through.", file=sys.stderr)

    # Print to stdout so the cron's deliver target also gets the failure report
    print(body)
    return 1


if __name__ == "__main__":
    sys.exit(main())
