#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# Invoker: jarvis-os-pm profile cron job `jarvis-os-pm-nous-expiry-preflight` (no_agent, every 10m),
# and manual runbook: jarvis-os/t_d2a24224.
"""Per-profile Nous token-expiry preflight for jarvis-os-pm (t_d2a24224).

Read-only credential-metadata preflight. Classifies jarvis-os-pm's Nous
access-token state as GREEN / WARN / RED and exits accordingly, following the
exit-code liveness doctrine (non-zero exit = alert, and the alert is explicitly
classified PROVIDER/CREDENTIAL so downstream never misreads it as a lifecycle
or protocol violation).

Difference from the old `nous-token-presence` cron (nous_token_presence.sh):
  - that cron checked credential *presence* in the *jarvis* profile (wrong lane)
    and only counted entries (len>0), never expiry; it is currently erroring
    ("jarvis nous creds: 0") and delivers only to `local`.
  - this preflight targets jarvis-os-pm (the PM lane pinned to provider:nous),
    reads the *expiry timestamp* (credential_pool.nous[0].expires_at plus the
    shared nous_auth.json cross-check), and fails fast as provider/credential.

No secrets are ever printed (access_token / refresh_token are never emitted).
Reads expiry metadata only. No writes to auth/config, no provider/model/fallback
routing changes, no spend, no restart.

Exit codes:
  0  GREEN  healthy (credential present, expires_at parseable, TTL > WARN threshold)
  2  WARN   present but TTL <= WARN threshold (near expiry) — headsup alert
  1  RED    expired, missing, or unparseable — provider/credential alert

Thresholds (override via env):
  JARVIS_OS_PM_WARN_TTL_SECONDS  default 600 (10 min)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROFILE = os.environ.get("JARVIS_OS_PM_PROFILE", "jarvis-os-pm")
AUTH_JSON = Path(
    os.environ.get(
        "JARVIS_OS_PM_AUTH_JSON",
        f"/home/frank/.hermes/profiles/{PROFILE}/auth.json",
    )
)
SHARED_NOUS = Path(
    os.environ.get("JARVIS_OS_PM_SHARED_NOUS", "/home/frank/.hermes/shared/nous_auth.json")
)
WARN_TTL_SECONDS = int(os.environ.get("JARVIS_OS_PM_WARN_TTL_SECONDS", "600"))


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def main() -> int:
    verbose = "--verbose" in sys.argv

    now = datetime.now(timezone.utc)
    problems = []          # list of human-readable RED reasons
    warnings = []          # list of human-readable WARN reasons
    detail = {}            # sanitized metadata for verbose/evidence output

    # ---- static NOUS_API_KEY short-circuit (2026-08-29, JARVIS) ----
    # Since the 08-28 OAuth revocation incident the fleet runs on a static
    # NOUS_API_KEY in each profile .env; the OAuth token store is deliberately
    # dormant. If the profile has a static key, the OAuth expires_at is NOT the
    # live credential and its expiry is a false alarm (proven: profile completed
    # kanban runs + live LLM call while this preflight screamed RED).
    env_file = AUTH_JSON.parent / ".env"
    static_key_present = bool(os.environ.get("NOUS_API_KEY"))
    if not static_key_present:
        try:
            for line in env_file.read_text().splitlines():
                if line.strip().startswith("NOUS_API_KEY=") and len(line.split("=", 1)[1].strip()) > 8:
                    static_key_present = True
                    break
        except Exception:
            pass
    detail["static_nous_api_key"] = static_key_present
    if static_key_present:
        print("[GREEN] jarvis-os-pm nous preflight: static NOUS_API_KEY present "
              "(OAuth store dormant by design; expiry not checked)")
        return 0

    # ---- per-profile credential_pool.nous (authoritative for dispatch) ----
    try:
        auth = json.loads(AUTH_JSON.read_text())
    except FileNotFoundError:
        problems.append(f"auth.json missing: {AUTH_JSON}")
        auth = {}
    except Exception as exc:
        problems.append(f"auth.json unreadable ({type(exc).__name__})")
        auth = {}

    detail["auth_path"] = str(AUTH_JSON)
    detail["active_provider"] = auth.get("active_provider")
    cp = auth.get("credential_pool") or {}
    nous = cp.get("nous") or []
    if not isinstance(nous, list):
        nous = list(nous) if nous else []
    if not nous:
        problems.append("credential_pool.nous empty/missing on jarvis-os-pm")
    else:
        entry = nous[0] if isinstance(nous[0], dict) else {}
        detail["nous_count"] = len(nous)
        detail["last_status"] = entry.get("last_status")
        detail["last_error_code"] = entry.get("last_error_code")
        exp = parse_dt(entry.get("expires_at"))
        detail["expires_at"] = entry.get("expires_at")
        if exp is None:
            problems.append("nous expires_at missing/unparseable")
        else:
            ttl = (exp - now).total_seconds()
            detail["ttl_seconds"] = round(ttl)
            if ttl <= 0:
                problems.append(f"nous token EXPIRED {abs(ttl):.0f}s ago (expires_at={exp.isoformat()})")
            elif ttl <= WARN_TTL_SECONDS:
                warnings.append(f"nous token near expiry TTL={ttl:.0f}s <= {WARN_TTL_SECONDS}s")
            else:
                detail["verdict"] = "GREEN"
    if not nous and auth.get("active_provider") == "nous":
        problems.append("active_provider=nous but no nous credential present (dispatch would crash)")

    # ---- shared nous_auth.json cross-check (source the fleet-cred-sync pushes from) ----
    detail["shared_path"] = str(SHARED_NOUS)
    try:
        shared = json.loads(SHARED_NOUS.read_text())
        sexp = parse_dt(shared.get("expires_at"))
        detail["shared_expires_at"] = shared.get("expires_at")
        if sexp is not None:
            sttl = (sexp - now).total_seconds()
            detail["shared_ttl_seconds"] = round(sttl)
            if sttl <= 0:
                problems.append(f"shared nous_auth.json token EXPIRED {abs(sttl):.0f}s ago")
            elif sttl <= WARN_TTL_SECONDS:
                warnings.append(f"shared nous token near expiry TTL={sttl:.0f}s")
    except FileNotFoundError:
        problems.append(f"shared nous_auth.json missing: {SHARED_NOUS}")
    except Exception:
        problems.append("shared nous_auth.json unreadable")

    # ---- classify / emit ----
    if problems:
        cls = "PROVIDER/CREDENTIAL RED"
        rc = 1
        lines = [f"[{cls}] jarvis-os-pm Nous token-expiry preflight FAILED"]
        lines += [f"  - {p}" for p in problems]
    elif warnings:
        cls = "PROVIDER/CREDENTIAL WARN"
        rc = 2
        lines = [f"[{cls}] jarvis-os-pm Nous token near expiry"]
        lines += [f"  - {w}" for w in warnings]
    else:
        cls = "GREEN"
        rc = 0
        lines = [f"[GREEN] jarvis-os-pm Nous token healthy (TTL={detail.get('ttl_seconds')}s)"]

    if verbose or rc != 0:
        for line in lines:
            print(line)
        if verbose:
            safe = {k: v for k, v in detail.items() if "token" not in k.lower()}
            print(json.dumps(safe, default=str, sort_keys=True))

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
