#!/usr/bin/env python3
# CANONICAL SOURCE — keep /home/frank/.hermes/scripts/ and
# /home/frank/.hermes/profiles/jarvis/scripts/ byte-identical.
# NOTE: the `fleet-cred-sync` cron resolves the bare script name to the PROFILE-LOCAL
# copy (verified 2026-08-12 by triggering the job and reading cron/output/). Editing only
# ~/.hermes/scripts/ changes nothing at runtime.
"""Durable credential sharing across the fleet (single-refresher + push).

Root cause this solves: xAI AND Codex (openai-codex) rotate the refresh_token on every
refresh, so only ONE client can refresh successfully. If every profile holds its own copy,
all but one fail once their access_token expires.

Fix: make jarvis (the gateway) the SOLE refresher, then push its fresh token to every
profile BEFORE their access_token expires. Copies therefore never self-refresh, so they
never hit the rotation failure. Interval must stay < access_token lifetime (~hourly) —
7 min gives a wide margin. NOTE: token values are never printed.

2026-06-30 (claude-bcf4cc1a): extended to also sync openai-codex (credential_pool +
provider section) after the fleet migrated its primary from xai-oauth/grok-4.3 to
openai-codex/gpt-5.5.

2026-08-12 (earlier): pre-warm made BEST-EFFORT per provider so one dead rung degrades
the sync instead of disabling it. History: this step hardcoded `--provider xai-oauth` and
exited on non-zero. When xAI's refresh token became invalid ("invalid_grant"), the whole
fleet's credential distribution stopped silently — nous lived only in jarvis and every
other profile crashed with "No access token found".

2026-08-12 (claude-orchestrator): two remaining defects fixed.

  1. COST — the nous pre-warm was a billed `hermes chat` completion every 7 min (~205
     LLM calls/day) purely to refresh a token. Replaced with the native in-process path
     (hermes_cli.auth.resolve_nous_runtime_credentials): ~1s, zero tokens.
     HERMES_HOME MUST be set to the jarvis profile or the refresh silently lands in the
     DEFAULT profile store instead (verified 2026-08-12: jarvis's expires_at was
     unchanged while the global store advanced).

  2. FLAPPING — Hermes only auto-refreshes within ACCESS_TOKEN_REFRESH_SKEW_SECONDS
     (120s) of expiry, but this job runs every 7 min. So a run could return a token with
     ~3 min of life, push it to all 73 profiles, and leave the fleet holding an EXPIRED
     credential for the remainder of the window — the recurring "nous keeps going down"
     outage. We now force a full-lifetime refresh whenever the token has less runway
     than MIN_PUSH_TTL_SECONDS, which is set well above the cron cadence.

  Exit code is now the alert: a fleet primary that cannot be refreshed exits non-zero
  instead of reporting a green "ok" over an outage (exit-code liveness doctrine).
"""
import json, glob, subprocess, sys

HERMES_AGENT = '/home/frank/.hermes/hermes-agent'
HERMES = f'{HERMES_AGENT}/venv/bin/hermes'
VENV_PY = f'{HERMES_AGENT}/venv/bin/python'
PROF = '/home/frank/.hermes/profiles'
JARVIS_HOME = f'{PROF}/jarvis'
GLOBAL = '/home/frank/.hermes/auth.json'
# NOUS DELIBERATELY EXCLUDED (2026-08-28, Frank).
# This script exists for xAI/grok OAuth. Nous was added to these tuples at some point and
# that single change caused a fleet-wide outage class: Hermes ALREADY shares Nous natively
# via <hermes-root>/shared/nous_auth.json — one file outside every profile, read by all,
# rotated under _nous_shared_store_lock(), with auto-import for profiles holding none.
# Pushing providers.nous into all 73 profile auth.json files converted that ONE coordinated
# credential into 73 independent refreshers racing on a SINGLE-USE refresh token. Nous Portal
# detects the replay as token reuse and revokes the whole family, logging every profile out
# (68 dead on 2026-08-28; ~80% of dispatch burned on workers dying in ~1s at startup).
# Do NOT re-add 'nous' here. If Nous creds need distributing, the answer is the native shared
# store, not a fan-out. See memory hermes-native-first-not-patches + card t_e30f855d.
POOL_KEYS = ('xai-oauth', 'xai')           # credential_pool entries to push
PROVIDER_KEYS = ('xai-oauth',)             # providers[] sections to mirror

# Minimum nous runway to push to the fleet. MUST stay comfortably above the 7-min cron
# cadence so a pushed token cannot expire before the next sync replaces it.
MIN_PUSH_TTL_SECONDS = 15 * 60

# --- 1) Pre-warm jarvis credentials (best-effort per provider; never aborts the push) ---

# nous: native, token-free refresh with a runway guarantee.
_NOUS_REFRESH = f'''
import sys, time, datetime
sys.path.insert(0, {HERMES_AGENT!r})
from hermes_cli.auth import resolve_nous_runtime_credentials

MIN_TTL = {MIN_PUSH_TTL_SECONDS}

def ttl(expires_at):
    if not expires_at:
        return -1
    try:
        dt = datetime.datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except Exception:
        return -1
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp() - time.time()

# Cheap path first: no-ops while the token is still healthy.
r = resolve_nous_runtime_credentials(timeout_seconds=30, force_refresh=False)
remaining = ttl(r.get("expires_at"))
if remaining < MIN_TTL:
    # Too little runway to survive until the next sync — force a full-lifetime token.
    r = resolve_nous_runtime_credentials(timeout_seconds=30, force_refresh=True)
    remaining = ttl(r.get("expires_at"))
if not r.get("api_key"):
    raise SystemExit("nous refresh returned no usable key")
if remaining < MIN_TTL:
    raise SystemExit("nous still short-lived after forced refresh: %.0fs" % remaining)
print("expires_at=%s (ttl %.0fm)" % (r.get("expires_at"), remaining / 60))
'''


def prewarm_nous():
    """Refresh jarvis's nous token natively. Returns (ok, message). Never raises."""
    try:
        p = subprocess.run(
            [VENV_PY, '-c', _NOUS_REFRESH],
            env={'HERMES_HOME': JARVIS_HOME, 'PATH': '/usr/bin:/bin', 'HOME': '/home/frank'},
            timeout=120, capture_output=True, text=True,
        )
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'
    out = (p.stdout or '').strip()
    if p.returncode != 0:
        err = (p.stderr or out or 'unknown error').strip().splitlines()
        return False, err[-1] if err else 'unknown error'
    return True, out


nous_ok, nous_msg = prewarm_nous()
print(f'prewarm nous: {"ok" if nous_ok else "FAILED"} ({nous_msg})')

# xai-oauth: kept best-effort. Dead as of 2026-08-12 (refresh_token invalid_grant) and
# will stay dead until Frank re-authenticates; a failed attempt costs no tokens because
# it fails at the auth step, and this resumes working automatically after re-login.
try:
    pw = subprocess.run(
        [HERMES, '-p', 'jarvis', 'chat', '--provider', 'xai-oauth',
         '-m', 'grok-4.3', '-t', '', '-q', 'Return exactly OK.'],
        timeout=180, capture_output=True,
    )
    print(f'prewarm xai-oauth: {"ok" if pw.returncode == 0 else "failed (continuing)"}')
except Exception as e:
    print(f'prewarm xai-oauth: {type(e).__name__} (continuing)')

# --- 2) Read jarvis's current credential(s). ---
def usable_entries(value):
    entries = value if isinstance(value, list) else [value]
    usable = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # OAuth copies must include an access token after the sole-refresher prewarm;
        # API-key entries must carry key material. Never propagate empty placeholders.
        if entry.get('access_token') or entry.get('api_key') or entry.get('key'):
            usable.append(entry)
    return usable

try:
    ja = json.load(open(f'{PROF}/jarvis/auth.json'))
except Exception as e:
    print(f'cannot read jarvis auth: {e}')
    raise SystemExit(1)
jp = ja.get('credential_pool', {})
jprov = ja.get('providers', {})

valid = {}
for k in POOL_KEYS:
    if k not in jp:
        continue
    entries = usable_entries(jp[k])
    if entries:
        valid[k] = entries if isinstance(jp[k], list) else entries[0]

# Provider sections (e.g. openai-codex chatgpt-mode tokens) the resolver needs alongside
# the pool entry. Only mirror sections that actually carry token material.
valid_providers = {}
for k in PROVIDER_KEYS:
    sect = jprov.get(k)
    if isinstance(sect, dict) and (sect.get('tokens') or sect.get('auth_mode') or sect.get('access_token') or sect.get('api_key')):
        valid_providers[k] = sect

if not valid and not valid_providers:
    print('no usable credential in jarvis pool; no sync performed')
    raise SystemExit(1)

# --- 3) Push to every other profile + the global pool (only when changed). ---
n = 0
for p in glob.glob(f'{PROF}/*/auth.json'):
    if p.endswith('/jarvis/auth.json'):
        continue
    try:
        d = json.load(open(p))
    except Exception:
        continue
    changed = False
    cp = d.setdefault('credential_pool', {})
    for k, v in valid.items():
        if cp.get(k) != v:
            cp[k] = v; changed = True
    prov = d.setdefault('providers', {})
    for k, v in valid_providers.items():
        if prov.get(k) != v:
            prov[k] = v; changed = True
    if changed:
        json.dump(d, open(p, 'w'), indent=2); n += 1

try:
    gd = json.load(open(GLOBAL))
    gd.setdefault('credential_pool', {}).update(valid)
    if valid_providers:
        gd.setdefault('providers', {}).update(valid_providers)
    json.dump(gd, open(GLOBAL, 'w'), indent=2)
except Exception as e:
    print(f'global pool update skipped: {e}')

print(f'credential sync from jarvis -> {n} profiles updated '
      f'(pool={list(valid)}, providers={list(valid_providers)}) + global pool')

# The fleet primary is nous. If it could not be refreshed, the credentials just pushed
# are on a countdown to expiry — surface that as a cron error, not a silent "ok".
if not nous_ok:
    print('FLEET PRIMARY (nous) COULD NOT BE REFRESHED — pushed credentials will expire')
    raise SystemExit(1)
