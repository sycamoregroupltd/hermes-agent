#!/usr/bin/env python3
"""fleet-slo-collector.py — Daily fleet SLO collector wrapper.

Invokes fleet-slo-report.py to generate today's SLO report.
Designed as a no_agent cron script for daily collection at midnight local.

Exit codes:
  0 — report generated successfully
  1 — failure generating report
"""
import subprocess
import sys
import os
import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_SCRIPT = os.path.join(SCRIPT_DIR, "fleet-slo-report.py")
OUTPUT_FILE = os.path.join("/tmp", f"FLEET_SLO_REPORT_{datetime.date.today():%Y-%m-%d}.md")


def main():
    if not os.path.isfile(REPORT_SCRIPT):
        print(f"ERROR: upstream reporter not found: {REPORT_SCRIPT}", file=sys.stderr)
        sys.exit(1)

    env = os.environ.copy()
    env["FLEET_SLO_OUTPUT"] = OUTPUT_FILE

    result = subprocess.run(
        [sys.executable, REPORT_SCRIPT],
        env=env,
        capture_output=True, text=True, timeout=300,
    )

    print(result.stdout.strip())
    if result.returncode != 0:
        print(f"stderr: {result.stderr.strip()}", file=sys.stderr)

    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
