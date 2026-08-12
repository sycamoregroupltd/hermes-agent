#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# Stale grok-CLI reaper (2026-07-05, claude-seat, Frank-approved after uplink incident):
# the grok-cli skills cap runs at 30 min (timeout 1800). Any grok CLI process older
# than 60 min is a guardrail violation — a hung run that saturates the uplink with
# retransmits (2026-07-05: 14h-old orphan pushed LAN latency to 400ms). Reap it.
# Also clears systemd-inhibit ghosts left by dead grok runs.
import subprocess, sys

GRACE_SECONDS = 3600
reaped = []

def etime_to_seconds(e):
    d = 0
    if "-" in e:
        d, e = e.split("-", 1)
    parts = [int(x) for x in e.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return int(d) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]

out = subprocess.run(["ps", "-eo", "pid,etimes,args", "--no-headers"],
                     capture_output=True, text=True).stdout
for line in out.splitlines():
    f = line.split(None, 2)
    if len(f) < 3:
        continue
    pid, etimes, args = f[0], int(f[1]), f[2]
    is_grok_run = args.startswith("grok ") or args == "grok"
    is_inhibit = args.startswith("systemd-inhibit") and "who=grok" in args.replace("--who=", "who=")
    if (is_grok_run and etimes > GRACE_SECONDS) or (is_inhibit and etimes > GRACE_SECONDS):
        subprocess.run(["kill", pid])
        reaped.append(f"{pid} ({etimes}s): {args[:90]}")

if reaped:
    print("REAPED stale grok processes (>60min, guardrail violation):")
    for r in reaped:
        print(" ", r)
# silent when clean (no-agent cron: empty stdout = no delivery)
sys.exit(0)
