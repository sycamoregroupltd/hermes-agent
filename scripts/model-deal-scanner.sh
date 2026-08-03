#!/usr/bin/env bash
# model-deal-scanner.sh — weekly sweep of every provider's LIVE model catalogue.
# Created 2026-08-03 (Frank: "new capable models and deals come out all the time —
# we need to keep on top of it to maximise production, efficiency and token cost").
#
# WHY THIS EXISTS: on 2026-08-02 the fleet ran for weeks on a model whose account had
# no credit, and on 2026-08-03 a 90% discount on the model we were ALREADY using was
# spotted by Frank in a social post, not by the system. Both are the same failure:
# nobody was watching the provider catalogue.
#
# DESIGN (literal):
#  - READ-ONLY. It NEVER changes a model pin. Model/provider routing is a Frank gate (A3).
#    Its job is to SURFACE options with evidence; the decision stays human.
#  - It writes a dated record to the vault and prints a DIFF-ONLY summary. Empty stdout
#    when nothing changed = silent (no-agent watchdog pattern), so a delivered message
#    always means something actually moved.
#  - It reports what it CANNOT see (pricing/discounts are not exposed by these APIs) rather
#    than implying full coverage.
set -u
export PATH="/home/frank/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
VAULT=/home/frank/obsidian-fleet-vault/Operations/model-catalogue
STATE=/home/frank/.hermes/state/model-catalogue.json
mkdir -p "$VAULT" "$(dirname "$STATE")"
TODAY=$(date -u +%F)
OUT="$VAULT/$TODAY-model-catalogue.md"

python3 - "$STATE" "$OUT" <<'PY'
import json, os, subprocess, sys, datetime, yaml

sys.path.insert(0, "/home/frank/.hermes/scripts")
from second_brain_writer import render_markdown, write_text_atomic

state_path, out_path = sys.argv[1], sys.argv[2]
prev = {}
if os.path.exists(state_path):
    try: prev = json.load(open(state_path))
    except Exception: prev = {}

def curl_json(url, key):
    # curl, NOT python urllib: raw urllib gets Cloudflare 403/1010 from this box (proven 2026-08-03)
    try:
        r = subprocess.run(['curl','-4','-s','--max-time','45',url,'-H',f'Authorization: Bearer {key}'],
                           capture_output=True, text=True, timeout=60)
        return json.loads(r.stdout)
    except Exception as e:
        return {'_error': f'{type(e).__name__}: {e}'}

providers = {}

# --- groq (key lives in a profile custom_providers block, not .env)
try:
    j = yaml.safe_load(open('/home/frank/.hermes/profiles/jarvis/config.yaml')) or {}
    gp = [p for p in (j.get('custom_providers') or []) if 'groq' in str(p.get('base_url','')).lower()]
    if gp:
        d = curl_json('https://api.groq.com/openai/v1/models', gp[0]['api_key'])
        providers['groq'] = sorted(m['id'] for m in d.get('data', [])) if 'data' in d else []
except Exception:
    pass

# --- nvidia NIM
try:
    for line in open('/home/frank/.env', errors='ignore'):
        if line.startswith('NVIDIA_API_KEY='):
            k = line.split('=',1)[1].strip()
            d = curl_json('https://integrate.api.nvidia.com/v1/models', k)
            providers['nim'] = sorted(m['id'] for m in d.get('data', [])) if 'data' in d else []
            break
except Exception:
    pass

# --- nous (via the hermes CLI, which holds the oauth credential)
try:
    r = subprocess.run(['hermes','model','--refresh'], capture_output=True, text=True, timeout=90)
    providers['nous'] = ['(catalogue refreshed via hermes model --refresh; see portal for pricing)']
except Exception:
    pass

lines, changed = [], False
for prov, models in sorted(providers.items()):
    old = set(prev.get(prov, []))
    new = set(models)
    added, removed = sorted(new-old), sorted(old-new)
    if added or removed:
        changed = True
        if added:   lines.append(f"  {prov}: NEW MODELS -> {', '.join(added[:8])}" + (" …" if len(added)>8 else ""))
        if removed: lines.append(f"  {prov}: REMOVED    -> {', '.join(removed[:8])}" + (" …" if len(removed)>8 else ""))

# durable vault record, written every run via the canonical writer
today = datetime.date.today()
body = "# Provider model catalogue\n\nRead-only sweep. This scanner NEVER changes a model pin —\n" \
       "routing is a Frank decision (A3).\n\n"
for prov, models in sorted(providers.items()):
    body += f"## {prov}\n\n"
    for m in models:
        body += f"- {m}\n"
    body += "\n"
body += ("## What this scanner CANNOT see\n\n"
         "Provider APIs expose model IDs and (sometimes) context windows — **not prices, discounts or\n"
         "promotions**. The 2026-08-03 DeepSeek V4 Flash 0731 90%-off window was announced socially and\n"
         "is invisible here. Pricing must still be checked at the portal, and any deal worth acting on\n"
         "should be recorded in the suggestion register.\n")
props = {
    "title": f"Provider model catalogue {today}",
    "type": "source",
    "status": "active",
    "created": today.isoformat(),
    "updated": today.isoformat(),
    "confidence": "high",
    "tags": ["providers", "models", "cost"],
    "sources": [
        "https://api.groq.com/openai/v1/models",
        "https://integrate.api.nvidia.com/v1/models",
        "hermes model --refresh (nous catalogue)",
    ],
    "generated": True,
    "generator": "model-deal-scanner.sh",
}
write_text_atomic(out_path, render_markdown(body, props))

json.dump(providers, open(state_path,'w'), indent=1)

if changed:
    print(f"MODEL CATALOGUE CHANGED ({datetime.date.today()}):")
    print("\n".join(lines))
    print(f"  full record: {out_path}")
    print("  NOTE: prices/discounts are NOT visible to this scanner — check portal.nousresearch.com")
PY
