#!/usr/bin/env python3
"""MECH-RED standing guard — detects mechanism key RED/DEAD transitions and
auto-restarts or escalates before the next governor probe cycle.

Consumes the output of jarvis_mechanism_liveness_collect.py (every 10m via
dgx-unified-health-probe) and acts on mechanism keys that are DEAD within 60s.

Idempotency: elon-proposal-2026-08-06-mech-red-guard
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERMES = Path("/home/frank/.local/bin/hermes")
MECH_COLLECT = Path("/home/frank/.hermes/scripts/jarvis_mechanism_liveness_collect.py")
STATE_DIR = Path("/home/frank/.hermes/cron/state")
MECH_RED_STATE = STATE_DIR / "mech_red_guard.json"
ESCALATION_STATE = STATE_DIR / "mech_red_escalations.json"

# Mechanism keys this guard monitors (from jarvis-os/t_498c8bcc, t_c10db950)
MECHANISM_KEYS = {
    "auto-review-router",
    "oob-canary",
    "escalation-notifier-service-gate",
    "escalation-notifier-critical",
    "breaker",
    "verdict-router",
    "wake-scanner",
}

MAX_AUTO_RESTART = 3
ESCALATION_COOLDOWN_H = 1  # don't re-escalate within 1h for same key


def load_state() -> dict:
    if MECH_RED_STATE.exists():
        try:
            return json.loads(MECH_RED_STATE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MECH_RED_STATE.write_text(json.dumps(state, indent=2))


def load_escalations() -> dict:
    if ESCALATION_STATE.exists():
        try:
            return json.loads(ESCALATION_STATE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_escalations(escalations: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ESCALATION_STATE.write_text(json.dumps(escalations, indent=2))


def run_mechanism_collector() -> dict | None:
    """Run jarvis_mechanism_liveness_collect.py and return parsed JSON."""
    try:
        r = subprocess.run(
            [sys.executable, str(MECH_COLLECT)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def get_dead_keys(rep: dict) -> list[dict]:
    """Extract DEAD rows for monitored mechanism keys."""
    dead = []
    for row in rep.get("rows", []):
        key = row.get("key", "")
        if key in MECHANISM_KEYS and row.get("status") == "DEAD":
            dead.append(row)
    return dead


def resume_cron_job(job_id: str) -> bool:
    """Resume a paused/crashed cron job via hermes CLI. Returns True on success."""
    try:
        r = subprocess.run(
            [str(HERMES), "cron", "resume", job_id],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def escalate(key: str, row: dict, fail_count: int) -> None:
    """Record escalation to os-reviewer for a mechanism key."""
    escalations = load_escalations()
    now = datetime.now(timezone.utc).isoformat()
    escalations[key] = {
        "last_escalated_at": now,
        "fail_count": fail_count,
        "job_id": row.get("job_id"),
        "job_name": row.get("job_name"),
        "script": row.get("script"),
        "profile": row.get("profile"),
        "reason": row.get("reason"),
        "last_error": row.get("last_error"),
        "last_run_at": row.get("last_run_at"),
        "repair_idempotency_key": row.get("repair_idempotency_key"),
        "suggested_repair_title": row.get("suggested_repair_title"),
    }
    save_escalations(escalations)


def should_escalate(key: str, fail_count: int) -> bool:
    """Check if we should escalate (3+ failures and not recently escalated)."""
    if fail_count < MAX_AUTO_RESTART:
        return False
    escalations = load_escalations()
    prev = escalations.get(key)
    if prev is None:
        return True
    try:
        last = datetime.fromisoformat(prev["last_escalated_at"])
        age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        if age_h < ESCALATION_COOLDOWN_H:
            return False
    except (ValueError, TypeError, KeyError):
        return True
    return True


def main() -> None:
    rep = run_mechanism_collector()
    if rep is None:
        print("MECH_RED_GUARD: collector unavailable")
        sys.exit(1)

    dead_keys = get_dead_keys(rep)
    if not dead_keys:
        print("MECH_RED_GUARD: all monitored mechanisms GREEN")
        sys.exit(0)

    state = load_state()
    now = datetime.now(timezone.utc).isoformat()
    alerts = []
    restarts_attempted = []
    restarts_succeeded = []
    escalations_fired = []

    for row in dead_keys:
        key = row["key"]
        job_id = row.get("job_id", "")
        prev = state.get(key, {"fail_count": 0, "last_seen_dead_at": None, "last_restart_at": None})

        # Increment fail count
        fail_count = prev.get("fail_count", 0) + 1
        prev["fail_count"] = fail_count
        prev["last_seen_dead_at"] = now

        # Attempt auto-restart (read-only: resume the cron job)
        restart_ok = False
        if job_id:
            restart_ok = resume_cron_job(job_id)
            prev["last_restart_at"] = now
            prev["last_restart_ok"] = restart_ok

        if restart_ok:
            restarts_succeeded.append(key)
            # Reset fail count on successful restart
            prev["fail_count"] = 0
        else:
            restarts_attempted.append(key)

        # Escalate after 3 consecutive failures
        if should_escalate(key, fail_count):
            escalate(key, row, fail_count)
            escalations_fired.append(key)

        state[key] = prev
        alerts.append({
            "key": key,
            "job_id": job_id,
            "job_name": row.get("job_name"),
            "reason": row.get("reason"),
            "fail_count": fail_count,
            "restart_attempted": bool(job_id),
            "restart_ok": restart_ok,
            "escalated": key in escalations_fired,
        })

    save_state(state)

    # Output — one line per alert, suitable for cron delivery
    if alerts:
        dead_list = ",".join(a["key"] for a in alerts)
        restart_ok_list = ",".join(restarts_succeeded) if restarts_succeeded else "none"
        restart_fail_list = ",".join(restarts_attempted) if restarts_attempted else "none"
        escalate_list = ",".join(escalations_fired) if escalations_fired else "none"

        print(f"MECH_RED_GUARD: {len(alerts)} DEAD mechanism(s): {dead_list}")
        print(f"  restarts_ok={restart_ok_list} restarts_failed={restart_fail_list} escalations={escalate_list}")
        for a in alerts:
            status = "RESTARTED_OK" if a["restart_ok"] else ("ESCALATED" if a["escalated"] else "RESTART_FAILED")
            print(f"  {a['key']}: {status} fail_count={a['fail_count']} reason={a['reason'][:80]}")

        # Exit 1 so the cron delivery path fires the alert
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
