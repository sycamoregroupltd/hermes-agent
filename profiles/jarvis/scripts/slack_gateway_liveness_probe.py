#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies.
"""slack_gateway_liveness_probe.py — Slack integration liveness probe.

Filed alongside the jarvis Slack rollout, 2026-09-03. Profile-parameterized so
the same canonical script can be cron-wired into any profile that has Slack
configured — set HERMES_PROFILE (defaults to 'jarvis', the only profile with
Slack live as of writing) or override individual paths directly.

Checks, in order:
  1. Token validity: auth.test against the Slack Web API with SLACK_BOT_TOKEN.
     Catches revoked/expired tokens independent of whether the gateway process
     is even up.
  2. Gateway process: hermes-gateway-<profile>.service is active in systemd.
  3. Socket Mode state from gateway.log: the most recent Slack connection-state
     line must be a "connected" event, not a dangling "Disconnected" with no
     later reconnect. A bare disconnect with nothing after it means the socket
     died silently and nobody would otherwise notice (this is exactly the gap
     that motivated this probe).

No-agent cron semantics: QUIET on full success (no stdout). Prints a report and
exits non-zero on any failure, so the cron only delivers when Slack is red.

Env overrides:
  HERMES_PROFILE            default jarvis — drives the three defaults below
  SLACK_BOT_TOKEN_ENV_FILE  default /home/frank/.hermes/profiles/<profile>/.env
  GATEWAY_LOG               default /home/frank/.hermes/profiles/<profile>/logs/gateway.log
  SYSTEMD_UNIT              default hermes-gateway-<profile>.service
  LOG_TAIL_LINES            default 4000 (how far back to scan for the last state line)
  STATE_FILE                default /home/frank/.hermes/state/slack_gateway_liveness_<profile>.json
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROFILE = os.environ.get("HERMES_PROFILE", "jarvis")
ENV_FILE = Path(
    os.environ.get(
        "SLACK_BOT_TOKEN_ENV_FILE", f"/home/frank/.hermes/profiles/{PROFILE}/.env"
    )
)
GATEWAY_LOG = Path(
    os.environ.get(
        "GATEWAY_LOG", f"/home/frank/.hermes/profiles/{PROFILE}/logs/gateway.log"
    )
)
SYSTEMD_UNIT = os.environ.get("SYSTEMD_UNIT", f"hermes-gateway-{PROFILE}.service")
LOG_TAIL_LINES = int(os.environ.get("LOG_TAIL_LINES", "4000"))
STATE_FILE = Path(
    os.environ.get(
        "STATE_FILE",
        f"/home/frank/.hermes/state/slack_gateway_liveness_{PROFILE}.json",
    )
)

CONNECTED_RE = re.compile(r"gateway\.run: ✓ slack connected")
DISCONNECTED_RE = re.compile(r"gateway\.run: ✓ slack disconnected")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_env_var(name: str) -> str | None:
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return None


# Matches the marker the op-slack-secrets-bootstrap.sh migration leaves behind, e.g.:
#   # SLACK_BOT_TOKEN resolved from op://Hermes/slack-bot-token (secrets.onepassword)
OP_REF_MARKER_RE = re.compile(
    r"^#\s*(\w+)\s+resolved from\s+(op://\S+?)\s*\(secrets\.onepassword\)"
)


def read_op_ref(name: str) -> str | None:
    """If NAME was migrated to 1Password, return its op:// item reference from the .env marker
    comment (vault/item only — the bootstrap script's marker never includes the field name)."""
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        m = OP_REF_MARKER_RE.match(line.strip())
        if m and m.group(1) == name:
            return m.group(2)
    return None


def resolve_token(name: str) -> tuple[str | None, str]:
    """Plaintext .env value if present, else resolve via `op read` from the migration marker."""
    plain = read_env_var(name)
    if plain:
        return plain, "plaintext .env"
    ref = read_op_ref(name)
    if not ref:
        return None, "not found (no plaintext value and no op:// migration marker)"
    field_ref = f"{ref.rstrip('/')}/credential"
    try:
        proc = subprocess.run(
            ["op", "read", field_ref],
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": read_env_var("OP_SERVICE_ACCOUNT_TOKEN") or ""},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"op read {field_ref} failed to run: {exc}"
    if proc.returncode != 0:
        return None, f"op read {field_ref} exit {proc.returncode}: {proc.stderr.strip()[:200]}"
    return proc.stdout.strip(), f"resolved via {field_ref}"


def check_token_auth() -> dict:
    token, source = resolve_token("SLACK_BOT_TOKEN")
    if not token:
        return {"ok": False, "detail": f"SLACK_BOT_TOKEN unavailable: {source}"}
    req = urllib.request.Request(
        "https://slack.com/api/auth.test",
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
        return {"ok": False, "detail": f"auth.test request failed: {exc}"}
    if not body.get("ok"):
        return {"ok": False, "detail": f"auth.test rejected token: {body.get('error', body)}"}
    team = body.get("team", "?")
    user = body.get("user", "?")
    return {"ok": True, "detail": f"authenticated as @{user} in workspace {team}"}


def check_systemd_unit() -> dict:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", SYSTEMD_UNIT],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": f"systemctl check failed: {exc}"}
    state = proc.stdout.strip()
    if state != "active":
        return {"ok": False, "detail": f"{SYSTEMD_UNIT} is '{state}', not 'active'"}
    return {"ok": True, "detail": f"{SYSTEMD_UNIT} active"}


def check_socket_state() -> dict:
    if not GATEWAY_LOG.exists():
        return {"ok": False, "detail": f"gateway log not found: {GATEWAY_LOG}"}
    try:
        # Bounded tail read — logs can be large; we only need the last N lines.
        proc = subprocess.run(
            ["tail", "-n", str(LOG_TAIL_LINES), str(GATEWAY_LOG)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        lines = proc.stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": f"could not read gateway log: {exc}"}

    last_connected_idx = -1
    last_disconnected_idx = -1
    last_connected_line = ""
    last_disconnected_line = ""
    for idx, line in enumerate(lines):
        if CONNECTED_RE.search(line):
            last_connected_idx = idx
            last_connected_line = line
        elif DISCONNECTED_RE.search(line):
            last_disconnected_idx = idx
            last_disconnected_line = line

    if last_connected_idx == -1 and last_disconnected_idx == -1:
        return {
            "ok": False,
            "detail": f"no Slack connect/disconnect events found in last {LOG_TAIL_LINES} log lines "
            "— Slack may never have connected on this run",
        }
    if last_disconnected_idx > last_connected_idx:
        return {
            "ok": False,
            "detail": f"most recent Slack socket event is a DISCONNECT with no later reconnect: "
            f"{last_disconnected_line.strip()}",
        }
    return {"ok": True, "detail": last_connected_line.strip()}


CHECKS = [
    ("token_auth", check_token_auth),
    ("systemd_unit", check_systemd_unit),
    ("socket_state", check_socket_state),
]


def main() -> int:
    results = {}
    for name, fn in CHECKS:
        try:
            results[name] = fn()
        except Exception as exc:  # never let the probe itself vanish silently
            results[name] = {"ok": False, "detail": f"probe error: {exc!r}"}

    failed = [n for n, r in results.items() if not r["ok"]]
    state = {
        "checked_at": now_iso(),
        "status": "red" if failed else "green",
        "failed": failed,
        "results": results,
    }
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as exc:
        print(f"WARN: could not write state file {STATE_FILE}: {exc}", file=sys.stderr)

    if not failed:
        return 0  # quiet on success

    print(f"SLACK GATEWAY RED — {PROFILE} Slack integration is degraded or dead")
    print(f"checked_at: {state['checked_at']}")
    for name, r in results.items():
        print(f"  [{'ok ' if r['ok'] else 'FAIL'}] {name}: {r['detail']}")
    print("")
    print(f"Recovery: restart the {PROFILE} gateway process manually from a shell "
          "(the messaging gateway management subcommand's restart action, run "
          "outside the running gateway itself), then re-check gateway status "
          "and tail logs/gateway.log for '[Slack]' lines. If token_auth failed, "
          "the bot token was revoked/rotated in Slack and needs to be "
          "regenerated from OAuth & Permissions and re-saved to .env.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
