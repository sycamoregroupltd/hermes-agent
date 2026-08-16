#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Provider Health Check Governor

Reads ~/.hermes/state/provider_priority.json, performs health checks on each
provider (CLI and API types), updates status/priority/last_checked, and writes
back atomically.

Run every 5 minutes via cron.

Auth overlay (2026-08-16, fleet recovery):
A reachable inference host is NOT a login. Nous with an empty credential pool
or a revoked refresh session must not report UP. Codex usage_limit_reached
must report RATE_LIMITED, not UP. This script never prints tokens.
"""

import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


STATE_DIR = Path.home() / ".hermes" / "state"
PRIORITY_FILE = STATE_DIR / "provider_priority.json"
AUTH_FILE = Path.home() / ".hermes" / "auth.json"

# Default priorities for restoration (from strategy / initialize)
DEFAULT_PRIORITIES = {
    "gemini-cli": 100,
    "claude-code": 95,
    "xai-oauth": 90,
    "nous": 80,
    "openai-codex": 50,
}

# CLI check commands (version or status that exits 0 on healthy auth)
CLI_CHECK_COMMANDS: Dict[str, list] = {
    "gemini-cli": ["gemini", "--version"],
    "claude-code": ["claude", "--version"],
}

# Basic API reachability endpoints (GET request, short timeout)
API_CHECK_ENDPOINTS: Dict[str, str] = {
    "xai-oauth": "https://api.x.ai/v1/models",
    "nous": "https://inference-api.nousresearch.com/v1/models",
    "openai-codex": "https://api.openai.com/v1/models",
}


def run_cli_check(cmd: list) -> bool:
    """Run CLI command with timeout; return True if exit code 0."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def run_api_check(url: str) -> bool:
    """Check endpoint reachability (proxy for API host health, not login)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-health/1"})
        with urllib.request.urlopen(req, timeout=5) as response:
            return 200 <= response.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _load_auth() -> dict:
    try:
        return json.loads(AUTH_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _pool_entries(auth: dict, name: str) -> list:
    pool = auth.get("credential_pool") or {}
    items = pool.get(name)
    return items if isinstance(items, list) else []


def _provider_meta(auth: dict, name: str) -> dict:
    providers = auth.get("providers") or {}
    meta = providers.get(name)
    return meta if isinstance(meta, dict) else {}


def auth_overlay(provider_id: str, reachable: bool) -> tuple[str, str]:
    """Return (status, note) after combining reachability with login state.

    Never returns secrets. Empty/revoked Nous is AUTH_REQUIRED, not UP.
    """
    auth = _load_auth()
    if provider_id == "nous":
        entries = _pool_entries(auth, "nous")
        err = (_provider_meta(auth, "nous").get("last_auth_error") or {})
        if not entries:
            code = err.get("code") or "no_pool_entry"
            return (
                "AUTH_REQUIRED",
                f"host={'up' if reachable else 'down'}; credential_pool.nous empty ({code})",
            )
        if err.get("relogin_required"):
            return (
                "AUTH_REQUIRED",
                f"host={'up' if reachable else 'down'}; relogin_required={err.get('code')}",
            )
        return ("UP" if reachable else "DOWN", f"host={'up' if reachable else 'down'}; pool={len(entries)}")
    if provider_id == "openai-codex":
        entries = _pool_entries(auth, "openai-codex")
        if not entries:
            return ("AUTH_REQUIRED", f"host={'up' if reachable else 'down'}; credential_pool.openai-codex empty")
        status = (entries[0] or {}).get("last_status")
        if status == "exhausted":
            return ("RATE_LIMITED", f"host={'up' if reachable else 'down'}; last_status=exhausted")
        if status not in (None, "ok"):
            return ("DOWN", f"host={'up' if reachable else 'down'}; last_status={status}")
        return ("UP" if reachable else "DOWN", f"host={'up' if reachable else 'down'}; last_status={status or 'ok'}")
    if provider_id == "xai-oauth":
        entries = _pool_entries(auth, "xai-oauth")
        err = (_provider_meta(auth, "xai-oauth").get("last_auth_error") or {})
        if not entries:
            return ("AUTH_REQUIRED", f"host={'up' if reachable else 'down'}; credential_pool.xai-oauth empty")
        status = (entries[0] or {}).get("last_status")
        if status == "ok":
            return ("UP" if reachable else "DOWN", f"host={'up' if reachable else 'down'}; pool_ok")
        if err.get("relogin_required") or status in ("invalid_grant", "error"):
            return (
                "AUTH_REQUIRED",
                f"host={'up' if reachable else 'down'}; last_status={status}",
            )
        return ("UP" if reachable else "DOWN", f"host={'up' if reachable else 'down'}; last_status={status}")
    return ("UP" if reachable else "DOWN", f"host={'up' if reachable else 'down'}")


def update_priority(
    current_priority: int, old_status: str, new_status: str, default: int
) -> int:
    """Adjust priority based on status change per strategy."""
    if new_status == "UP" and old_status != "UP":
        return default
    if new_status in ("DOWN", "RATE_LIMITED", "AUTH_REQUIRED") and old_status == "UP":
        return max(10, current_priority // 2)
    return current_priority


def check_provider_health(providers: list) -> list:
    """Run checks and return updated list."""
    now = datetime.now(timezone.utc).isoformat()
    updated = []

    for p in providers:
        provider_id = p["id"]
        ptype = p["type"]
        old_status = p.get("status", "UNKNOWN")
        old_priority = p.get("priority", 50)
        default_prio = DEFAULT_PRIORITIES.get(provider_id, 50)
        note = ""

        if ptype == "cli":
            cmd = CLI_CHECK_COMMANDS.get(provider_id)
            healthy = run_cli_check(cmd) if cmd else False
            new_status = "UP" if healthy else "DOWN"
            note = f"cli={'up' if healthy else 'down'}"
        elif ptype == "api":
            url = API_CHECK_ENDPOINTS.get(provider_id)
            reachable = run_api_check(url) if url else True
            new_status, note = auth_overlay(provider_id, reachable)
        else:
            new_status = "UNKNOWN"
            note = "unknown type"

        new_priority = update_priority(
            old_priority, old_status, new_status, default_prio
        )

        p["status"] = new_status
        p["priority"] = new_priority
        p["last_checked"] = now
        prefix = (p.get("notes") or "").split(" | Health:")[0].strip()
        p["notes"] = f"{prefix} | Health: {new_status} @ {now[:16]} | {note}".strip(" |")

        updated.append(p)

    return updated


def write_atomic(data: list) -> None:
    """Write JSON atomically using temp file + rename."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=STATE_DIR, prefix="provider_priority.", suffix=".json.tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, PRIORITY_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main():
    if not PRIORITY_FILE.exists():
        print(f"ERROR: {PRIORITY_FILE} not found. Run initialize_providers.py first.")
        return

    with open(PRIORITY_FILE, "r") as f:
        providers = json.load(f)

    updated_providers = check_provider_health(providers)
    write_atomic(updated_providers)

    print(f"Health check complete. Updated {PRIORITY_FILE}")
    for p in updated_providers:
        print(f"  {p['id']}: {p['status']} (prio {p['priority']})")


if __name__ == "__main__":
    main()
