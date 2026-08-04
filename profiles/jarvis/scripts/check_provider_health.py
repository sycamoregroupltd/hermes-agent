#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Provider Health Check Governor

Reads ~/.hermes/state/provider_priority.json, performs health checks on each
provider (CLI and API types), updates status/priority/last_checked, and writes
back atomically.

Run every 5 minutes via cron.
"""

import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any


STATE_DIR = Path.home() / ".hermes" / "state"
PRIORITY_FILE = STATE_DIR / "provider_priority.json"

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

# Basic API reachability endpoints (HEAD request, short timeout)
# Full test query would require provider SDK + credentials; this is connectivity + basic health proxy
API_CHECK_ENDPOINTS: Dict[str, str] = {
    "xai-oauth": "https://api.x.ai/v1/models",
    # MUST match model.base_url in config.yaml. Was api.nousresearch.com ("approximate;
    # adjust if needed") which does not resolve -- http=000 -- so nous reported DOWN
    # unconditionally while inference was healthy. A probe of the wrong host is not a
    # health check, it is a constant. Verified 2026-08-04: this host returns 200.
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
    """Check endpoint reachability (proxy for API health).

    Uses GET, not HEAD: inference-api.nousresearch.com answers HEAD with 403 while
    GET returns 200, so a HEAD probe reported a healthy provider as DOWN.

    HTTPError is handled SEPARATELY on purpose. It is raised for every 4xx, so the
    old blanket `except (..., HTTPError, ...)` swallowed them before the
    `200 <= status < 500` tolerance could apply -- the "4xx may be auth, still
    reachable" intent was dead code, and any auth-gated endpoint read as DOWN.
    Reachable-but-unauthorized is UP for this check's purpose; only 5xx and
    transport failures are DOWN.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-health/1"})
        with urllib.request.urlopen(req, timeout=5) as response:
            return 200 <= response.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500          # 4xx = reachable (auth/permission), not down
    except (urllib.error.URLError, TimeoutError, OSError):
        return False                 # DNS/TLS/connection/timeout = genuinely down


def update_priority(
    current_priority: int, old_status: str, new_status: str, default: int
) -> int:
    """Adjust priority based on status change per strategy."""
    if new_status == "UP" and old_status != "UP":
        return default
    if new_status in ("DOWN", "RATE_LIMITED") and old_status == "UP":
        return max(10, current_priority // 2)  # halve but keep reasonable floor
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

        if ptype == "cli":
            cmd = CLI_CHECK_COMMANDS.get(provider_id)
            healthy = run_cli_check(cmd) if cmd else False
            new_status = "UP" if healthy else "DOWN"
        elif ptype == "api":
            url = API_CHECK_ENDPOINTS.get(provider_id)
            healthy = (
                run_api_check(url) if url else True
            )  # fallback to keep alive if no endpoint
            new_status = "UP" if healthy else "DOWN"
        else:
            new_status = "UNKNOWN"

        new_priority = update_priority(
            old_priority, old_status, new_status, default_prio
        )

        p["status"] = new_status
        p["priority"] = new_priority
        p["last_checked"] = now
        # Optionally append note about check result
        if "notes" in p:
            p["notes"] = (
                p["notes"].split(" | Health:")[0]
                + f" | Health: {new_status} @ {now[:16]}"
            )
        else:
            p["notes"] = f"Health: {new_status} @ {now[:16]}"

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
