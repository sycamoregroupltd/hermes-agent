#!/usr/bin/env python3
"""Read-only: inspect cron jobs across profiles for DQSH/framework wiring."""
import json
import sys
from pathlib import Path

PROFILES = ["jarvis", "jarvis-os-pm", "trading-devops", "sycode-trading-pm", "trading-data-oracle"]

def main():
    found = False
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
        for j in jobs:
            jid = str(j.get("id") or "")
            script = str(j.get("script") or "")
            name = str(j.get("name") or "")
            if (
                jid == "c7226b0fbbe5"
                or "data-quality-framework" in script
                or "data_quality_framework" in script
                or "run_dqsh" in script
                or "framework" in name.lower()
            ):
                found = True
                print(f"{prof} | id={jid} | name={name} | enabled={j.get('enabled')} | sched={j.get('schedule')}")
                print(f"    script={script}")
                print(f"    prompt={str(j.get('prompt') or '')[:250]}")
    if not found:
        print("NO DQSH/framework cron jobs found in:", PROFILES)

if __name__ == "__main__":
    main()
