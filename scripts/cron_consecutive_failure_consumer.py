#!/usr/bin/env python3
"""Cron-script-failure consumer (criterion 3 of t_4ef9eaa8).

Monitors /home/frank/.hermes/profiles/jarvis/cron/output/ for no_agent
cron jobs whose last run report contains failure markers. Emits stdout
summary when there are N or more consecutive failures; silent otherwise.

Usage: cron_consecutive_failure_consumer.py [--threshold N]
Default threshold: N=3 consecutive failed runs before alerting.

Deliver-to is left to whatever profile's gateway/ticker owns it (local or channel).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

CRON_OUTPUT = Path(
    "/home/frank/.hermes/profiles/jarvis/cron/output"
)
EXECUTIONS_DB = Path(
    "/home/frank/.hermes/profiles/jarvis/cron/executions.db"
)
ALERT_FILE = CRON_OUTPUT / "consecutive_failure_alert.last"
CONSECUTIVE_FAILURE_THRESHOLD = int(
    sys.argv[1].removeprefix("--threshold=") if "--threshold=" in sys.argv[1] else 3
) if len(sys.argv) > 1 and sys.argv[1].startswith("--") else 3


def get_script_failures(threshold: int) -> list[dict]:
    """Scan cron output dirs for no_agent script failures.

    For each subdirectory under CRON_OUTPUT/<job_id>/, reads the newest
    .md file and checks whether the run was a failure (exit code != 0 or
    'FAILED'/'ERROR' markers in the body). Returns list of dicts:
      {job_id, name, latest_output_path, failure_text, run_count}.
    """
    failures = []

    if not CRON_OUTPUT.exists():
        return failures

    for job_dir in sorted(CRON_OUTPUT.iterdir()):
        if not job_dir.is_dir():
            continue

        md_files = sorted(job_dir.glob("*.md"))
        if not md_files:
            continue

        latest = md_files[-1]
        content = latest.read_text(encoding="utf-8", errors="replace")
        lower = content.lower()

        # Failure detection heuristics
        is_failed = False
        fail_reason = ""

        if "exit code 1" in lower or "script exited with code 1" in lower:
            is_failed = True
            # Extract the actual error line
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("stderr:") or stripped.startswith("ERROR"):
                    fail_reason = stripped
                    break
            if not fail_reason:
                fail_reason = "Script exit code 1"

        elif "failed:" in lower[:200] and ("error" in lower or "fail" in lower):
            is_failed = True
            fail_reason = "Script reported failure"

        elif "timeouterror" in lower:
            is_failed = True
            for line in content.splitlines():
                if "timeout" in line.lower():
                    fail_reason = line.strip()
                    break

        if not is_failed:
            continue

        # Find matching job name from names subdir or by ID mapping
        name = job_dir.name  # job_id is often the dir name
        try:
            names_json = Path(
                f"/home/frank/.hermes/profiles/jarvis/cron/{job_dir.name}_names.json"
            )
            if names_json.exists():
                name = json.loads(names_json.read_text())["name"]
        except Exception:
            pass

        # Count recent consecutive failures (look at all MD files in reverse order)
        run_count = 0
        for f in reversed(md_files):
            fc = f.read_text(encoding="utf-8", errors="replace").lower()
            if any(m in fc for m in ["exit code 1", "script exited with code 1", "failed:", "timeouterror"]):
                run_count += 1
            else:
                break

        failures.append({
            "job_id": job_dir.name,
            "name": name,
            "latest_output": str(latest),
            "reason": fail_reason,
            "run_count": run_count,
        })

    return failures


def count_recent_failures_via_db(job_id: str, days: int = 7) -> int:
    """Fallback: count failed executions via executions.db."""
    try:
        con = sqlite3.connect(str(EXECUTIONS_DB))
        cur = con.execute(
            """SELECT COUNT(*) FROM executions
               WHERE job_id = ? AND status IN ('failed', 'crashed')
               AND started_at >= datetime('now', ?)""",
            (job_id, f"-{days} days"),
        )
        cnt = cur.fetchone()[0]
        con.close()
        return cnt
    except Exception:
        return 0


def main() -> int:
    print(f"Cron-consecutive-failure consumer (threshold={CONSECUTIVE_FAILURE_THRESHOLD})")
    print("=" * 60)

    failures = get_script_failures(CONSECUTIVE_FAILURE_THRESHOLD)

    alerted = [f for f in failures if f["run_count"] >= CONSECUTIVE_FAILURE_THRESHOLD]

    for f in sorted(alerted, key=lambda x: -x["run_count"]):
        print(
            f"\n[ALERT] {f['name']} ({f['job_id']})\n"
            f"  Output : {f['latest_output']}\n"
            f"  Reason : {f['reason']}\n"
            f"  Consecutive failures: {f['run_count']}"
        )

    # Record alert state for history tracking
    if alerted:
        AlertRecord = []
        for a in alerted:
            AlertRecord.append({
                "job_id": a["job_id"],
                "name": a["name"],
                "consecutive": a["run_count"],
                "alerted_at": "auto",
            })
        ALERT_FILE.write_text(
            json.dumps(AlertRecord, indent=2), encoding="utf-8"
        )
        print(f"\n{'='*60}")
        print(f"Result: {len(alerted)} job(s) exceed consecutive-failure threshold.")
        return 1

    print("\nNo jobs exceed consecutive-failure threshold.")
    print("Result: ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
