#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
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
openai-codex/gpt-5.5. jarvis's primary is openai-codex, so the `-z ok` prewarm refreshes
the codex token before each push. The original xai-only behavior is preserved.
"""
import json, glob, subprocess

HERMES = '/home/frank/.hermes/hermes-agent/venv/bin/hermes'
PROF = '/home/frank/.hermes/profiles'
GLOBAL = '/home/frank/.hermes/auth.json'
POOL_KEYS = ('xai-oauth', 'xai', 'openai-codex', 'nous')   # credential_pool entries to push
PROVIDER_KEYS = ('xai-oauth', 'openai-codex', 'nous')     # providers[] sections to mirror

# 1) Pre-warm jarvis xAI explicitly so its stored xai-oauth token is fresh
#    (refreshes if near-expiry, rewrites jarvis/auth.json). Do not rely on
#    `hermes -z ok`: in non-TTY cron shells it can hang under some model/config
#    paths. The chat path below is the Hermes CLI path verified by `auth status`
#    and real xai-oauth smoke tests.
try:
    prewarm = subprocess.run([
        HERMES, '-p', 'jarvis', 'chat', '--provider', 'xai-oauth',
        '-m', 'grok-4.3', '-t', '', '-q', 'Return exactly OK.'
    ], timeout=180, capture_output=True)
except Exception as e:
    print(f'jarvis xai-oauth prewarm failed ({type(e).__name__}); no sync performed')
    raise SystemExit(0)
if prewarm.returncode != 0:
    print('jarvis xai-oauth prewarm failed; no sync performed')
    raise SystemExit(0)

# 2) Read jarvis's current credential(s).
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
    print(f'cannot read jarvis auth: {e}'); raise SystemExit(0)
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
    raise SystemExit(0)

# 3) Push to every other profile + the global pool (only when changed).
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

