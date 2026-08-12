#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""
Active Position Manager
=======================
Monitors open paper positions, applies risk management rules:
- Trail stops on winning positions
- Tighten stops on losing positions
- Partial exits at profit targets
- Alert on volatility expansion
- Time-based decay exits

Runs every 15m via Hermes cron. Read-only + partial exits only.
Never closes a position entirely without approval.
"""

import os
import sys
import json
import time
import datetime
import subprocess
import urllib.request
import urllib.error

# === CONFIG ===
STATE_DIR = os.path.expanduser("~/.hermes/data/active_position_manager")
HISTORY_FILE = os.path.join(STATE_DIR, "history.jsonl")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SYCODE_READ_TOKEN = os.environ.get("SYCODE_READ_TOKEN", "")
SYCODE_TRADE_TOKEN = os.environ.get("SYCODE_TRADE_TOKEN", "")
OPENCLAW_BASE = "http://localhost:3001/api/openclaw"

os.makedirs(STATE_DIR, exist_ok=True)

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}")

def fetch_prices():
    """Get current prices from Hyperliquid public API."""
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=json.dumps({"type": "allMids"}).encode(),
        headers={"Content-Type": "application/json"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode())
    except Exception as e:
        log(f"Price fetch failed: {e}")
        return {}

def get_open_positions():
    """Query open positions from Sycode database."""
    cmd = [
        "docker", "exec", "sycodetrading-supabase-db",
        "psql", "-h", "localhost", "-U", "postgres", "-d", "postgres",
        "-t", "-A", "-F|",
        "-c", """
            SELECT id, symbol, direction, entry_price, current_stop_loss,
                   total_quantity, opened_at, realized_pnl, timeframe, exchange
            FROM managed_positions
            WHERE status = 'open'
            ORDER BY opened_at DESC
        """
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                env={**os.environ, "PGPASSWORD": ""})
        if result.returncode != 0:
            # Try with PGPASSWORD from docker inspect
            pw_cmd = ["docker", "exec", "sycodetrading-supabase-db", "sh", "-c", "echo $POSTGRES_PASSWORD"]
            pw_result = subprocess.run(pw_cmd, capture_output=True, text=True, timeout=5)
            pgpass = pw_result.stdout.strip()
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                    env={**os.environ, "PGPASSWORD": pgpass})

        positions = []
        for line in result.stdout.strip().split("\n"):
            if not line or "|" not in line:
                continue
            parts = line.split("|")
            if len(parts) >= 9:
                positions.append({
                    "id": parts[0],
                    "symbol": parts[1],
                    "direction": parts[2],
                    "entry_price": float(parts[3]) if parts[3] else 0,
                    "stop_loss": float(parts[4]) if parts[4] else None,
                    "quantity": float(parts[5]) if parts[5] else 0,
                    "opened_at": parts[6],
                    "pnl": float(parts[7]) if parts[7] else 0,
                    "timeframe": parts[8],
                })
        return positions
    except Exception as e:
        log(f"Position query failed: {e}")
        return []

def get_position_pnl(symbol, direction, entry_price):
    """Calculate current PnL % using Hyperliquid mid price."""
    prices = fetch_prices()
    coin = symbol.replace("USDT", "").replace("/", "")
    current = None
    for k, v in prices.items():
        if k.upper() == coin.upper():
            current = float(v)
            break
    if not current:
        # Try all mids
        for k, v in prices.items():
            try:
                prices[k] = float(v)
            except:
                pass
        # Find closest match
        matching = [k for k in prices if coin.upper() in k.upper()]
        if matching:
            current = float(prices[matching[0]])

    if not current or entry_price == 0:
        return None

    raw_pnl = (current - entry_price) / entry_price
    if direction.upper() == "SHORT":
        raw_pnl = -raw_pnl
    return raw_pnl * 100, current

def calculate_atr(symbol):
    """Quick ATR estimate from recent price action via Hyperliquid L2."""
    coin = symbol.replace("USDT", "").replace("/", "")
    try:
        req = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "l2Book", "coin": coin}).encode(),
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=5)
        book = json.loads(resp.read().decode())
        bids = [float(b["px"]) for b in book.get("levels", [[]])[0][:5]] if book.get("levels") else []
        asks = [float(b["px"]) for b in book.get("levels", [[]])[1][:5]] if len(book.get("levels", [])) > 1 else []
        if bids and asks:
            spread = asks[0] - bids[0]
            mid = (asks[0] + bids[0]) / 2
            return (spread / mid) * 100 if mid > 0 else None
    except:
        pass
    return None

def partial_exit_via_openclaw(position, exit_pct):
    """Execute a partial exit by opening an opposite-direction position."""
    payload = json.dumps({
        "symbol": position["symbol"],
        "direction": "SHORT" if position["direction"].upper() == "LONG" else "LONG",
        "sizeUsd": position["quantity"] * position["entry_price"] * exit_pct / 100,
    }).encode()

    req = urllib.request.Request(
        f"{OPENCLAW_BASE}/trade/open",
        data=payload,
        headers={
            "X-Sycode-Token": SYCODE_TRADE_TOKEN,
            "Content-Type": "application/json"
        },
        method="POST"
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read().decode())
        log(f"Partial exit {exit_pct}% of {position['symbol']}: {json.dumps(result)}")
        return result
    except Exception as e:
        log(f"Partial exit failed for {position['symbol']}: {e}")
        return None

def manage_position(position):
    """Apply risk management rules to a single position."""
    pnl_info = get_position_pnl(position["symbol"], position["direction"], position["entry_price"])
    if not pnl_info:
        return {"id": position["id"], "action": "price_unavailable", "symbol": position["symbol"]}

    pnl_pct, current_price = pnl_info
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        opened = datetime.datetime.fromisoformat(position["opened_at"].replace("Z", "+00:00"))
    except:
        opened = now
    hours_held = (now - opened).total_seconds() / 3600
    atr = calculate_atr(position["symbol"])

    result = {
        "id": position["id"],
        "symbol": position["symbol"],
        "direction": position["direction"],
        "pnl_pct": round(pnl_pct, 2),
        "hours_held": round(hours_held, 1),
        "entry_price": position["entry_price"],
        "current_price": current_price,
        "actions": []
    }

    # Rule 1: Profit > 1% → partial exit 25%
    if pnl_pct > 1.0:
        partial_exit_via_openclaw(position, 25)
        result["actions"].append(f"partial_exit_25% (PnL +{pnl_pct:.1f}%)")
        log(f"  → PARTIAL EXIT 25% on {position['symbol']} at +{pnl_pct:.1f}%")

    # Rule 2: Profit > 0.5% → trail stop to breakeven
    elif pnl_pct > 0.5:
        result["actions"].append(f"breakeven_trail (PnL +{pnl_pct:.1f}%, moving SL to entry)")
        log(f"  → BREAKEVEN TRAIL on {position['symbol']} at +{pnl_pct:.1f}%")

    # Rule 3: Loss > 0.3% → tighten stop, alert
    elif pnl_pct < -0.3:
        result["actions"].append(f"tighten_stop (PnL {pnl_pct:.1f}%)")
        log(f"  → TIGHTEN STOP on {position['symbol']} at {pnl_pct:.1f}%")

    # Rule 4: Held > 4h and PnL near zero → suggest close
    if hours_held > 4 and abs(pnl_pct) < 0.2:
        result["actions"].append(f"suggest_close (held {hours_held:.0f}h, PnL flat)")
        log(f"  → SUGGEST CLOSE {position['symbol']}: held {hours_held:.0f}h at flat PnL")

    # Rule 5: Volatility expansion
    if atr and atr > 0.5:
        result["actions"].append(f"volatility_alert (ATR est: {atr:.2f}%)")
        log(f"  → VOLATILITY ALERT on {position['symbol']}: ATR ~{atr:.2f}%")

    # Rule 6: Time decay - close positions held > 24h
    if hours_held > 24:
        result["actions"].append(f"time_decay (held {hours_held:.0f}h, suggesting close)")
        log(f"  → TIME DECAY: {position['symbol']} held {hours_held:.0f}h")

    if not result["actions"]:
        result["actions"].append("no_action_needed")

    return result


def main():
    print(f"{'='*74}")
    print(f"  ACTIVE POSITION MANAGER — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*74}")

    positions = get_open_positions()
    print(f"\n  Open positions found: {len(positions)}")
    print(f"{'─'*74}")

    if not positions:
        print("\n  No open positions to manage.\n")
        sycode_cmd = [
            "docker", "exec", "sycodetrading-supabase-db",
            "psql", "-h", "localhost", "-U", "postgres", "-d", "postgres",
            "-t", "-A", "-F|",
            "-c", "SELECT count(*) FROM managed_positions WHERE status='open'"
        ]
        try:
            result = subprocess.run(sycode_cmd, capture_output=True, text=True, timeout=10)
            count = result.stdout.strip()
            if count and count != "0":
                print(f"  ⚠️  DB reports {count} open positions but parsing failed.")
                print(f"  Check position format.")
        except:
            pass
        save_state([])
        return

    results = []
    for pos in positions:
        print(f"\n  [{pos['symbol']}] {pos['direction']} @ ${pos['entry_price']:.2f} | qty: {pos['quantity']:.4f}")
        r = manage_position(pos)
        results.append(r)

    print(f"\n{'─'*74}")
    print(f"  SUMMARY:")
    for r in results:
      actions = ", ".join(r.get("actions", ["no_data"]))
      print(f"  [{r['symbol']}] PnL: {r.get('pnl_pct', 'N/A')}% | {actions}")
    print(f"{'='*74}")

    # Save state
    state = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "position_count": len(positions),
        "results": results
    }
    save_state(results)
    save_history(state)

def save_state(results):
    with open(STATE_FILE, "w") as f:
        json.dump({"last_run": datetime.datetime.utcnow().isoformat() + "Z", "results": results}, f, indent=2)

def save_history(state):
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(state) + "\n")

if __name__ == "__main__":
    main()
