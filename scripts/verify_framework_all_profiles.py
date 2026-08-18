#!/usr/bin/env python3
"""Read-only: search ALL profile cron stores for any reference to the DQSH framework."""
import json
import sys
from pathlib import Path

ROOT = Path("/home/frank/.hermes/profiles")
NEEDLES = ["data_quality_framework", "data-quality-framework", "sycode_data_quality", "sycode-data-quality"]

def main():
    found = False
    for prof_dir in sorted(ROOT.iterdir()):
        jobs_p = prof_dir / "cron" / "jobs.json"
        if not jobs_p.exists():
            continue
        try:
            data = json.loads(jobs_p.read_text())
        except Exception as e:
            print(prof_dir.name, "ERR", e)
            continue
        jobs = data if isinstance(data, list) else data.get("jobs", [])
        for j in jobs:
            blob = json.dumps({k: j.get(k) for k in ("id", "name", "prompt", "script")})
            if any(n in blob for n in NEEDLES):
                found = True
                print(f"{prof_dir.name} | id={j.get('id')} | name={j.get('name')} | enabled={j.get('enabled')} | script={j.get('script')}")
                print(f"    prompt={str(j.get('prompt') or '')[:300]}")
    if not found:
        print("NO cron job in ANY profile references the DQSH framework")

if __name__ == "__main__":
    main()
