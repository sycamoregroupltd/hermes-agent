#!/usr/bin/env python3
"""fleet-analyst triage monitor snapshot for sycode-trading board (task t_671747f7).

HARDENED 2026-08-12 (kanban t_0e0bcda9): no-agent cron script.

Why: the previous T6-monitoring cron was an LLM-driven job (no_agent=false)
whose prompt was a python heredoc. On 2026-08-12 16:29Z the Nous provider had
no access token ("No access token found for Nous Portal login") so the LLM
session could not start and the tick died before the snapshot ran -> a 12h CSV
gap (09:27Z -> 21:30Z). A retry would not have helped (the token was absent,
every retry fails until re-auth).

This script runs with NO LLM and NO provider token: pure python3 + read-only
sqlite. The cron job is now `no_agent=true`, so the scheduler executes this
script directly - immune to Nous auth hiccups and to gateway final-cleanup
subprocess teardown (no tool subprocess exists).

Hardening features (CSV row shape UNCHANGED so the series stays consistent):
- GAP_DETECTED warning when the last CSV sample is older than ~1.9x the 6h
  cadence (i.e. a tick was missed). Flags the gap; never fabricates rows.
- --dry-run: compute + print the row WITHOUT appending. Used for the
  auth-hiccup simulation / CI (proves the script needs no Hermes/Nous env).

Canonical home: /home/frank/.hermes/scripts/sycode-triage-snapshot.py
Supersedes:     /home/frank/.hermes/var/log/fleet-metrics/sycode-triage-snapshot.py
"""
import sqlite3, time, json, os, sys, datetime

DB = '/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db'
# SYCODE_TRIAGE_CSV overrides the output path (used by the auth-hiccup / gap
# simulation tests so they never pollute the real measurement series).
CSV = os.environ.get('SYCODE_TRIAGE_CSV',
                     '/home/frank/.hermes/var/log/fleet-metrics/sycode-triage-monitor.csv')
SCHEDULE_MINUTES = 360  # T6 cadence
# Warn when the previous sample is older than ~1.9x the cadence (any missed 6h tick)
GAP_THRESHOLD_SECONDS = int(SCHEDULE_MINUTES * 60 * 1.9)
DRY_RUN = '--dry-run' in sys.argv

NOW = int(time.time())
ts = time.strftime('%Y-%m-%dT%H:%M:%S%z', time.gmtime(NOW))

con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
cur = con.cursor()

def age_bucket(ca):
    h = (NOW - ca) / 3600.0
    if h <= 24:
        return '0-24h'
    if h <= 48:
        return '24-48h'
    if h <= 168:
        return '48h-7d'
    if h <= 336:
        return '7d-14d'
    return '14d+'

rows = cur.execute("SELECT created_at FROM tasks WHERE status='triage'").fetchall()
buckets = {'0-24h': 0, '24-48h': 0, '48h-7d': 0, '7d-14d': 0, '14d+': 0}
for (ca,) in rows:
    buckets[age_bucket(ca)] += 1

per_status = dict(cur.execute(
    "SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY 2 DESC"
).fetchall())

vis = cur.execute(
    "SELECT id,title,status,assignee FROM tasks WHERE title LIKE 'PM TRIAGE VISIBILITY:%' ORDER BY created_at DESC LIMIT 5"
).fetchall()

pm_6h = cur.execute(
    "SELECT COUNT(*) FROM task_comments WHERE author LIKE '%sycode-trading-pm%' AND created_at >= ?",
    (NOW - 6 * 3600,)
).fetchone()[0]

arr_24 = cur.execute(
    "SELECT COUNT(*) FROM tasks WHERE created_at >= ? AND status IN ('triage','blocked','todo','ready','running','scheduled')",
    (NOW - 86400,)
).fetchone()[0]

line = (f"{ts},{len(rows)},{buckets['0-24h']},{buckets['24-48h']},"
        f"{buckets['48h-7d']},{buckets['7d-14d']},{buckets['14d+']},"
        f"{json.dumps(per_status)},{len(vis)},{pm_6h},{arr_24}\n")

# --- Gap detection: flag missed samples without fabricating rows ---
last_sample_ts = None
if os.path.exists(CSV):
    try:
        last_line = None
        with open(CSV) as f:
            for last_line in f:
                pass
        if last_line and last_line.strip():
            last_sample_ts = last_line.split(',')[0]
            last_epoch = datetime.datetime.strptime(last_sample_ts, '%Y-%m-%dT%H:%M:%S%z').timestamp()
            gap = NOW - last_epoch
            if gap > GAP_THRESHOLD_SECONDS:
                print(f"GAP_DETECTED|last_sample={last_sample_ts}|gap_seconds={int(gap)}|now={ts}|"
                      f"action=monitoring_hiccup_recovered_on_this_tick", flush=True)
    except Exception as e:
        print(f"GAP_CHECK_ERROR|{e}", flush=True)

print(f"SNAPSHOT | {ts} | triage={len(rows)} | buckets={json.dumps(buckets)} | "
      f"statuses={json.dumps(per_status)} | visible={len(vis)} | pm_6h={pm_6h} | arrivals_24h={arr_24}", flush=True)

if DRY_RUN:
    print(f"DRY_RUN | would_append | {line.rstrip()}", flush=True)
else:
    os.makedirs(os.path.dirname(CSV), exist_ok=True)
    with open(CSV, 'a') as f:
        f.write(line)

print("--- CSV tail ---")
with open(CSV) as f:
    lines = f.readlines()
print(f"total_lines={len(lines)}")
for l in lines[-5:]:
    print(l.strip())
con.close()
