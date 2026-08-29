#!/usr/bin/env python3
"""nous-balance-liveness.py — synthetic Nous Portal balance probe (card t_141e28ed, 2026-08-29).

WHY: 2026-08-03 fleet-wide worker blackhole — nous balance hit $0 and every
PAID-pinned worker (moonshotai/kimi-k3 @ Nous Portal) exited rc=0 WITHOUT a
kanban lifecycle call (protocol_violation) or died pid-not-alive, silently
shredding retries across every board. The goal-judge lane was rescued by pinning
to a FREE model (tencent/hy3:free) and later the whole pool re-pinned to
deepseek/deepseek-v4-flash-0731. The lesson: an empty balance alerts, it does
not silently eat worker retries.

WHAT: reads the real Nous Portal usable-credit balance through the same
production code path the 15m guard-bundle watchdog uses
(hermes_cli.nous_account). Run with the hermes venv python:

    /home/frank/.hermes/hermes-agent/.venv/bin/python \\
        /home/frank/.hermes/scripts/nous-balance-liveness.py [home] [threshold_usd]

HOME-RESOLUTION (t_141e28ed, 2026-08-29): stack-health-audit.sh runs with
HERMES_HOME=/home/frank/.hermes, the ROOT home whose nous session was revoked on
2026-08-28 (refresh-token reuse) — it carries no usable nous token, so reading
the balance from it returns "no usable-credit figure" and would FALSE-ALARM on
every 10-minute audit tick. The balance is ACCOUNT-level; the working tokens
live in the 70+ PROFILE homes. So the probe auto-discovers a profile home that
can actually read the account, exactly the way goal-judge-liveness.py measures
lane liveness from profile state.db. It tries the explicit home first, then
each profile home that has had a session in the recent window, and reports
WHICH home served the read. It FAILS only when NO profile home can read the
balance — a genuine provider/credential exhaustion condition.

Exit 0 = balance readable AND >= threshold.
Exit 1 = balance below threshold (worker/provider exhaustion risk) OR no profile
         home could read the balance (auth broken — the class that shredded
         retries in 2026-08).

Consumed by: stack-health-audit.sh (system crontab, */10) — see the NOUS-BALANCE
section there. Threshold overridable via NOUS_BALANCE_LIVENESS_THRESHOLD env
(default $5.00, matching the watchdog). Probe homes overridable via
NOUS_BALANCE_PROFILES_GLOB (RED-DRILL ONLY).
"""
import glob
import os
import sqlite3
import sys
import time

sys.path.insert(0, "/home/frank/.hermes/hermes-agent")

DEFAULT_HOME = "/home/frank/.hermes"
PROFILES_GLOB = os.environ.get(
    "NOUS_BALANCE_PROFILES_GLOB", "/home/frank/.hermes/profiles/*/state.db"
)
# A profile is a candidate balance reader if it has had a session recently —
# mirroring goal-judge-liveness.py's lane-liveness window.
LANE_WINDOW_MIN = int(os.environ.get("NOUS_BALANCE_LANE_WINDOW_MIN", "45"))


def _has_recent_session(db_path: str, since_epoch: float) -> bool:
    """True if a home's state.db has a session started after `since_epoch`."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM sessions WHERE started_at >= ?", (since_epoch,)
            ).fetchone()
            return bool(row and int(row[0]) > 0)
        finally:
            con.close()
    except Exception:
        return False


def _candidate_homes(prefer_home: str) -> list:
    """Ordered candidate home list: preferred first, then recent-active profiles."""
    homes = [prefer_home]
    now = time.time()
    for db in sorted(glob.glob(PROFILES_GLOB)):
        home = os.path.dirname(db)
        if home == prefer_home:
            continue
        if _has_recent_session(db, now - LANE_WINDOW_MIN * 60):
            homes.append(home)
    return homes


def _read_balance(home: str):
    """Return (usable, subscription, purchased) for one home, or (None,None,None)."""
    os.environ["HERMES_HOME"] = home
    try:
        from hermes_cli.nous_account import get_nous_portal_account_info
    except Exception:
        return None, None, None
    try:
        info = get_nous_portal_account_info(force_fresh=True, min_jwt_ttl_seconds=0)
        access = info.paid_service_access_info
        usable = access.total_usable_credits if access else None
        subscription = access.subscription_credits_remaining if access else None
        purchased = access.purchased_credits_remaining if access else None
        return usable, subscription, purchased
    except Exception:
        return None, None, None


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    home = args[0] if len(args) > 0 and args[0].startswith("/") else DEFAULT_HOME
    threshold = float(os.environ.get("NOUS_BALANCE_LIVENESS_THRESHOLD", "5.0"))
    if len(args) >= 2 and args[1].replace(".", "", 1).isdigit():
        threshold = float(args[1])

    usable = subscription = purchased = None
    serving_home = None
    attempted = []
    for cand in _candidate_homes(home):
        attempted.append(cand)
        u, s, p = _read_balance(cand)
        if u is not None:
            usable, subscription, purchased = u, s, p
            serving_home = cand
            break

    if serving_home is None:
        print(
            f"NOUS-BALANCE-FAIL unreadable: no profile home could read the account "
            f"(attempted {len(attempted)}: {', '.join(os.path.basename(h) for h in attempted)}) "
            f"— balance cannot be verified (provider/credential exhaustion risk, t_141e28ed)."
        )
        return 1

    if usable < threshold:
        print(
            f"NOUS-BALANCE-LOW usable=${usable:.2f} below ${threshold:.2f} threshold "
            f"(subscription={subscription}, purchased={purchased}, via={os.path.basename(serving_home)}) "
            f"— worker/provider exhaustion risk (t_141e28ed). Top up: https://portal.nousresearch.com/billing?topup=open"
        )
        return 1

    print(
        f"NOUS-BALANCE-OK usable=${usable:.2f} (subscription={subscription}, "
        f"purchased={purchased}, via={os.path.basename(serving_home)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
