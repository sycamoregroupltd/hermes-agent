#!/usr/bin/env bash
# cron-doctor: ALERT-ONLY fleet cron health check (2026-06-11).
# Improves on cron-watchdog (which only flagged NEVER-run jobs and read no results).
# This reads each job's LAST-RUN STATUS, classifies the failure, and reports — NO auto-fix.
#
# Classes it recognises (the recurring ones):
#   - script-path     : "Script not found: .../profiles/<P>/scripts/<f>" — script lives in
#                       ~/.hermes/scripts/ but executor wants a real copy in the profile dir.
#                       Fix: cp ~/.hermes/scripts/<f> ~/.hermes/profiles/<P>/scripts/<f>
#   - xai-oauth       : "xAI OAuth state is missing access_token" — the shadow-state bug.
#                       Fix: ~/uaa-rules/known-fixes/xai-oauth-shadow-state.md (promote global OAuth).
#   - other           : surfaced verbatim for human triage.
set -u
python3 <<'PY'
import json, subprocess, re, sys
out = subprocess.run(["hermes","cron","list"], capture_output=True, text=True).stdout
blocks = re.split(r'\n  [0-9a-f]{12}', out)
problems = []
for b in blocks:
    nm = re.search(r'Name:\s*(\S+)', b)
    lr = re.search(r'Last run:[^\n]*?(ok|error:\s*[^\n]+)', b)
    if not (nm and lr): continue
    res = lr.group(1)
    if res == "ok": continue
    name, err = nm.group(1), res
    # Avoid self-perpetuating alerts: this script reads the previous cron-doctor
    # result before the current run can overwrite it, so reporting its own prior
    # failure would refresh the same failure forever. Other watchdogs cover this job.
    if name == "cron-doctor":
        continue
    if "Script not found" in err:
        m = re.search(r'profiles/([^/]+)/scripts/(\S+)', err)
        cls = f"script-path (profile={m.group(1)}, script={m.group(2)} — cp from ~/.hermes/scripts/)" if m else "script-path"
    elif "missing access_token" in err or "xai" in err.lower() and "oauth" in err.lower():
        cls = "xai-oauth shadow-state — see ~/uaa-rules/known-fixes/xai-oauth-shadow-state.md"
    else:
        cls = "other: " + err[:90]
    problems.append((name, cls))

if not problems:
    print("[SILENT] cron-doctor: all cron jobs last-ran OK")
    sys.exit(0)
print(f"⚠️ cron-doctor: {len(problems)} cron job(s) failing:")
for name, cls in problems:
    print(f"  • {name} — {cls}")
print("\n(alert-only — no auto-fix. Apply the indicated fix or run the known-fix doc.)")
sys.exit(1)
PY

