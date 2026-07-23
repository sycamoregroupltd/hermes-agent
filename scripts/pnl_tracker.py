#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""PnL Tracker + Slack/Email fallback. Records paper balance every cycle + sends test notification."""
import subprocess, json, os, sys, urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Secret scrubber — never persist a captured token (see redact() docstring).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from secret_redact import redact
def safe_err(label, proc):
    """Return a redacted stderr string safe to log/persist (token masked)."""
    return f"{label}: {redact((proc.stderr or '')[:2000])}"

# Sycode OpenClaw token — shared credential store (mirrors position_manager.py).
_CRED_ENV_FILE = os.environ.get("SYCODE_CREDENTIAL_ENV_FILE", "/home/frank/.hermes/secrets/sycode-credential.env")
if os.path.exists(_CRED_ENV_FILE):
    try:
        from dotenv import load_dotenv
        load_dotenv(_CRED_ENV_FILE, override=False)
    except Exception:
        pass
SYCODE_TOKEN = os.environ.get("SYCODE_READ_TOKEN") or os.environ.get("OPENCLAW_READ_TOKEN")
if not SYCODE_TOKEN:
    print(f"[FATAL] Missing Sycode token. Set SYCODE_READ_TOKEN/OPENCLAW_READ_TOKEN "
          f"or populate {_CRED_ENV_FILE}.", file=sys.stderr)
    sys.exit(3)

DB = ["docker", "exec", "-i", "sycodetrading-supabase-db", "psql", "-U", "postgres", "-d", "postgres"]

# --- PnL Tracking ---
r = subprocess.run(["curl","-s","--connect-timeout","10","--max-time","30",
    "-H", f"X-Sycode-Token:{SYCODE_TOKEN}",
    "http://localhost:3001/api/openclaw/status"], capture_output=True, text=True, timeout=35)
if r.returncode != 0:
    # Any failure (including timeout) leaks the argv repr — which embeds the
    # token — into stderr. Mask it before logging so the cron job's persisted
    # last_error never contains the secret.
    print(safe_err("OPENCLAW_STATUS_FAILED", r), file=sys.stderr)
    sys.exit(1)
status = json.loads(r.stdout) if r.stdout else {}
balance = status.get("balance", {}).get("total", 0)
positions = status.get("openPositions", 0)

entry = json.dumps({"balance": round(balance, 2), "positions": positions, "ts": datetime.now(timezone.utc).isoformat()})
sql = f"INSERT INTO n8n_market_data (source, payload) VALUES ('pnl-snapshot', $TAG${entry}$TAG$::jsonb);"
subprocess.run(DB, input=sql.encode(), capture_output=True, timeout=10)
print(f"PnL: ${balance:.2f} ({positions} positions)")

# --- Notification test — try Slack first ---
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL")
if SLACK_WEBHOOK:
    try:
        msg = json.dumps({"text": f"🤖 Trading Engine Heartbeat\nBalance: ${balance:.2f}\nPositions: {positions}"}).encode()
        req = urllib.request.Request(SLACK_WEBHOOK, data=msg, headers={"Content-Type":"application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        print(f"Slack: {resp.status}")
    except Exception as e:
        print(f"Slack failed: {e}")
else:
    print("No Slack webhook configured")

# Check if SMTP is available for email
import smtplib
SMTP_HOST = os.environ.get("SMTP_HOST")
if SMTP_HOST:
    try:
        s = smtplib.SMTP(SMTP_HOST, int(os.environ.get("SMTP_PORT", 587)), timeout=10)
        s.ehlo()
        print(f"SMTP {SMTP_HOST}: reachable")
        s.quit()
    except Exception as e:
        print(f"SMTP failed: {e}")
else:
    print("No SMTP configured")

print(f"[PNL] Balance=${balance:.2f} Positions={positions}")
