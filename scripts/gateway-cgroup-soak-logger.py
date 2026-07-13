#!/usr/bin/env python3
"""Gateway cgroup guard SOAK ledger logger (task t_74fcd323).

Companion to gateway-cgroup-guard.py (sprawl sentinel) and the systemd
memory-guard.conf drop-ins from t_a89cb802.

Why this exists:
  The soak acceptance criteria (no OOM, no cap-correlated restart,
  steady-state under MemoryHigh, zero sprawl) need a *durable time-series*
  record over the 7-day window. The sprawl sentinel's alert channel
  (discord #fleet-reports) is currently unconfigured, so without a local
  ledger the soak would have NO historical evidence at day 7.

What it does (read-only, no gate crossed):
  * For each of the 5 gateway cgroups, reads /sys/fs/cgroup memory.current /
    memory.high / memory.max / pids.max / pids.current / memory.events and the
    unit NRestarts (systemctl --user show).
  * Runs the sprawl sentinel (report-only) as a subprocess to capture the
    total_sprawl count.
  * Writes ONE append-only JSONL line per tick to the ledger.
  * Writes/updates the manifest (apply date, config, due date) on first run.

It NEVER sets cgroup limits, NEVER kills processes, NEVER restarts units.

Exit code: always 0 (observational). The soak verifier (day 7) reads the
ledger and decides pass/fail.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# --- config (from t_a89cb802 ADR) -------------------------------------------------
APPLY_DATE = "2026-07-11"          # date guards were applied
SOAK_DAYS = 7
CGROUP_ROOT = "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice"

GATEWAY_UNITS = [
    "hermes-gateway-jarvis",
    "hermes-gateway-jarvis-os-pm",
    "hermes-gateway-sycode-trading-pm",
    "hermes-gateway-jarvis-voice",
    "hermes-surface-gateway",
]

# MemoryHigh (soft) / MemoryMax (hard) per ADR, for status classification.
LIMITS = {
    "hermes-gateway-jarvis":            (8 * 1024**3, 24 * 1024**3),
    "hermes-gateway-jarvis-os-pm":      (16 * 1024**3, 52 * 1024**3),
    "hermes-gateway-sycode-trading-pm": (2 * 1024**3, 4 * 1024**3),
    "hermes-gateway-jarvis-voice":      (1 * 1024**3, 2 * 1024**3),
    "hermes-surface-gateway":           (512 * 1024**2, 1 * 1024**3),
}

SENTINEL = Path("/home/frank/.hermes/scripts/gateway-cgroup-guard.py")
LEDGER_DIR = Path("/home/frank/.hermes/var/gateway-cgroup-soak")
LEDGER = LEDGER_DIR / "ledger.jsonl"
MANIFEST = LEDGER_DIR / "manifest.json"

APPLY_TS_UTC = datetime(2026, 7, 11, 10, 52, tzinfo=timezone.utc)  # 11:52 BST


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _int(v: str | None) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def unit_restarts(unit: str) -> int:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", "NRestarts", "--value", f"{unit}.service"],
            capture_output=True, text=True, timeout=30,
        )
        return _int(out.stdout.strip()) or 0
    except (OSError, subprocess.SubprocessError):
        return -1  # unknown


def unit_active_enter(unit: str) -> str | None:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", "ActiveEnterTimestamp", "--value", f"{unit}.service"],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def sentinel_sprawl() -> int:
    """Run the sprawl sentinel report-only and return total_sprawl count."""
    if not SENTINEL.exists():
        return -1
    try:
        out = subprocess.run(
            [sys.executable, str(SENTINEL), "--json"],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout)
        return int(data.get("total_sprawl", -1))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return -1


def classify(cur: int | None, high: int | None) -> str:
    if cur is None or high is None or high <= 0:
        return "unknown"
    ratio = cur / high
    if ratio >= 1.0:
        return "over_high"
    if ratio >= 0.9:
        return "near_high"
    return "ok_under_high"


def snapshot() -> dict:
    ts = datetime.now(timezone.utc)
    units: dict = {}
    for unit in GATEWAY_UNITS:
        base = f"{CGROUP_ROOT}/{unit}.service"
        cur = _int(_read(f"{base}/memory.current"))
        high = _int(_read(f"{base}/memory.high"))
        mx = _int(_read(f"{base}/memory.max"))
        pmax = _int(_read(f"{base}/pids.max"))
        pcur = _int(_read(f"{base}/pids.current"))
        events_raw = _read(f"{base}/memory.events") or ""
        ev = {}
        for tok in events_raw.split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                ev[k] = _int(v)
        restarts = unit_restarts(unit)
        status = classify(cur, high)
        # max approach warning
        max_warn = (cur is not None and mx is not None and mx > 0 and (cur / mx) >= 0.85)
        units[unit] = {
            "memory_current": cur,
            "memory_high": high,
            "memory_max": mx,
            "pids_current": pcur,
            "pids_max": pmax,
            "oom": ev.get("oom", 0),
            "oom_kill": ev.get("oom_kill", 0),
            "oom_group_kill": ev.get("oom_group_kill", 0),
            "high_events": ev.get("high", 0),
            "restart_count": restarts,
            "status": status,
            "max_approach_warn": bool(max_warn),
        }
    sprawl = sentinel_sprawl()
    return {
        "ts_utc": ts.isoformat(),
        "task_id": "t_74fcd323",
        "soak_day": (ts - APPLY_TS_UTC).days + 1,
        "units": units,
        "total_sprawl": sprawl,
    }


def write_manifest() -> None:
    due = APPLY_TS_UTC + timedelta(days=SOAK_DAYS)
    manifest = {
        "task_id": "t_74fcd323",
        "apply_date": APPLY_DATE,
        "apply_ts_utc": APPLY_TS_UTC.isoformat(),
        "soak_days": SOAK_DAYS,
        "due_ts_utc": due.isoformat(),
        "due_date": due.date().isoformat(),
        "units": GATEWAY_UNITS,
        "limits": {u: {"memory_high": h, "memory_max": m} for u, (h, m) in LIMITS.items()},
        "ledger": str(LEDGER),
        "verifier": "/home/frank/.hermes/profiles/devops/scripts/gateway-cgroup-soak-verify.py",
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2))


def main() -> int:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    if not MANIFEST.exists():
        write_manifest()
    rec = snapshot()
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    # Compact stdout (cron is deliver=local / silent on empty; we print a short line)
    n_over = sum(1 for u in rec["units"].values() if u["status"] == "over_high")
    n_oom = sum(1 for u in rec["units"].values() if u["oom_kill"] or u["oom_group_kill"])
    print(f"soak day={rec['soak_day']} units={len(rec['units'])} "
          f"over_high={n_over} oom={n_oom} sprawl={rec['total_sprawl']} "
          f"-> {LEDGER.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
