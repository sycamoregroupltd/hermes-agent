#!/usr/bin/env python3
"""Classify a Hermes hook payload for dangerous docker/podman terminal use.

Reads JSON (or raw text) on stdin. Prints a block reason on stdout, or nothing
to allow. Fail-open: any parse error prints nothing (caller allows).
"""
from __future__ import annotations

import json
import re
import sys


def _command_from_payload(raw: str) -> str:
    try:
        d = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(d, dict):
        return raw
    for k in ("command", "cmd", "input"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    tool_input = d.get("tool_input") or d.get("arguments") or d.get("params") or {}
    if isinstance(tool_input, dict):
        for k in ("command", "cmd", "script"):
            v = tool_input.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return raw


def classify(text: str) -> str:
    low = text.lower()
    db_ct = re.search(
        r"(sycodetrading-supabase-db|supabase-db|sycodetrading-pgbouncer|"
        r"timescaledb)",
        low,
    )
    if re.search(r"(docker|podman|nerdctl)\s+run\b[^\n]*--privileged\b", low):
        return "docker run --privileged blocked (gate-terminal-docker-safety)"
    if re.search(
        r"(docker|podman|nerdctl)\s+exec\b[^\n]*\b(printenv|\benv\b|cat\s+[^\n]*\.env|"
        r"cat\s+/proc/\S+/environ)",
        low,
    ):
        return "docker exec env/.env dump blocked (gate-terminal-docker-safety)"
    if re.search(
        r"(docker|podman|nerdctl)\s+inspect\b[^\n]*(config\.env)",
        low,
    ):
        return "docker inspect Env dump blocked (gate-terminal-docker-safety)"
    if re.search(r"(docker|podman|nerdctl)\s+exec\b", low) and db_ct:
        return "docker exec into supabase/postgres container blocked (UAA: becomes supabase_admin)"
    if re.search(
        r"((docker|podman)\s+compose\b[^\n]*\b(down|rm)\b.*sycodetrading|"
        r"docker\s+rm\s+[^\n]*-f[^\n]*sycodetrading|"
        r"docker\s+compose\b[^\n]*sycodetrading[^\n]*\bdown\b)",
        low,
    ):
        return "docker compose down/rm of sycodetrading* blocked (gate-terminal-docker-safety)"
    if (
        db_ct
        and re.search(r"\b(psql|pg_dump|pg_restore)\b", low)
        and re.search(r"\b(insert|update|delete|drop|alter|truncate|create)\b", low)
    ):
        return "psql write against supabase/postgres via terminal blocked (Frank-gated DML)"
    return ""


def main() -> int:
    raw = sys.stdin.read()
    try:
        reason = classify(_command_from_payload(raw))
    except Exception:
        return 0
    if reason:
        sys.stdout.write(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
