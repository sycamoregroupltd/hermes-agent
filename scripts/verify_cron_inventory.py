#!/usr/bin/env python3
"""Read-only: full enabled cron inventory for the two DQSH-adjacent profiles."""
import json
import sys
from pathlib import Path

PROFILES = ["jarvis-os-pm", "trading-devops", "trading-data-oracle", "jarvis"]

def main():
    for prof in PROFILES:
        p = Path(f"/home/frank/.hermes/profiles/{prof}/cron/jobs.json")
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
        except Exception as e:
            print(prof, "ERR", e)
            continue
        jobs = data if isinstance(data, list) else data.get("jobs", [])
        print(f"===== {prof} ({len(jobs)} jobs) =====")
        for j in jobs:
            enabled = j.get("enabled")
            if enabled is False:
                continue
            print(
                f"  {j.get('id')} | {j.get('name')} | {j.get('schedule')} | script={j.get('script')}"
            )

if __name__ == "__main__":
    main()
