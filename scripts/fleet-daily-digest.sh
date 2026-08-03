#!/usr/bin/env bash
# fleet-daily-digest.sh — the six numbers Frank actually needs, once a day, to his phone.
# Created 2026-08-02 after the right-sizing analysis found: 194 cards waiting on Frank
# and EVERY escalation path either disabled or delivering to `local` (a file nobody reads).
#
# DESIGN (deliberate):
#  - ALWAYS emits output. This is a no-agent cron delivered to telegram, so the message
#    arriving IS the liveness proof. If it stops arriving, that silence is the alarm.
#    (Every other monitor here is silent-when-clean, which is exactly how ~20 of them
#    died unnoticed for 22 days.)
#  - Six numbers only. A dashboard nobody reads is the failure mode we are escaping.
#  - Pure sqlite/read-only. No agent, no provider dependency — it must work during a 429.
set -u
export PATH="/home/frank/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

python3 - <<'PY'
import sqlite3, glob, datetime, os, json

def q(db, sql, args=()):
    try:
        c = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        n = c.execute(sql, args).fetchone()[0]; c.close(); return n
    except Exception:
        return 0

boards = glob.glob('/home/frank/.hermes/kanban/boards/*/kanban.db')
DAY = 86400
done = crash = spawn = created = 0
openc = blocked = frank = 0
for db in boards:
    done   += q(db, "SELECT COUNT(*) FROM task_events WHERE kind='completed' AND created_at>strftime('%s','now')-?", (DAY,))
    crash  += q(db, "SELECT COUNT(*) FROM task_events WHERE kind='crashed'   AND created_at>strftime('%s','now')-?", (DAY,))
    spawn  += q(db, "SELECT COUNT(*) FROM task_events WHERE kind='spawned'   AND created_at>strftime('%s','now')-?", (DAY,))
    created+= q(db, "SELECT COUNT(*) FROM tasks WHERE created_at>strftime('%s','now')-?", (DAY,))
    openc  += q(db, "SELECT COUNT(*) FROM tasks WHERE status IN ('todo','ready','blocked','triage','scheduled')")
    blocked+= q(db, "SELECT COUNT(*) FROM tasks WHERE status='blocked'")
    frank  += q(db, "SELECT COUNT(*) FROM tasks WHERE status='blocked' AND block_kind IN ('needs_input','frank_gate')")

crash_pct = round(100*crash/spawn) if spawn else 0
net = created - done

# dead scheduled jobs: enabled, but sitting in a store whose ticker is stale/absent
dead = 0
for p in glob.glob('/home/frank/.hermes/profiles/*/cron/jobs.json'):
    prof = p.split('/')[-3]
    hb = f'/home/frank/.hermes/profiles/{prof}/cron/ticker_heartbeat'
    fresh = os.path.exists(hb) and (datetime.datetime.now().timestamp() - os.path.getmtime(hb)) < 900
    if fresh: continue
    try:
        d = json.load(open(p)); j = d if isinstance(d, list) else d.get('jobs', d)
        if isinstance(j, dict): j = list(j.values())
        dead += sum(1 for x in j if isinstance(x, dict) and x.get('enabled'))
    except Exception:
        pass

arrow = "UP" if net > 0 else ("down" if net < 0 else "flat")
print(f"FLEET DAILY — {datetime.datetime.now(datetime.timezone.utc):%d %b %H:%M}Z")
print(f"1. Done (24h):      {done}")
print(f"2. Crash rate:      {crash_pct}%  ({crash} of {spawn} spawns)   target <5%")
print(f"3. Queue trend:     {net:+d}  ({created} created vs {done} done)  {arrow}   target 0 or negative")
print(f"4. Open / blocked:  {openc} / {blocked}")
print(f"5. WAITING ON YOU:  {frank}")
print(f"6. Dead scheduled:  {dead} enabled jobs in stores that do not tick   target 0")
print("")
print("If this message stops arriving, that silence is the alarm.")
PY
