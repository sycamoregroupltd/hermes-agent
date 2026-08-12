#!/usr/bin/env python3
"""Alert when Nous Portal usable credits fall below threshold.
No output when healthy; cron delivers non-empty output to Discord #critical-alerts.

Falling-edge dedup: alert once when credits first cross below threshold, stay
silent while the same low-balance condition persists, then re-remind slowly.
"""
import json, os, sys, time
from pathlib import Path

sys.path.insert(0, "/home/frank/.hermes/hermes-agent")
from hermes_cli.nous_account import get_nous_portal_account_info

THRESHOLD = float(os.getenv("NOUS_BALANCE_ALERT_THRESHOLD_USD", "5.0"))
REMIND_SECONDS = int(os.getenv("NOUS_BALANCE_REMIND_SECONDS", str(24 * 3600)))
STATE = Path(os.getenv("NOUS_BALANCE_WATCHDOG_STATE", "/home/frank/.hermes/profiles/jarvis/cron/state/nous_balance_watchdog.first_seen.json"))


def read_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


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


info = get_nous_portal_account_info(force_fresh=True, min_jwt_ttl_seconds=0)
access = info.paid_service_access_info
usable = access.total_usable_credits if access else None
subscription = access.subscription_credits_remaining if access else None
purchased = access.purchased_credits_remaining if access else None
if usable is None:
    # Transient JWT/account-info hiccup: do not page Frank. Preserve any existing
    # low-balance state so a real low balance remains deduped on the next read.
    sys.exit(0)
elif usable < THRESHOLD:
    now = int(time.time())
    state = read_state()
    first_seen = int(state.get("first_seen") or now)
    last_alert = int(state.get("last_alert") or 0)
    if not state or now - last_alert >= REMIND_SECONDS:
        write_state({"first_seen": first_seen, "last_alert": now, "usable": usable, "threshold": THRESHOLD})
        print(f"CRITICAL: Nous usable credits low: ${usable:.2f} below ${THRESHOLD:.2f} threshold (subscription={subscription}, purchased={purchased}). Top up: https://portal.nousresearch.com/billing?topup=open")
    else:
        write_state({**state, "usable": usable, "threshold": THRESHOLD, "last_seen": now})
else:
    clear_state()
