#!/usr/bin/env python3
"""Write an Alertmanager-webhook-shaped alert into a spool dir for the jarvis drain.

Canonical spool writer for the cron alert relay (t_1b0f7543). Any profile-local
cron wrapper calls this with absolute paths so exactly ONE profile (jarvis) holds
the Discord token; the jarvis drain job (sycode-alertmanager-oob-spool-drain /
fleet-alert-spool-drain) delivers the spooled alerts to Discord.

Usage:
  spool_alert_write.py --spool DIR --alertname NAME [--severity SEV] [--summary TEXT]

Summary may also be read from stdin when --summary is omitted. The written JSON is
Alertmanager-webhook shaped (status/alerts[]/labels/annotations/startsAt) and is
chmod 644 so the drain (running as frank) can always read it, even when the
incoming dir is root-owned (the 2026-07-29 incident class).

Exit codes: 0 on success; 1 on write failure. This is a LOCAL-ONLY writer: it makes
no provider/API call.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spool", required=True, help="spool incoming dir (absolute)")
    ap.add_argument("--alertname", required=True)
    ap.add_argument("--severity", default="warning", choices=["info", "warning", "critical"])
    ap.add_argument("--summary", default=None, help="alert summary; stdin used if omitted")
    ap.add_argument("--max-chars", type=int, default=1500)
    args = ap.parse_args()

    summary = args.summary
    if summary is None:
        summary = sys.stdin.read()
    summary = (summary or "").strip()
    if not summary:
        # Nothing to alert about — silent success (watchdog contract).
        return 0
    if len(summary) > args.max_chars:
        summary = summary[: args.max_chars] + "\n...[truncated]"

    spool = Path(args.spool)
    try:
        spool.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        print(f"spool_alert_write: cannot create spool dir {spool}: {exc}", file=sys.stderr)
        return 1

    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = {
        "status": "firing",
        "alerts": [
            {
                "labels": {
                    "alertname": args.alertname,
                    "severity": args.severity,
                    "job": "cron-spool-relay",
                },
                "annotations": {"summary": summary},
                "startsAt": ts,
            }
        ],
    }
    file = spool / f"cronrelay-{args.alertname}-{int(time.time() * 1000)}.json"
    try:
        with open(file, "w") as f:
            json.dump(payload, f)
        # Lesson from 2026-07-29: chmod on EVERY write or a root-owned dir/file
        # makes the drain unable to read/unlink, and the alert is silently lost.
        os.chmod(file, 0o644)
    except Exception as exc:  # noqa: BLE001
        print(f"spool_alert_write: write failed {file}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
