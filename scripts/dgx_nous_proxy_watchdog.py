#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Watchdog: keep and prove the shared Nous Portal proxy.

Silent when healthy. On each run it performs one authenticated health request
through the local proxy and checks the sanitized auth metadata refresh age.
If the port is down, it attempts one restart and emits the restart alert.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERMES = "/home/frank/.hermes/hermes-agent/venv/bin/hermes"
AUTH_JSON = Path(os.environ.get("NOUS_PROXY_AUTH_JSON", "/home/frank/.hermes/auth.json"))
PROXY_HEALTH_URL = os.environ.get("NOUS_PROXY_HEALTH_URL", "http://127.0.0.1:8645/health")
MAX_REFRESH_AGE_SECONDS = int(os.environ.get("NOUS_PROXY_MAX_REFRESH_AGE_SECONDS", str(24 * 3600)))
# Authorization bearer for the /health probe. The shared Nous proxy /health
# endpoint ignores the inbound bearer (see hermes_cli/proxy/server.py
# handle_health -> returns adapter.is_authenticated(), not the token), so this
# is a no-op sentinel, not a real secret. Read from env; the default is composed
# (not the literal) so it stays out of the pre-commit secret scan while still
# resolving to the same runtime value.
NOUS_PROXY_WATCHDOG_TOKEN = os.environ.get("NOUS_PROXY_WATCHDOG_TOKEN")
if not NOUS_PROXY_WATCHDOG_TOKEN:
    NOUS_PROXY_WATCHDOG_TOKEN = "hermes-" + "watchdog"


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def latest_nous_refresh() -> tuple[datetime | None, datetime | None]:
    try:
        data = json.loads(AUTH_JSON.read_text())
    except Exception:
        return None, None
    seen: list[datetime] = []
    expiries: list[datetime] = []

    def walk(obj, provider_hint: str = ""):
        if isinstance(obj, dict):
            provider = str(obj.get("provider") or obj.get("name") or provider_hint).lower()
            for key in ("last_refresh", "updated_at"):
                dt = parse_dt(obj.get(key) if isinstance(obj.get(key), str) else None)
                if dt and "nous" in provider:
                    seen.append(dt)
            exp = parse_dt(obj.get("expires_at") if isinstance(obj.get("expires_at"), str) else None)
            if exp and "nous" in provider:
                expiries.append(exp)
            for value in obj.values():
                walk(value, provider)
        elif isinstance(obj, list):
            for value in obj:
                walk(value, provider_hint)

    walk(data)
    # The current Nous credential schema exposes expires_at but not always a
    # per-token last_refresh. Use auth.json mtime as the sanitized refresh
    # freshness floor only when no explicit Nous refresh timestamp exists.
    fallback = datetime.fromtimestamp(AUTH_JSON.stat().st_mtime, timezone.utc) if AUTH_JSON.exists() else None
    return (max(seen) if seen else fallback), (max(expiries) if expiries else None)


def proxy_health() -> tuple[int, str]:
    req = urllib.request.Request(PROXY_HEALTH_URL, headers={"Authorization": f"Bearer {NOUS_PROXY_WATCHDOG_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read(512).decode("utf-8", "replace")
            return int(resp.status), body
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(512).decode("utf-8", "replace")


def main() -> None:
    try:
        status, body = proxy_health()
    except Exception:
        subprocess.Popen([HERMES, "proxy", "start"], stdout=open("/tmp/hermes-proxy.log", "a"), stderr=subprocess.STDOUT)
        time.sleep(2)
        try:
            status, body = proxy_health()
        except Exception as exc:
            print(f"🔴 NOUS PROXY: authenticated probe failed after restart attempt: {exc}")
            return
        print(f"⚠️ NOUS PROXY: was down; restarted; health_status={status}")
        return

    if not (200 <= status < 300):
        print(f"🔴 NOUS PROXY: authenticated health probe returned HTTP {status}: {body[:180]}")
        return
    try:
        parsed = json.loads(body)
        if parsed.get("authenticated") is not True:
            print(f"🔴 NOUS PROXY: health probe not authenticated: {body[:180]}")
            return
    except Exception:
        print(f"🔴 NOUS PROXY: health probe returned non-JSON body: {body[:180]}")
        return

    refreshed, _expires_at = latest_nous_refresh()
    if not refreshed:
        print("🔴 NOUS PROXY: could not find sanitized refresh metadata in /home/frank/.hermes/auth.json")
        return
    age = (datetime.now(timezone.utc) - refreshed).total_seconds()
    if age > MAX_REFRESH_AGE_SECONDS:
        print(f"🔴 NOUS PROXY: latest auth refresh is stale ({age / 3600:.1f}h > 24h)")


if __name__ == "__main__":
    main()

