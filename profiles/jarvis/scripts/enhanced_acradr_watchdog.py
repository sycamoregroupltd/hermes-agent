#!/usr/bin/env python3
# CANONICAL SOURCE — Enhanced ACRADR Phase 3 liveness watchdog (t_00a856e5).
#
# Verifies the hourly ACRADR scanner (enhanced_acradr_runner.py) is actually
# ticking. The runner writes a heartbeat timestamp to
#   /home/frank/.hermes/profiles/jarvis/cron/state/acradr_heartbeat.txt
# on every successful run. This watchdog checks how stale that timestamp is and
# alerts discord:#jarvis-os-governance ONLY when the scanner has gone silent
# (missed >= STALE_MINUTES). It is silent on success (no Discord spam).
#
# Runs as `no_agent: true`; `deliver: local` (it owns its own Discord alert).
# Exit code: 0 healthy, 2 stale (so the cron layer can also flag it), 1 error.

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

JARVIS_HOME = os.environ.get("HERMES_HOME", "/home/frank/.hermes/profiles/jarvis")
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
HEARTBEAT_FILE = Path(os.environ.get(
    "ACRADR_HEARTBEAT_FILE",
    "/home/frank/.hermes/profiles/jarvis/cron/state/acradr_heartbeat.txt",
))
ALERT_TARGET = "discord:jarvis-os-governance"
STALE_MINUTES = int(os.environ.get("ACRADR_STALE_MINUTES", "90"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_heartbeat(path: Path = HEARTBEAT_FILE) -> Optional[datetime]:
    try:
        raw = Path(path).read_text(encoding="utf-8").strip().splitlines()
        if not raw:
            return None
        return datetime.strptime(raw[-1].strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except Exception:
        return None


def _alert(message: str) -> bool:
    env = os.environ.copy()
    env["HERMES_HOME"] = JARVIS_HOME
    try:
        res = subprocess.run(
            [HERMES_BIN, "-p", "jarvis", "send", "-q", "-t", ALERT_TARGET,
             "-s", "ACRADR Watchdog", message],
            capture_output=True, text=True, timeout=120, env=env,
        )
        return res.returncode == 0
    except Exception as e:
        sys.stderr.write(f"watchdog alert failed: {e}\n")
        return False


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="ACRADR scanner liveness watchdog")
    p.add_argument("--heartbeat-file", default=str(HEARTBEAT_FILE))
    p.add_argument("--stale-minutes", type=int, default=STALE_MINUTES)
    p.add_argument("--no-alert", action="store_true",
                   help="Report only, do not send Discord alert (for tests).")
    args = p.parse_args(argv)

    hb = _read_heartbeat(args.heartbeat_file)
    if hb is None:
        msg = (f"🚨 ACRADR Watchdog: scanner heartbeat MISSING "
               f"(expected at {args.heartbeat_file}). The hourly anomaly detector "
               f"may never have run or its state dir is gone.")
        if not args.no_alert:
            _alert(msg)
        print(msg)
        return 2

    age_min = (_now() - hb).total_seconds() / 60.0
    if age_min > args.stale_minutes:
        msg = (f"🚨 ACRADR Watchdog: scanner heartbeat STALE "
               f"({age_min:.0f}m old, threshold {args.stale_minutes}m). "
               f"Last tick {hb.strftime('%Y-%m-%dT%H:%M:%SZ')}. "
               f"The hourly anomaly detector may be down.")
        if not args.no_alert:
            _alert(msg)
        print(msg)
        return 2

    print(f"✅ ACRADR scanner healthy — last tick "
          f"{hb.strftime('%Y-%m-%dT%H:%M:%SZ')} ({age_min:.0f}m ago).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
