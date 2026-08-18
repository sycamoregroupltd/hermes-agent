#!/usr/bin/env python3
"""Liveness monitor for the needs_input SLA probe (SEAT DECISION binding req #3).

An SLA probe that dies silently is WORSE than none: its absence is
indistinguishable from "no breaches", creating false confidence. This monitor
asserts that a sweep actually happened recently.

Exit codes:
  0  — probe ran within the freshness window (sweep present)
  1  — ALARM: heartbeat missing or older than --max-age-hours (sweep absent)
  2  — usage / config error

The probe emit is WEEKLY (Monday 08:00 UTC, cron needs-input-sla-probe-emit).
The default max-age is 8 days (192h) so a healthy week between sweeps does NOT
alarm, while a missed week is caught on the second week. A monitor can still
run frequently (e.g. every 7h) — it simply reports OK between digestives.

Typical cron wiring:
  needs-input-sla-probe-liveness.py --heartbeat /tmp/needs-input-sla-probe.heartbeat
If it exits 1, alert Frank / the owning profile.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

DEFAULT_HEARTBEAT = "/tmp/needs-input-sla-probe.heartbeat"
# The probe emit is now WEEKLY (Monday 08:00 UTC). The liveness monitor must
# tolerate a week between sweeps: max-age is set to 8 days (allows one full
# missed weekly sweep to still alarm on the second week without false alarms
# between digestives). Tighter values would fire on every healthy week.
DEFAULT_MAX_AGE_HOURS = 8 * 24  # 192h — one weekly sweep + one missed-week grace


def main() -> int:
    ap = argparse.ArgumentParser(description="Liveness alarm for the needs_input SLA probe")
    ap.add_argument("--heartbeat", default=DEFAULT_HEARTBEAT,
                   help=f"Heartbeat file written by the probe (default: {DEFAULT_HEARTBEAT})")
    ap.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS,
                   help=f"Alarm if heartbeat is older than this many hours (default: {DEFAULT_MAX_AGE_HOURS})")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc).timestamp()

    if not os.path.exists(args.heartbeat):
        print(f"ALARM: heartbeat file missing: {args.heartbeat} "
              f"(probe has never run or its writes are lost)")
        return 1

    try:
        with open(args.heartbeat) as f:
            content = f.read().strip()
        ts = int(content)
    except (OSError, ValueError) as e:
        print(f"ALARM: heartbeat unreadable/corrupt ({e}): {args.heartbeat}")
        return 1

    age_h = (now - ts) / 3600.0
    if age_h > args.max_age_hours:
        print(f"ALARM: last sweep {age_h:.1f}h ago exceeds max-age "
              f"{args.max_age_hours}h — probe sweep is absent")
        return 1

    print(f"OK: last sweep {age_h:.1f}h ago (within {args.max_age_hours}h)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
