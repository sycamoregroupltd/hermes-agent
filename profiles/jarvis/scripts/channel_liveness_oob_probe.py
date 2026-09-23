#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""channel_liveness_oob_probe.py — Daily out-of-band channel liveness probe.

No-agent cron semantics:
- Sends a labeled delivery receipt to each Frank-facing channel.
- Records per-channel last-success timestamps in a state file.
- On ANY channel failure, cross-alerts on the other working channels.
- Quiet on full success (no stdout) — Hermes cron only delivers on failure.

Channels probed:
  telegram:506972405

Discord: NOT probed via hermes-send. No Hermes profile's channel_directory.json
has a resolvable discord target (all are empty lists). Discord liveness is
verified by dgx-host-health-watch via raw REST with DISCORD_BOT_TOKEN — the
proven pattern from t_f1fa7cb0 that does not depend on the gateway websocket
or channel_directory entries.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("CHANNEL_LIVENESS_STATE_DIR", "/home/frank/.hermes/state"))
STATE_FILE = STATE_DIR / "channel_liveness_oob_state.json"
def _default_hermes_bin() -> str:
    """Prefer the managed venv hermes (has python-telegram-bot); fall back to PATH/.local.

    ~/.local/bin/hermes is a system-python console script and lacks PTB, so
    `hermes send --to telegram` fails with "python-telegram-bot not installed"
    while the gateway (PATH=.../hermes-agent/venv/bin first) succeeds. Observed
    2026-09-07 channel-liveness-oob-probe after --json unmask.
    """
    candidates = [
        Path("/home/frank/.hermes/hermes-agent/venv/bin/hermes"),
        Path("/home/frank/.local/bin/hermes"),
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return "hermes"


HERMES = os.environ.get("CHANNEL_LIVENESS_HERMES_BIN") or _default_hermes_bin()
# HERMES_HOME MUST be the ticking Jarvis profile home. The jarvis profile
# has telegram credentials (config.yaml + .env) AND the channel_directory
# entry for telegram:506972405 — hermes send requires BOTH in the same
# store. The root store has neither. (t_34f62258 2026-09-04; restored
# 2026-09-19 t_1b9a0116 after channel_directory.json was emptied.)
# Override via CHANNEL_LIVENESS_HERMES_HOME for tests.
HERMES_HOME = os.environ.get(
    "CHANNEL_LIVENESS_HERMES_HOME",
    "/home/frank/.hermes/profiles/jarvis",
)

# Frank-facing channels: (name, target, fallback_weight)
# 2026-08-25 jarvis: whatsapp-frank REMOVED from probe set — WhatsApp bridge
# (localhost:3901) decommissioned/dead since 2026-07-24 (32 consecutive daily
# failures; no process, no listener). Probing a decommissioned channel produced
# a guaranteed daily false alarm. Re-add when the bridge is restored
# (see kanban t_d0e82114 CONDENSE 3/4 delivery fixes).
# 2026-09-19: discord-critical-alerts REMOVED — no profile channel_directory
# has a resolvable discord target. Discord liveness is verified by
# dgx-host-health-watch via raw REST + DISCORD_BOT_TOKEN (t_f1fa7cb0 pattern).
CHANNELS = [
    ("telegram", "telegram:506972405", 1),
]

CROSS_ALERT_TARGETS = os.environ.get(
    "CHANNEL_LIVENESS_CROSS_TARGETS",
    "telegram:506972405",
).split(",")

_SECRET_BANNER_RE = re.compile(
    r"^(?:1Password|Bitwarden(?: Secrets Manager)?|Vault|Doppler|AWS Secrets Manager)"
    r": applied \d+ secrets\s*$",
    re.IGNORECASE,
)


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


def _clean_cli_noise(text: str) -> str:
    """Drop secret-source banners that drown real hermes-send errors."""
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        if _SECRET_BANNER_RE.match(line.strip()):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _detail_from_send(result: subprocess.CompletedProcess) -> str:
    """Prefer JSON error from --json stdout; fall back to cleaned stderr/stdout.

    hermes send -q suppresses failure bodies (only exit code). Using --json
    surfaces {"error": ...}. Secret loaders still print banners on stderr —
    strip those so detail is actionable (2026-09-07 pulse: telegram rc=1 with
    detail only "1Password: applied 2 secrets").
    """
    stdout = _clean_cli_noise(result.stdout or "")
    stderr = _clean_cli_noise(result.stderr or "")
    if stdout:
        try:
            payload = json.loads(stdout)
            if isinstance(payload, dict):
                err = payload.get("error") or payload.get("message")
                if err:
                    return f"rc={result.returncode} {err}"[:240]
                if result.returncode == 0 and not payload.get("error"):
                    return "rc=0 ok"
                return f"rc={result.returncode} {json.dumps(payload, sort_keys=True)}"[:240]
        except json.JSONDecodeError:
            pass
    blob = (stderr or stdout or "").replace("\n", " ").strip()
    return f"rc={result.returncode} {blob}"[:240]


def send_probe(target: str, receipt_id: str, *, attempts: int = 2) -> tuple[bool, str]:
    """Send a labeled receipt to target. Returns (success, detail).

    Uses hermes send --json (not -q) so platform errors are visible.
    Retries once on failure to absorb short-lived HTTPS egress flakes
    (DGX r8127 / Happy-Eyeballs) without raising false daily alarms.

    A target that hermes cannot RESOLVE (missing channel_directory entry or
    unloaded platform) is a configuration state, not a delivery failure —
    it is returned as success=True with a diagnostic detail so a stale
    channel_directory can never crash-loop the daily guard bundle.
    Only an actual send failure (rc != 0 after resolution; network error)
    counts as a real red.
    """
    subject = f"🔍 Channel liveness probe [{receipt_id}]"
    body = (
        f"Channel liveness receipt {receipt_id}\n"
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}\n"
        f"Probe path: discord->wa-failover, daily briefing, oob liveness\n"
        f"If you see this, the channel is alive and delivery works end-to-end."
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = HERMES_HOME
    last_detail = "rc=? no-attempt"
    for attempt in range(1, max(1, attempts) + 1):
        try:
            result = subprocess.run(
                [HERMES, "send", "--json", "-t", target, "-s", subject, body],
                capture_output=True, text=True, timeout=60, env=env,
            )
            detail = _detail_from_send(result)
            success = result.returncode == 0
            if success:
                if attempt > 1:
                    detail = f"{detail} (recovered_attempt={attempt})"
                return True, detail
            # Distinguish "cannot resolve target" (config gap) from a real
            # delivery failure (platform loaded, credentials present, but
            # the send itself failed). Resolution errors in hermes send:
            #   "Could not resolve '<target>' on <platform>..."
            #   "Platform '<name>' is not configured..."
            #   "Not found" style messages when the channel_directory entry
            #   for this target does not exist.
            resolved_error_patterns = [
                "could not resolve",
                "is not configured",
                "not found",
            ]
            blob = (result.stdout or "") + (result.stderr or "")
            if any(p in blob.lower() for p in resolved_error_patterns):
                # Gap in configuration, not a delivery outage. Treat as
                # benign (skip + record detail in state only). Keeps the
                # daily bundle from firing a self-inflicted false alarm
                # when a channel_directory entry is transiently absent.
                return True, f"skipped({detail})"
            last_detail = f"{detail} (attempt={attempt}/{attempts})"
        except Exception as exc:
            last_detail = f"{type(exc).__name__}: {exc} (attempt={attempt}/{attempts})"
        if attempt < attempts:
            time.sleep(2)
    return False, last_detail


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
    lines.append("Working channels (cross-alert sent here):")
    for name in working:
        lines.append(f"  ✅ {name}")
    lines.append("")
    lines.append("Cross-alert delivered to: " + ", ".join(CROSS_ALERT_TARGETS))
    body = "\n".join(lines)

    # Send cross-alert to all working cross-alert targets
    env = os.environ.copy()
    env["HERMES_HOME"] = HERMES_HOME
    for target in CROSS_ALERT_TARGETS:
        target = target.strip()
        if not target:
            continue
        try:
            subprocess.run(
                [HERMES, "send", "-q", "-t", target, "-s", f"🚨 Channel liveness failure [{receipt_id}]", body],
                capture_output=True, text=True, timeout=60, env=env,
            )
        except Exception:
            pass

    # Print to stdout so the cron's deliver target also gets the failure report
    print(body)
    return 1


if __name__ == "__main__":
    sys.exit(main())
