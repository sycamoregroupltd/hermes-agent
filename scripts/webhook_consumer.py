#!/usr/bin/env python3
"""Webhook-driven signal consumer. Watches for new signal-router entries in n8n_market_data
and processes them in real-time (faster than 5m cron polling).
Runs as systemd service."""
import subprocess, json, time, os, sys
from datetime import datetime

OC = "http://localhost:3001/api/openclaw"
TOKEN = os.environ.get("SYCODE_READ_TOKEN") or os.environ.get("JARVIS_READ_TOKEN")
TRADE_TOKEN = os.environ.get("SYCODE_TRADE_TOKEN") or os.environ.get("JARVIS_TRADE_TOKEN")
DB = ["docker", "exec", "-i", "sycodetrading-supabase-db", "psql", "-U", "postgres", "-d", "postgres"]
STATE_DIR = "/home/frank/.hermes/state"
os.makedirs(STATE_DIR, exist_ok=True)

def db(sql):
    r = subprocess.run(DB + ["-c", sql], capture_output=True, text=True, timeout=15)
    return r.stdout.strip()

def api(method, path, data=None, trade=False):
    tok = TRADE_TOKEN if trade else TOKEN
    if not tok:
        print(f"TOKEN_MISSING: {'SYCODE_TRADE_TOKEN/JARVIS_TRADE_TOKEN' if trade else 'SYCODE_READ_TOKEN/JARVIS_READ_TOKEN'}", flush=True)
        return {}
    cmd = ["curl", "-s", "-X", method, "-H", f"X-Sycode-Token:{tok}", "-H", "Content-Type:application/json",
           "--connect-timeout", "10", "--max-time", "20"]
    if data: cmd += ["-d", json.dumps(data)]
    cmd += [f"{OC}{path}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    return json.loads(r.stdout) if r.stdout else {}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M')}] [WEBHOOK] {msg}", flush=True)

print("[Webhook Consumer] Starting...", flush=True)

# Track processed entries
seen_file = os.path.join(STATE_DIR, "webhook_consumer_seen.txt")
last_seen = set()
if os.path.exists(seen_file):
    with open(seen_file) as f:
        last_seen = set(f.read().strip().split('\n'))

while True:
    try:
        # Poll n8n_market_data for new signal-router or paper-trader entries
        rows = [r for r in db("""
            SELECT id FROM n8n_market_data WHERE source IN ('paper-trader')
            AND captured_at > NOW() - INTERVAL '5 minutes'
            ORDER BY id DESC LIMIT 5;
        """).split('\n') if r]

        for row_id in rows:
            if row_id not in last_seen:
                last_seen.add(row_id)
                log(f"New entry ID={row_id}")

                # If there are signals to check, do it immediately
                signals = api("GET", "/signals/live?limit=3").get("signals", [])
                if signals:
                    sig = signals[0]
                    conf = sig.get("confidence", 0)
                    if conf >= 30:
                        sym = sig.get("symbol", "")
                        direction = sig.get("direction", "")
                        entry = sig.get("entryPrice", 0)
                        stop = entry * 0.97 if direction == "LONG" else entry * 1.03
                        result = api("POST", "/trade/open", {
                            "symbol": sym, "direction": direction,
                            "sizeUsd": 100, "stopLoss": round(stop, 8), "leverage": 1
                        }, trade=True)
                        if "orderId" in result:
                            log(f"⚡ {direction} {sym} @ ${result['fillPrice']:.2f} (webhook-triggered)")
                        else:
                            log(f"⚠️ {sym}: {result.get('error','?')}")

        # Persist seen IDs
        seen_list = list(last_seen)
        with open(seen_file, 'w') as f:
            f.write('\n'.join(seen_list[-100:]))

        time.sleep(15)  # Poll every 15s instead of 5min cron

    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        break
    except Exception as e:
        print(f"Error: {e}", flush=True)
        time.sleep(15)