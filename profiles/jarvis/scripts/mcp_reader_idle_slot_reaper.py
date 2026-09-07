#!/usr/bin/env python3
"""Reclaim idle mcp_reader backends when the role hits its connection ceiling.

Context (native-improve 2026-09-06 ~21:31 BST):
  trading-risk-reviewer hit FATAL: too many connections for role mcp_reader.
  Probe found 30/30 backends all idle for 1–2 days (last query ROLLBACK) from
  172.18.0.1 — classic leaked client pools, not active work.

Safety:
  - Only role mcp_reader (research SELECT role). Never touches deployer/live writers.
  - Only state=idle (never active / idle-in-transaction).
  - Only idle longer than IDLE_MIN_MINUTES (default 30).
  - Ceiling: at most MAX_KILL backends terminated per run (default 20).
  - Silent when healthy (empty stdout). Prints one JSON line on reclaim/alert.
  - No DDL, no credential changes, no docker restart, no hermes update.

Env:
  PG_CONTAINER (default sycodetrading-supabase-db)
  MCP_READER_ROLE (default mcp_reader)
  IDLE_MIN_MINUTES (default 30)
  MAX_KILL (default 20)
  TARGET_FREE (default 5)  — stop once free slots >= this
  PRESSURE_PCT (default 0.80) — only reclaim when used/limit >= this
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone


PG_CONTAINER = os.getenv("PG_CONTAINER", "sycodetrading-supabase-db")
ROLE = os.getenv("MCP_READER_ROLE", "mcp_reader")
IDLE_MIN = int(os.getenv("IDLE_MIN_MINUTES", "30"))
MAX_KILL = int(os.getenv("MAX_KILL", "20"))
TARGET_FREE = int(os.getenv("TARGET_FREE", "5"))
PRESSURE_PCT = float(os.getenv("PRESSURE_PCT", "0.80"))


def psql(sql: str) -> str:
    r = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", "postgres", "-Atc", sql],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        raise RuntimeError(msg[-1][:200] if msg else f"psql rc={r.returncode}")
    return r.stdout.strip()


def main() -> int:
    try:
        limit_s = psql(
            "SELECT rolconnlimit FROM pg_roles WHERE rolname = '%s';" % ROLE.replace("'", "''")
        )
        if not limit_s:
            print(json.dumps({"status": "error", "error": f"role {ROLE} missing"}), file=sys.stderr)
            return 2
        limit = int(limit_s)
        if limit < 0:
            # unlimited — nothing to reclaim under role ceiling
            return 0

        used = int(psql("SELECT count(*) FROM pg_stat_activity WHERE usename = '%s';" % ROLE.replace("'", "''")) or "0")
        free = max(0, limit - used)
        pressure = used / limit if limit else 0.0

        # Healthy once we have TARGET_FREE slots, even if still "pressured".
        if free >= TARGET_FREE:
            return 0
        if pressure < PRESSURE_PCT:
            return 0

        # candidates: idle only, aged past threshold, oldest first
        raw = psql(
            "SELECT pid || ',' || EXTRACT(EPOCH FROM (now() - state_change))::int "
            "FROM pg_stat_activity "
            "WHERE usename = '%s' AND state = 'idle' "
            "AND state_change < now() - interval '%d minutes' "
            "ORDER BY state_change ASC;"
            % (ROLE.replace("'", "''"), IDLE_MIN)
        )
        candidates: list[tuple[int, int]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            pid_s, age_s = line.split(",", 1)
            candidates.append((int(pid_s), int(float(age_s))))

        if not candidates:
            # at pressure but no reclaimable idle — alert only
            print(
                json.dumps(
                    {
                        "status": "pressure_no_idle",
                        "role": ROLE,
                        "used": used,
                        "limit": limit,
                        "free": free,
                        "pressure": round(pressure, 3),
                    },
                    sort_keys=True,
                )
            )
            return 0

        # Under pressure, take enough oldest idle to restore TARGET_FREE (capped by MAX_KILL).
        need = max(0, TARGET_FREE - free)
        if pressure >= PRESSURE_PCT and need == 0 and free < TARGET_FREE:
            need = TARGET_FREE - free
        if pressure >= PRESSURE_PCT and need < 1:
            need = 1
        to_kill = candidates[: min(MAX_KILL, need)]
        if not to_kill:
            return 0

        killed = []
        for pid, age in to_kill:
            ok = psql("SELECT pg_terminate_backend(%d);" % pid)
            if ok.lower() in {"t", "true", "1"}:
                killed.append({"pid": pid, "idle_s": age})

        used2 = int(psql("SELECT count(*) FROM pg_stat_activity WHERE usename = '%s';" % ROLE.replace("'", "''")) or "0")
        print(
            json.dumps(
                {
                    "status": "reclaimed",
                    "role": ROLE,
                    "killed": len(killed),
                    "pids": [k["pid"] for k in killed],
                    "used_before": used,
                    "used_after": used2,
                    "limit": limit,
                    "free_after": max(0, limit - used2),
                    "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
