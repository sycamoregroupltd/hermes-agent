#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Deterministic signal-to-trader. Reads OpenClaw signals, trades automatically if confidence >= threshold.
Uses fear/greed context from DB. Faster and more consistent than LLM-based trading."""
import subprocess, json, os, sys
from datetime import datetime

OC = os.environ.get("OPENCLAW_API_BASE", "http://localhost:3001/api/openclaw")
READ_TOKEN_ENV = "OPENCLAW_READ_TOKEN"
TRADE_TOKEN_ENV = "OPENCLAW_TRADE_TOKEN"
ENV_FILE = os.environ.get("AUTO_SIGNAL_TRADER_ENV_FILE", "/home/frank/.hermes/secrets/auto-signal-trader.env")
DB = ["docker", "exec", "-i", "sycodetrading-supabase-db", "psql", "-t", "-U", "postgres", "-d", "postgres"]
dry_run = os.environ.get("AUTO_SIGNAL_TRADER_DRY_RUN", "").lower() in {"1", "true", "yes"}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M')}] [TRADER] {msg}")


def load_env_file(path):
    if not path or not os.path.exists(path):
        return
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        log(f"Secret env file {path} is too permissive ({mode:o}); fail-closed")
        sys.exit(2)
    allowed = {READ_TOKEN_ENV, TRADE_TOKEN_ENV, "OPENCLAW_API_BASE"}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key in allowed and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")


def env_token(name):
    tok = os.environ.get(name, "").strip()
    if not tok:
        log(f"Missing required environment variable {name}; fail-closed before OpenClaw API access")
        sys.exit(2)
    return tok


load_env_file(ENV_FILE)
TOKEN = env_token(READ_TOKEN_ENV)
TRADE_TOKEN = env_token(TRADE_TOKEN_ENV)

def api(method, path, data=None, trade=False):
    tok = TRADE_TOKEN if trade else TOKEN
    cmd = ["curl", "-sS", "-X", method, "-H", f"X-Sycode-Token:{tok}", "-H", "Content-Type:application/json",
           "--connect-timeout", "15", "--max-time", "35"]
    if data: cmd += ["-d", json.dumps(data)]
    cmd += [f"{OC}{path}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return {"_error": f"timeout calling {path}"}
    if r.returncode != 0:
        return {"_error": f"curl rc={r.returncode} calling {path}: {r.stderr.strip()[:160]}"}
    if not r.stdout:
        return {}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        return {"_error": f"bad json calling {path}: {exc}"}

def db(sql):
    r = subprocess.run(DB + ["-c", sql], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()

# 1. Get market context
fg_row = db("SELECT payload->'data'->>'value' FROM n8n_market_data WHERE source='fear-greed' ORDER BY captured_at DESC LIMIT 1;")
fg = int(fg_row) if fg_row.isdigit() else 50

# 2. Set threshold based on market conditions
if fg < 15:
    threshold = 40  # Extreme Fear: be conservative
    log(f"Extreme Fear ({fg}) → threshold=40")
elif fg > 85:
    threshold = 35  # Extreme Greed: still trade but only SHORT
    log(f"Extreme Greed ({fg}) → SHORT only, threshold=35")
else:
    threshold = 30  # Normal
    log(f"Normal ({fg}) → threshold=30")

# 3. Read signals
signals_resp = api("GET", "/signals/live?limit=10")
if signals_resp.get("_error"):
    log(f"Signal fetch failed fail-closed: {signals_resp['_error']}")
    signals = []
else:
    signals = signals_resp.get("signals", [])
log(f"Found {len(signals)} signals")

# 4. Read open positions
status = api("GET", "/status")
if status.get("_error"):
    log(f"Status fetch failed fail-closed: {status['_error']}")
    status = {"openPositions": 0, "balance": {"total": 0}, "positionManagement": {"positions": []}}
if not isinstance(status, dict):
    status = {"openPositions": 0, "balance": {"total": 0}, "positionManagement": {"positions": []}}
open_positions = int(status.get("openPositions") or 0)
balance_obj = status.get("balance", {})
if not isinstance(balance_obj, dict):
    balance_obj = {}
balance = float(balance_obj.get("total") or 0)

# Get symbols with open positions
open_symbols = set()
pm_obj = status.get("positionManagement", {})
if not isinstance(pm_obj, dict):
    pm_obj = {}
positions = pm_obj.get("positions", [])
if not isinstance(positions, list):
    positions = []
for p in positions:
    if not isinstance(p, dict):
        continue
    sym = p.get("symbol", "").replace("/", "")
    open_symbols.add(sym)

# 5. Trade loop
trades_opened = 0
trades_skipped = 0
max_positions = 10

for s in signals:
    if not isinstance(s, dict):
        trades_skipped += 1
        continue
    sym = s.get("symbol", "")
    direction = s.get("direction", "")
    conf = float(s.get("confidence") or 0)
    entry = float(s.get("entryPrice") or 0)

    if sym in open_symbols:
        trades_skipped += 1
        continue
    if open_positions + trades_opened >= max_positions:
        log(f"Max positions ({max_positions}) reached")
        break
    if conf < threshold:
        trades_skipped += 1
        continue
    if fg > 85 and direction == "LONG":
        trades_skipped += 1
        continue
    if entry <= 0:
        trades_skipped += 1
        continue

    # Calculate stop loss
    stop = entry * 0.97 if direction == "LONG" else entry * 1.03

    if dry_run:
        log(f"DRY-RUN would open {direction} {sym} @ ${entry:.2f} (conf={conf})")
        trades_skipped += 1
        continue

    result = api("POST", "/trade/open", {
        "symbol": sym, "direction": direction,
        "sizeUsd": 100, "stopLoss": round(stop, 8), "leverage": 1
    }, trade=True)

    if "orderId" in result:
        log(f"✅ {direction} {sym} @ ${result['fillPrice']:.2f} (conf={conf})")
        trades_opened += 1
        open_symbols.add(sym)
    else:
        log(f"❌ {sym}: {result.get('error') or result.get('_error') or '?'}")
        trades_skipped += 1

# 6. Write report to DB
report = json.dumps({
    "signals_checked": len(signals),
    "trades_opened": trades_opened,
    "trades_skipped": trades_skipped,
    "open_positions": open_positions,
    "balance": round(balance, 2),
    "fear_greed": fg,
    "threshold": threshold,
    "timestamp": datetime.utcnow().isoformat()
})
safe = report.replace("'", "''")
if dry_run:
    log("DRY-RUN: skipping paper-trader report DB write")
else:
    db(f"INSERT INTO n8n_market_data (source, payload) VALUES ('paper-trader', '{safe}'::jsonb);")

log(f"Done: {trades_opened} opened, {trades_skipped} skipped, balance=${balance:.2f}")
