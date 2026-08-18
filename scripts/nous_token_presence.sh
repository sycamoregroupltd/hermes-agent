#!/usr/bin/env bash
# Primary-provider liveness watch. Canonical copy; profile copies are exec shims.
#
# WHY THIS WAS REWRITTEN (2026-08-14):
# The previous version asserted that the jarvis nous credential POOL WAS NON-EMPTY:
#     n = auth.json.credential_pool.nous ;  exit(0 if len(n) > 0 else 1)
# On 2026-08-13T21:10:06Z the nous refresh session was REVOKED (invalid_grant). A revoked
# credential is still PRESENT in the pool, so len(n) stayed 1 and this script exited 0 —
# "healthy" — for the entire 11-hour fleet blackout. Presence is not validity. (Same bug
# class as backup-freshness-monitor.sh's `test -e`, which passed an empty directory the
# same night.)
#
# WHAT THIS CHECKS INSTEAD: outcomes. Did the primary provider actually SERVE anything
# recently? Session rows in each profile's state.db are written by real inference calls,
# so they cannot be faked by a stale flag. Pool `last_status` is deliberately NOT trusted
# as an alert trigger — those flags are known zombies in both directions (codex has read
# "exhausted" while serving 100k-token calls).
#
# FALSE-ALARM GUARD: 00:00-08:00 UTC is a genuine fleet quiet window (typically ~3 active
# profiles). "Zero nous sessions" alone would page every night. So this alerts only when
# the fleet is demonstrably TRYING to work and the primary is producing nothing — i.e.
# other providers are serving while the primary is silent.
#
# Empty stdout + exit 0 = healthy. Any output + exit 1 = alert (cron error state IS the
# alert, per the exit-code liveness doctrine — stdout is never parsed by no-agent cron).
set -u
export PATH="/home/frank/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

python3 - <<'PY'
import glob, json, os, sqlite3, sys, time

WINDOW_MIN   = 45   # >30 so a short quiet patch cannot produce a false zero
MIN_ACTIVITY = 5    # fleet must show at least this many sessions before "primary dark" means anything

now = int(time.time())
since = now - WINDOW_MIN * 60
alerts = []

# --- 1. Outcome probe: who actually served in the window, across every profile ---
counts = {}
dbs = glob.glob('/home/frank/.hermes/profiles/*/state.db')
for db in dbs:
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=5)
        for prov, n in con.execute(
            "SELECT COALESCE(billing_provider,'unknown'), COUNT(*) FROM sessions "
            "WHERE started_at >= ? GROUP BY 1", (since,)):
            counts[prov] = counts.get(prov, 0) + n
        con.close()
    except Exception:
        # A locked or unreadable store is UNKNOWN, not zero. Skip it rather than let it
        # drag the total down and manufacture a false "primary dark".
        continue

total = sum(counts.values())
primary = 'nous'
try:
    import re
    cfg = open('/home/frank/.hermes/config.yaml').read()
    m = re.search(r'^\s*provider:\s*([A-Za-z0-9_-]+)', cfg, re.M)
    if m:
        primary = m.group(1)
except Exception:
    pass
# Test seam: a monitor nobody can prove RED is decorative. Setting this to a provider that
# is serving nothing must make the script exit 1. Never set it in the cron job itself.
primary = os.environ.get('NOUS_WATCH_PRIMARY') or primary

prim_n = counts.get(primary, 0)
if total >= MIN_ACTIVITY and prim_n == 0:
    others = ', '.join(f'{k}={v}' for k, v in sorted(counts.items(), key=lambda x: -x[1])) or 'none'
    alerts.append(
        f"PRIMARY PROVIDER DARK: '{primary}' served 0 sessions in the last {WINDOW_MIN}m "
        f"while the fleet ran {total} sessions on other providers ({others}).")
    alerts.append(
        "  A present credential is not a working one — check for a revoked refresh session: "
        "hermes model  (device-code relogin), then verify with a forced smoke: "
        "hermes -p jarvis chat -q 'Return exactly OK.' --toolsets \"\"")

# --- 2. Expiry probe: catch a token that is about to die BEFORE it takes the fleet down ---
for auth in glob.glob('/home/frank/.hermes/profiles/*/auth.json'):
    try:
        pool = (json.load(open(auth)).get('credential_pool') or {}).get(primary) or []
    except Exception:
        continue
    for cred in pool:
        if not isinstance(cred, dict):
            continue
        exp = cred.get('expires_at')
        try:
            exp = float(exp)
        except (TypeError, ValueError):
            continue
        if exp and exp < now:
            prof = os.path.basename(os.path.dirname(auth))
            mins = int((now - exp) / 60)
            alerts.append(
                f"PRIMARY TOKEN EXPIRED: {prof}/{primary} access token expired {mins}m ago "
                f"(expires_at={int(exp)}). Refresh may still work; a REVOKED refresh session will not.")

if alerts:
    print('\n'.join(alerts))
    sys.exit(1)
sys.exit(0)
PY
