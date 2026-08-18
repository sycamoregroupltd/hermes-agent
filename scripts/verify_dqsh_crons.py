#!/usr/bin/env python3
"""Read-only: list cron jobs referencing DQSH/CDPDS/DIAR/data-quality across profiles."""
import json
import sys
from pathlib import Path

PROFILES = ["trading-devops", "sycode-trading-pm", "jarvis"]
KEYS = ["dqsh", "cdpds", "diar", "data_quality", "data-integrity", "integrity", "data quality"]

def main():
    found = False
    for prof in PROFILES:
        p = Path(f"/home/frank/.hermes/profiles/{prof}/cron/jobs.json")
        if not p.exists():
            print(prof, "NO jobs.json")
            continue
        try:
            data = json.loads(p.read_text())
        except Exception as e:
            print(prof, "ERR", e)
            continue
        jobs = data if isinstance(data, list) else data.get("jobs", [])
        for j in jobs:
            name = str(j.get("name") or "")
            prompt = str(j.get("prompt") or "")
            script = str(j.get("script") or "")
            blob = (name + " " + prompt + " " + script).lower()
            if any(k in blob for k in KEYS):
                found = True
                print(
                    f"{prof} | id={j.get('id')} | name={name} | sched={j.get('schedule')} | "
                    f"enabled={j.get('enabled')} | script={script[:100]}"
                )
    if not found:
        print("NO matching cron jobs found in:", PROFILES)

if __name__ == "__main__":
    main()
