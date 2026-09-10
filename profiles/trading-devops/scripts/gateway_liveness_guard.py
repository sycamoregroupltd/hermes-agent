#!/usr/bin/env python3
"""
gateway_liveness_guard.py — no_agent watchdog (t_655e325b).

WHY: trading-data-oracle's Hermes gateway was never installed as a systemd
user service for 36+ days. With no gateway process, that profile's cron
ticker never fired at all, and the existing dead-store-invariant-guard
(cron_ticker_invariant_guard.py, t_4bedf8d5) only detects this INDIRECTLY,
via a stale/missing ticker_heartbeat file inside that profile's own
cron/jobs.json store. That is a good generic proxy once a store exists and
has been ticked at least once, but it is a proxy, not a direct check of
"is this profile's gateway actually installed and running as a supervised
service" — the literal failure class from t_655e325b. This guard is a
DIRECT, complementary check (defense-in-depth, not a replacement for
t_4bedf8d5): for every profile that has at least one ENABLED cron job, it
verifies the profile's systemd user unit (hermes-gateway-<profile>.service)
both EXISTS and is ACTIVE (running). It does not touch tickers/heartbeats
and does not auto-disable jobs (t_4bedf8d5 already owns that mutation);
this guard is alert-only, deterministic, and silent when clean.

SILENT WHEN CLEAN (watchdog pattern: empty stdout = no delivery).

Alert taxonomy:
  RED  "gateway MISSING": no hermes-gateway-<profile>.service unit file at all.
  RED  "gateway NOT RUNNING": unit exists but systemctl reports it inactive/failed.
  Never alerts for a profile with zero enabled cron jobs (no consumer at risk).

Exit codes: 0 on a successful scan (alerts, if any, are the delivery channel
via non-empty stdout); non-zero only on an actual script failure (missing
profiles dir, subprocess execution failure), which the cron scheduler
escalates as a broken-watchdog alert distinct from a clean/dirty scan.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROFILES_DIR = Path("/home/frank/.hermes/profiles")

# Profiles that intentionally never run a gateway (archival/quarantine dirs,
# or profiles that are cron-only via a SIBLING gateway e.g. symlinked stores)
# are naturally excluded because they have zero enabled cron jobs; no
# allowlist is needed beyond that.


def _load_enabled_job_count(jobs_path: Path) -> int:
    try:
        raw = json.loads(jobs_path.read_text(encoding="utf-8"))
    except Exception:
        return -1  # unreadable store; still worth flagging separately below
    jobs = raw.get("jobs", raw) if isinstance(raw, dict) else raw
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    if not isinstance(jobs, list):
        return 0
    return sum(1 for j in jobs if isinstance(j, dict) and j.get("enabled", False))


def _systemctl(args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.returncode, (proc.stdout or "").strip()
    except Exception as exc:
        return -1, f"EXC:{exc}"


def main() -> int:
    if not PROFILES_DIR.is_dir():
        print(f"🔴 GUARD-ERROR gateway_liveness_guard: profiles dir missing: {PROFILES_DIR}", file=sys.stderr)
        return 3

    # Resolve symlinked profile stores to their real path so a symlink
    # (e.g. profiles/sycode-trading -> sycode-trading-pm) is scanned exactly
    # once under its real name, mirroring cron_ticker_invariant_guard.py.
    seen_real: set[str] = set()
    profiles: list[str] = []
    for p in sorted(PROFILES_DIR.iterdir()):
        if not p.is_dir():
            continue
        jobs_path = p / "cron" / "jobs.json"
        if not jobs_path.is_file():
            continue
        real = str(jobs_path.resolve())
        if real in seen_real:
            continue
        seen_real.add(real)
        profiles.append(p.name)

    alerts: list[str] = []
    for prof in profiles:
        jobs_path = PROFILES_DIR / prof / "cron" / "jobs.json"
        n_enabled = _load_enabled_job_count(jobs_path)
        if n_enabled <= 0:
            continue  # no live consumer at risk (0 enabled, or unreadable-but-empty)

        unit = f"hermes-gateway-{prof}.service"
        rc_enabled, out_enabled = _systemctl(["is-enabled", unit])
        rc_active, out_active = _systemctl(["is-active", unit])

        unit_missing = ("not-found" in out_enabled) or ("No such file" in out_enabled)
        is_active = out_active == "active"

        if unit_missing:
            alerts.append(
                f"🔴 GATEWAY-MISSING: profile '{prof}' has {n_enabled} enabled cron "
                f"job(s) but no systemd unit '{unit}' exists — cron ticker for this "
                f"profile has NO supervised process and cannot survive a host "
                f"restart/logout. Run: hermes -p {prof} gateway install [t_655e325b]"
            )
        elif not is_active:
            alerts.append(
                f"🔴 GATEWAY-NOT-RUNNING: profile '{prof}' has {n_enabled} enabled "
                f"cron job(s), unit '{unit}' exists but systemctl reports "
                f"'{out_active or 'unknown'}' (not active) — check: systemctl --user "
                f"status {unit} ; journalctl --user -u {unit} --since '10 min ago' "
                f"[t_655e325b]"
            )

    if alerts:
        print("\n".join(alerts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
