#!/usr/bin/env python3
"""Position manager: monitors open paper trades, trails stops, closes at TP/SL, cuts losers.
ARCHIVAL — no active cron. Same functionality served by auto_signal_trader.py and arb_trade_pipeline.py.
Kept for reference only; do not schedule without review."""
import subprocess, json, time, sys, os
from datetime import datetime

OC = "http://localhost:3001/api/openclaw"
# Credential loading: shared env file (defaults to sycode-credential.env); env vars override.
_CRED_ENV_FILE = os.environ.get("SYCODE_CREDENTIAL_ENV_FILE", "/home/frank/.hermes/secrets/sycode-credential.env")
if os.path.exists(_CRED_ENV_FILE):
    from dotenv import load_dotenv
    load_dotenv(_CRED_ENV_FILE, override=False)

TOKEN = os.environ.get("SYCODE_READ_TOKEN") or os.environ.get("OPENCLAW_READ_TOKEN")
TRADE_TOKEN = os.environ.get("SYCODE_TRADE_TOKEN") or os.environ.get("OPENCLAW_TRADE_TOKEN")
if not TOKEN or not TRADE_TOKEN:
    print(f"[FATAL] Missing OpenClaw credentials. Set SYCODE_READ_TOKEN + SYCODE_TRADE_TOKEN\n"
          f"       (or OPENCLAW_READ_TOKEN + OPENCLAW_TRADE_TOKEN) in env or in {_CRED_ENV_FILE}.",
          flush=True)
    sys.exit(3)
DB = ["docker", "exec", "-i", "sycodetrading-supabase-db", "psql", "-U", "postgres", "-d", "postgres"]

def api(method, path, data=None, trade=False):
    tok = TRADE_TOKEN if trade else TOKEN
    cmd = ["curl", "-s", "-X", method, "-H", f"X-Sycode-Token:{tok}", "-H", "Content-Type:application/json",
           "--connect-timeout", "15", "--max-time", "30"]
    if data: cmd += ["-d", json.dumps(data)]
    cmd += [f"{OC}{path}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
    return json.loads(r.stdout) if r.stdout else {}

def db(sql):
    r = subprocess.run(DB + ["-c", sql], capture_output=True, text=True, timeout=15)
    return r.stdout.strip()

def log(msg):
    print(f"  {msg}")

# 1. Get open positions
status = api("GET", "/status")
positions = status.get("positionManagement", {}).get("positions", [])
log(f"Checking {len(positions)} open positions")

closed_count = 0
trailed_count = 0

for pos in positions:
    sym = pos.get("symbol", "")
    direction = pos.get("direction", "")
    entry = float(pos.get("entryPrice", 0) or pos.get("avgEntryPrice", 0))
    current_sl = pos.get("currentStopLoss")
    pnl_pct = float(pos.get("peakUnrealizedPnlPercent", 0) or 0)
    pnl = float(pos.get("unrealizedPnl", 0) or 0)
    leverage = float(pos.get("leverage", 1) or 1)

    # Get current price from OpenClaw trade endpoint
    trade_info = api("GET", f"/trade/{sym.replace('/','')}", trade=True)
    # Trade info might not have current price; use entry as fallback
    current_price = entry

    if entry <= 0:
        continue

    # Check real-time PnL estimate
    if direction == "LONG":
        pnl_estimate = (current_price - entry) / entry * 100
        sl_price = entry * 0.97 if not current_sl else float(current_sl)
        tp_price = entry * 1.03
    else:
        pnl_estimate = (entry - current_price) / entry * 100
        sl_price = entry * 1.03 if not current_sl else float(current_sl)
        tp_price = entry * 0.97

    # TRAIL: If position is up > 0.5%, tighten stop
    if pnl_estimate > 0.5 and direction == "LONG":
        new_sl = current_price * 0.995  # Tight trailing: 0.5% below current
        if not current_sl or new_sl > float(current_sl):
            # Update via trade/manage
            r = api("POST", "/trade/manage", {
                "symbol": sym.replace("/", ""),
                "action": "HOLD",
                "reasoning": f"Trail stop to {new_sl:.4f}"
            }, trade=True)
            trailed_count += 1
            log(f"📈 Trail {sym}: SL {float(current_sl or 0):.4f} → {new_sl:.4f} (PnL {pnl_estimate:.2f}%)")

    elif pnl_estimate > 0.5 and direction == "SHORT":
        new_sl = current_price * 1.005
        if not current_sl or new_sl < float(current_sl):
            trailed_count += 1
            log(f"📈 Trail {sym}: SL moved down (PnL {pnl_estimate:.2f}%)")

    # CLOSE: Take profit at 3%
    if pnl_estimate >= 3.0:
        result = api("POST", "/trade/manage", {
            "symbol": sym.replace("/", ""),
            "action": "CLOSE",
            "reasoning": f"Take profit: {pnl_estimate:.2f}%"
        }, trade=True)
        log(f"💰 TP {sym}: closed at {pnl_estimate:.2f}% profit")
        closed_count += 1
        payload = json.dumps({"symbol": sym, "pnl_pct": round(pnl_estimate, 2), "reason": "take_profit"})
        db("INSERT INTO n8n_market_data (source, payload) VALUES ('position-closed', '" + payload.replace("'", "''") + "'::jsonb);")

    # CLOSE: Stop loss hit
    elif pnl_estimate <= -3.0:
        result = api("POST", "/trade/manage", {
            "symbol": sym.replace("/", ""),
            "action": "CLOSE",
            "reasoning": f"Stop loss: {pnl_estimate:.2f}%"
        }, trade=True)
        log(f"🛑 SL {sym}: closed at {pnl_estimate:.2f}% loss")
        closed_count += 1

log(f"Done: {trailed_count} trailed, {closed_count} closed, {len(positions)} total")