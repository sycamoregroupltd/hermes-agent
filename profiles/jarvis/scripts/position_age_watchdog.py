#!/usr/bin/env python3
"""Position-age watchdog (no_agent mode).
Closes Jarvis paper positions open > 4h. Flags stale managed_positions.
Silent exit if nothing to do — $0 cost on idle ticks."""

import subprocess, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

PGPASSWORD = os.environ.get("POSTGRES_PASSWORD") or os.environ.get("PGPASSWORD") or ""
DB = ["docker", "exec", "-e", f"PGPASSWORD={PGPASSWORD}", "sycodetrading-supabase-db",
      "psql", "-U", "postgres", "-d", "postgres", "-t", "-A"]
DB_JARVIS = ["docker", "exec", "-e", f"PGPASSWORD={PGPASSWORD}", "sycodetrading-supabase-db",
             "psql", "-U", "postgres", "-d", "sycodetrading", "-t", "-A"]


def db(sql):
    r = subprocess.run(DB + ["-c", sql], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()

def dbj(sql):
    r = subprocess.run(DB_JARVIS + ["-c", sql], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()

def notify(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] [WATCHDOG] {msg}", flush=True)


REMIND_SECONDS = int(os.getenv("POSITION_AGE_REMIND_SECONDS", str(24 * 3600)))
STATE = Path(os.getenv("POSITION_AGE_STATE", "/home/frank/.hermes/profiles/jarvis/cron/state/position_age_watchdog.first_seen.json"))


def read_state():
    try:
        state = json.loads(STATE.read_text())
    except Exception:
        return {}
    if not isinstance(state, dict):
        return {}
    # Backward-compatible migration from the prior single-fingerprint shape:
    # {"fingerprint": "managed-stale:<detail>", ...}
    # New shape is per alert label so simultaneous conditions do not overwrite
    # each other's dedup state:
    # {"managed-stale": {"fingerprint": ..., ...}, "jarvis-open": {...}}
    if "fingerprint" in state:
        fingerprint = str(state.get("fingerprint") or "")
        label = fingerprint.split(":", 1)[0] if ":" in fingerprint else "legacy"
        return {label: state}
    return state


def write_state(payload):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(f".{STATE.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, STATE)


def clear_state():
    try:
        STATE.unlink()
    except FileNotFoundError:
        pass


def should_emit(label, detail):
    now = int(time.time())
    state = read_state()
    key = f"{label}:{detail}"
    raw_entry = state.get(label)
    entry = raw_entry if isinstance(raw_entry, dict) else {}
    if entry.get("fingerprint") != key:
        state[label] = {"fingerprint": key, "first_seen": now, "last_alert": now, "last_seen": now}
        write_state(state)
        return True
    last_alert = int(entry.get("last_alert") or 0)
    if now - last_alert >= REMIND_SECONDS:
        state[label] = {**entry, "last_alert": now, "last_seen": now}
        write_state(state)
        return True
    state[label] = {**entry, "last_seen": now}
    write_state(state)
    return False


def prune_state(active_labels):
    state = read_state()
    if not state:
        return
    pruned = {label: payload for label, payload in state.items() if label in active_labels}
    if pruned:
        write_state(pruned)
    else:
        clear_state()


active_dedup_labels = set()

# ── 1. Jarvis paper positions open > 4 hours ──
now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
cutoff_4h = now_ms - (4 * 3600 * 1000)

stale = dbj(f"""
SELECT jarvis_id, symbol, direction,
  round(entry_price::numeric, 4) as entry,
  round(({now_ms} - open_time)::numeric / 3600000, 1) as age_hours
FROM jarvis_positions
WHERE status = 'open' AND open_time < {cutoff_4h}
ORDER BY open_time
LIMIT 20;
""")

closed = 0
if stale:
    lines = [l for l in stale.split('\n') if l.strip()]
    count = len(lines)
    notify(f"AUTO-CLOSE: {count} Jarvis paper positions open >4h")
    close_now = int(datetime.now(timezone.utc).timestamp() * 1000)
    for l in lines[:20]:
        parts = l.split('|')
        if len(parts) >= 4:
            jid = parts[0]
            # Close at entry price (zero P&L) — paper only, safe cleanup
            sql = f"""
            UPDATE jarvis_positions
            SET status = 'closed',
                close_time = {close_now},
                close_reason = 'watchdog-auto-close-stale',
                realized_pnl = 0
            WHERE jarvis_id = '{jid}' AND status = 'open';
            """
            dbj(sql)
            closed += 1
            notify(f"  CLOSED {parts[2]} {parts[1]} @ ${parts[3]} ({parts[4]}h old)")
    print(f"JARVIS-AUTO-CLOSED:{closed}")

# ── 2. Managed positions open > 7 days ──
cutoff_7d = now_ms - (7 * 24 * 3600 * 1000)

stale_managed = db(f"""
SELECT strategy_name, symbol, direction,
  round(entry_price::numeric, 4) as entry,
  round((extract(epoch from now()) * 1000 - extract(epoch from opened_at) * 1000)::numeric / 86400000, 1) as age_days
FROM managed_positions
WHERE status = 'open' AND opened_at < now() - interval '7 days'
ORDER BY opened_at
LIMIT 10;
""")

if stale_managed:
    active_dedup_labels.add("managed-stale")
    lines = stale_managed.split('\n')
    count = len([l for l in lines if l.strip()])
    detail = "\n".join("|".join(l.split("|")[:4]) for l in lines[:5])
    if should_emit("managed-stale", detail):
        notify(f"STALE: {count} managed positions open >7d")
        for l in lines[:5]:
            parts = l.split('|')
            if len(parts) >= 4:
                notify(f"  {parts[0]} {parts[2]} {parts[1]} @ ${parts[3]} ({parts[4]}d)")
        print(f"MANAGED-STALE:{count}")
        for l in lines[:5]:
            parts = l.split('|')
            if len(parts) >= 4:
                print(f"  {parts[0]} {parts[2]} {parts[1]} @ ${parts[3]} ({parts[4]}d)")

# ── 3. Total Jarvis open count with trend ──
total_open = dbj("SELECT count(*) FROM jarvis_positions WHERE status = 'open';")
total_open = int(total_open) if total_open.isdigit() else 0
if total_open > 6000:
    active_dedup_labels.add("jarvis-open")
    if should_emit("jarvis-open", "over-6000"):
        print(f"JARVIS-OPEN:{total_open} — growing, was 5,311 last check")
        notify(f"Jarvis positions growing: {total_open} open (was 5,311)")

# Silent exit if nothing to report — $0 cost
if not stale and not stale_managed and total_open < 6000:
    clear_state()
    sys.exit(0)

prune_state(active_dedup_labels)
