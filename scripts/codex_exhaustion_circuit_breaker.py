#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""codex_exhaustion_circuit_breaker.py — graceful-degradation guard for total
provider exhaustion (deterministic, no-agent, A2 delegated class).

v1 converted total openai-codex exhaustion into a global pause of the
Jarvis-hosted fleet-dispatch-loop. v2a keeps that same breaker/wrapper
substrate but, during total Codex exhaustion, can write a sanitized Nous-health
allowlist that fleet-dispatch.sh uses to dispatch only boards whose dry-run
frontier contains allowlisted assignees. v2a is staged behind
CODEX_SELECTIVE_DISPATCH_ENABLED=1 so active cron keeps v1 behavior until the
implementation review explicitly approves activation.

READ-ONLY on auth material: parses only credential-pool metadata fields needed
for health classification. It never prints token values, credential labels,
provider account identifiers, raw provider/OAuth errors, access tokens, refresh
tokens, agent keys, or API keys. State/log output is restricted to profile names
and coarse reason classes.

Rollback: leave CODEX_SELECTIVE_DISPATCH_ENABLED unset/0 and resume the dispatcher
with `HERMES_HOME=/home/frank/.hermes/profiles/jarvis /home/frank/.local/bin/hermes cron resume
a9def8c365df`, restoring the prior global-pause behavior if needed.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

AUTH = os.environ.get("CODEX_BREAKER_AUTH", "/home/frank/.hermes/profiles/jarvis/auth.json")
ROOT_AUTH = os.environ.get("CODEX_BREAKER_ROOT_AUTH", "/home/frank/.hermes/auth.json")
PROFILES_DIR = os.environ.get("CODEX_BREAKER_PROFILES_DIR", "/home/frank/.hermes/profiles")
DISPATCH_JOB = os.environ.get("CODEX_BREAKER_DISPATCH_JOB", "a9def8c365df")
STATE = os.environ.get("CODEX_BREAKER_STATE", "/home/frank/.hermes/state/codex-circuit-breaker.json")
SELECTIVE_STATE = os.environ.get("CODEX_SELECTIVE_STATE", "/home/frank/.hermes/state/codex-selective-dispatch.json")
ALLOWLIST = os.environ.get("CODEX_SELECTIVE_ALLOWLIST", "/home/frank/.hermes/state/codex-selective-dispatch-allowlist.json")
LOG = os.environ.get("CODEX_BREAKER_LOG", "/home/frank/.hermes/logs/codex-circuit-breaker.log")
HERMES = os.environ.get("CODEX_BREAKER_HERMES", "/home/frank/.local/bin/hermes")
RECOVERY_SWEEP_SCRIPT = os.environ.get(
    "CODEX_RECOVERY_SWEEP_SCRIPT",
    "/home/frank/.hermes/profiles/jarvis/scripts/kanban_transient_recovery.py",
)
ALERTS_ENABLED = os.environ.get("CODEX_BREAKER_ALERTS", "1") not in {"0", "false", "False", "no", "NO"}
ALERT_TARGET = os.environ.get("CODEX_BREAKER_ALERT_TARGET", "discord:#critical-alerts")
ALLOWLIST_TTL_SECONDS = int(os.environ.get("CODEX_SELECTIVE_ALLOWLIST_TTL_SECONDS", "600"))
SOURCE_TASK = os.environ.get("CODEX_SELECTIVE_SOURCE_TASK", "t_f05ea0b9")
SELECTIVE_ENABLED = os.environ.get("CODEX_SELECTIVE_DISPATCH_ENABLED", "0") in {"1", "true", "True", "yes", "YES"}
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

os.environ["PATH"] = "/home/frank/.local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")
# HERMES_HOME is set by profile context (cron scheduler) or environment; do not
# hardcode it here, so /home/frank/.local/bin/hermes cron pause/resume finds the intended cron store.


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso_utc(ts: float | None = None) -> str:
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) if ts is not None else utc_now()
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            pass
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.datetime.fromisoformat(s).timestamp()
        except ValueError:
            return None
    return None


def log(msg: str) -> None:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}\n")


def load_json(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def provider_creds(auth: dict[str, Any], provider: str) -> list[dict[str, Any]]:
    creds = auth.get("credential_pool", {}).get(provider, [])
    if isinstance(creds, dict):
        creds = [creds]
    if not isinstance(creds, list):
        return []
    return [c for c in creds if isinstance(c, dict)]


def codex_available() -> tuple[bool, str]:
    """True if at least one openai-codex credential is usable (not exhausted,
    or its recorded reset time has passed). Reads status fields only."""
    auth = load_json(AUTH)
    creds = provider_creds(auth, "openai-codex")
    if not creds:
        return True, "no-codex-pool-visible (fail-open: do not pause on missing data)"
    now = time.time()
    detail: list[str] = []
    for idx, cred in enumerate(creds, 1):
        status = cred.get("last_status")
        reset = parse_time(cred.get("last_error_reset_at"))
        if status != "exhausted":
            return True, f"credential-{idx} status={coarse_status(status)}"
        if reset is not None and reset < now:
            return True, f"credential-{idx} exhausted but reset passed"
        reset_label = iso_utc(reset) if reset else "unknown"
        detail.append(f"credential-{idx}=exhausted(reset={reset_label})")
    return False, "; ".join(detail)


def coarse_status(status: Any) -> str:
    if status is None or status == "":
        return "unknown-ok"
    value = str(status).strip().lower()
    if value in {"ok", "exhausted", "dead", "rate_limited", "unauthorized", "error"}:
        return value
    return "other"


def read_state(path: str = STATE) -> dict[str, Any]:
    try:
        return load_json(path)
    except Exception:
        return {"tripped": False}


def codex_exhaustion_episode_id(why: str) -> str:
    """Return a stable identity for one provider-exhaustion episode."""
    resets = sorted(set(re.findall(r"reset=([^;)]+)", why)))
    if resets:
        return "reset=" + ",".join(resets)
    return "why=" + why[:200]


def atomic_write_json(path: str | Path, payload: dict[str, Any], mode: int = 0o644) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, p)


def write_state(payload: dict[str, Any]) -> None:
    atomic_write_json(STATE, payload, mode=0o644)


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def recovery_sweep() -> None:
    """Unblock provider-transient kanban blockers after Codex recovers.

    This intentionally runs only from the TRIPPED -> RECOVERED transition branch.
    It delegates filtering to the no-agent kanban_transient_recovery helper, whose
    provider-sweep mode only unblocks blocks carrying provider-transient signals.
    """
    script = Path(RECOVERY_SWEEP_SCRIPT)
    if not script.is_file():
        log(f"RECOVERY-SWEEP-SKIPPED missing_script={script}")
        return
    result = run([sys.executable, str(script), "--mode", "provider-sweep"])
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    detail = stdout if stdout else "no provider-transient blocks swept"
    log(f"RECOVERY-SWEEP rc={result.returncode} {detail[:500]}")
    if stderr:
        log(f"RECOVERY-SWEEP-STDERR {stderr[:500]}")
    if result.returncode != 0:
        alert(
            "DGX circuit breaker recovery sweep failed",
            f"Codex recovered but provider-transient kanban sweep failed rc={result.returncode}; "
            "manual inspection may be needed. See codex-circuit-breaker.log.",
        )


WA_FALLBACK = os.environ.get("CODEX_BREAKER_WA_FALLBACK", "whatsapp:Frank")

def alert(subject: str, body: str) -> None:
    if not ALERTS_ENABLED:
        log(f"ALERT-SUPPRESSED {subject}: {body[:160]}")
        return
    result = run([HERMES, "send", "-q", "-t", ALERT_TARGET, "-s", subject, body])
    if result.returncode == 0:
        log(f"ALERT-SENT target={ALERT_TARGET} subject={subject}")
        return
    log(
        f"ALERT-FAILED target={ALERT_TARGET} rc={result.returncode} "
        f"subject={subject} stdout={result.stdout.strip()[:160]} stderr={result.stderr.strip()[:160]}"
    )
    # WhatsApp fallback — cross-channel failover for critical alerts
    wa = run([HERMES, "send", "-q", "-t", WA_FALLBACK, "-s", f"🔁 FAILOVER: {subject}", body])
    if wa.returncode == 0:
        log(f"ALERT-FAILOVER-OK target={WA_FALLBACK} subject={subject}")
    else:
        log(f"ALERT-FAILOVER-FAILED target={WA_FALLBACK} rc={wa.returncode} subject={subject}")


def profile_names() -> list[str]:
    root = Path(PROFILES_DIR)
    if not root.exists():
        return []
    names: list[str] = []
    for child in root.iterdir():
        if child.is_dir() and not child.name.startswith(".") and PROFILE_NAME_RE.fullmatch(child.name):
            names.append(child.name)
    return sorted(names)


def profile_auth_path(profile: str) -> Path:
    return Path(PROFILES_DIR) / profile / "auth.json"


def load_profile_provider_creds(profile: str, provider: str) -> tuple[list[dict[str, Any]], str]:
    """Hermes-equivalent provider pool lookup without returning secret values.

    Profile-local provider entries are authoritative. The root/global pool is a
    read-only fallback only when the profile has zero entries for the provider.
    """
    local_path = profile_auth_path(profile)
    if local_path.exists():
        try:
            local_creds = provider_creds(load_json(local_path), provider)
        except Exception:
            return [], "profile-auth-parse-error"
        if local_creds:
            return local_creds, "profile"
    try:
        root_creds = provider_creds(load_json(ROOT_AUTH), provider)
    except Exception:
        return [], "root-auth-parse-error"
    if root_creds:
        return root_creds, "root-fallback"
    return [], "missing"


def nous_spawnable(profile: str, now: float | None = None) -> tuple[bool, str]:
    now = time.time() if now is None else now
    if not PROFILE_NAME_RE.fullmatch(profile):
        return False, "invalid-profile-name"
    creds, source = load_profile_provider_creds(profile, "nous")
    if source.endswith("parse-error"):
        return False, source
    if not creds:
        return False, "nous-missing"
    saw_ok_candidate = False
    reasons: list[str] = []
    for cred in creds:
        status = coarse_status(cred.get("last_status"))
        expires_at = parse_time(cred.get("expires_at"))
        agent_key_expires_at = parse_time(cred.get("agent_key_expires_at"))
        if expires_at is not None and expires_at <= now:
            reasons.append("token-expired")
            continue
        if agent_key_expires_at is not None and agent_key_expires_at <= now:
            reasons.append("agent-key-expired")
            continue
        if status in {"unknown-ok", "ok"}:
            saw_ok_candidate = True
            continue
        if status == "exhausted":
            reasons.append("nous-exhausted")
        elif status == "dead":
            reasons.append("nous-dead")
        else:
            reasons.append(f"nous-{status}")
    if saw_ok_candidate:
        return True, f"nous-ok-{source}"
    return False, sorted(set(reasons))[0] if reasons else "nous-unhealthy"


def build_allowlist(now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    allowed: list[str] = []
    excluded: dict[str, str] = {}
    for profile in profile_names():
        ok, reason = nous_spawnable(profile, now=now)
        if ok:
            allowed.append(profile)
        else:
            excluded[profile] = reason
    generated = iso_utc(now)
    expires = iso_utc(now + ALLOWLIST_TTL_SECONDS)
    return {
        "mode": "selective",
        "generated_at": generated,
        "reason": "all-openai-codex-exhausted",
        "provider": "nous",
        "profiles": allowed,
        "excluded_profiles": excluded,
        "expires_at": expires,
        "source_task": SOURCE_TASK,
    }


def mark_selective_inactive(reason: str) -> None:
    now = time.time()
    atomic_write_json(
        SELECTIVE_STATE,
        {
            "mode": "normal",
            "tripped": False,
            "selective_active": False,
            "updated_at": iso_utc(now),
            "reason": reason,
            "source_task": SOURCE_TASK,
        },
        mode=0o644,
    )
    atomic_write_json(
        ALLOWLIST,
        {
            "mode": "normal",
            "generated_at": iso_utc(now),
            "reason": reason,
            "provider": "nous",
            "profiles": [],
            "excluded_profiles": {},
            "expires_at": iso_utc(now),
            "source_task": SOURCE_TASK,
        },
        mode=0o644,
    )


def write_selective_active(allowlist: dict[str, Any], why: str) -> None:
    atomic_write_json(ALLOWLIST, allowlist, mode=0o644)
    atomic_write_json(
        SELECTIVE_STATE,
        {
            "mode": "selective",
            "tripped": True,
            "selective_active": True,
            "updated_at": allowlist["generated_at"],
            "allowlist_path": ALLOWLIST,
            "allowlist_expires_at": allowlist["expires_at"],
            "allowed_count": len(allowlist.get("profiles", [])),
            "excluded_count": len(allowlist.get("excluded_profiles", {})),
            "why": why,
            "source_task": SOURCE_TASK,
        },
        mode=0o644,
    )


def ensure_dispatch_resumed(reason: str) -> None:
    r = run([HERMES, "cron", "resume", DISPATCH_JOB])
    log(f"DISPATCH-RESUME-CHECK {reason}; resume rc={r.returncode} {r.stdout.strip()[:120]}")


def pause_dispatch(reason: str) -> None:
    r = run([HERMES, "cron", "pause", DISPATCH_JOB])
    log(f"TRIPPED ({reason}); pause rc={r.returncode} {r.stdout.strip()[:120]}")


def main() -> None:
    ok, why = codex_available()
    state = read_state()

    if ok and not state.get("tripped"):
        if SELECTIVE_ENABLED:
            mark_selective_inactive("codex-available")
        log(f"OK {why}")
        return

    if not ok:
        episode_id = codex_exhaustion_episode_id(why)
        if not SELECTIVE_ENABLED:
            first_trip = not state.get("tripped")
            if first_trip:
                pause_dispatch(why)
                if state.get("last_trip_episode") == episode_id:
                    log(f"ALERT-SUPPRESSED duplicate TRIP episode={episode_id}")
                else:
                    alert(
                        "DGX circuit breaker TRIPPED: all codex credentials exhausted",
                        f"Every openai-codex credential is exhausted ({why}). Paused fleet-dispatch-loop "
                        f"so new workers don't crash into a dead provider (running workers finish naturally). "
                        f"Selective dispatch v2a is staged but not active pending implementation review.",
                    )
            else:
                log(f"STILL-TRIPPED {why}; selective-v2a-disabled")
            write_state({"tripped": True, "mode": "global_pause", "at": time.time(), "why": why, "episode_id": episode_id, "last_trip_episode": episode_id})
            return
        allowlist = build_allowlist()
        write_selective_active(allowlist, why)
        allowed_count = len(allowlist.get("profiles", []))
        excluded_count = len(allowlist.get("excluded_profiles", {}))
        ensure_dispatch_resumed("selective-mode")
        first_trip = not state.get("tripped") or state.get("mode") != "selective"
        write_state({"tripped": True, "mode": "selective", "at": time.time(), "why": why, "episode_id": episode_id, "last_trip_episode": episode_id})
        marker = "SELECTIVE-TRIPPED" if first_trip else "STILL-SELECTIVE"
        log(f"{marker} ({why}); allowlist allowed={allowed_count} excluded={excluded_count} expires={allowlist['expires_at']}")
        if first_trip:
            if state.get("last_trip_episode") == episode_id:
                log(f"ALERT-SUPPRESSED duplicate SELECTIVE episode={episode_id}")
            else:
                alert(
                    "DGX circuit breaker SELECTIVE: all codex credentials exhausted",
                    f"Every openai-codex credential is exhausted ({why}). Kept fleet-dispatch-loop enabled, "
                    f"but restricted cron dispatch to boards whose dry-run frontier is fully allowed by sanitized Nous health. "
                    f"Allowed profiles: {allowed_count}; excluded profiles: {excluded_count}. Activation of broader behavior still requires review.",
                )
        return

    if ok and state.get("tripped"):
        ensure_dispatch_resumed("codex-recovered")
        mark_selective_inactive("codex-recovered")
        recovery_sweep()
        write_state({"tripped": False, "mode": "normal", "at": time.time(), "why": why, "last_trip_episode": state.get("last_trip_episode") or state.get("episode_id")})
        log(f"RECOVERED ({why}); selective allowlist inactive")
        log("ALERT-SUPPRESSED auto RECOVER")
        return


if __name__ == "__main__":
    main()
