#!/usr/bin/env python3
"""Fetch all Sycode analytics data via token-safe curl and produce report."""
import subprocess, json, sys, os
from datetime import datetime, timezone

SYCODE_CURL = os.path.expanduser("~/.hermes/scripts/sycode-token-safe-curl.sh")
BASE = "http://localhost:3001/api/openclaw"

def safe_fetch(endpoint):
    """Run token-safe curl, return parsed JSON."""
    try:
        result = subprocess.run(
            [SYCODE_CURL, "SYCODE_READ_TOKEN", "-sS", f"{BASE}{endpoint}"],
            capture_output=True, text=True, timeout=30
        )
        # Check for token resolution error line
        output = result.stdout.strip()
        if "Could not resolve host" in output:
            # The error message gets prepended; data might be after it
            lines = output.split('\n')
            # Find where JSON starts (after the error line if present)
            for i, line in enumerate(lines):
                if line.strip().startswith('{') or line.strip().startswith('['):
                    output = '\n'.join(lines[i:])
                    break
        return json.loads(output) if output else {}
    except Exception as e:
        print(f"Error fetching {endpoint}: {e}", file=sys.stderr)
        return {}

# Step 1: ML Predictions
print("--- ML Predictions ---")
ml_data = safe_fetch("/ml/predictions/recent?limit=50")
preds = ml_data.get('predictions', [])
wins = sum(1 for p in preds if p.get('resolved_outcome') == 'win')
losses = sum(1 for p in preds if p.get('resolved_outcome') == 'loss')
total = wins + losses
rate = (wins/total*100) if total > 0 else 0
print(f"Resolved: {total}, Wins: {wins}, Losses: {losses}, Win Rate: {rate:.1f}%")

models = {}
for p in preds:
    m = p.get('model_name', p.get('modelType', 'unknown'))
    outcome = p.get('resolved_outcome')
    if m not in models:
        models[m] = {'wins': 0, 'losses': 0, 'total': 0}
    if outcome in ('win', 'loss'):
        models[m]['total'] += 1
        if outcome == 'win':
            models[m]['wins'] += 1
        else:
            models[m]['losses'] += 1
for m, v in sorted(models.items()):
    wr = (v['wins']/v['total']*100) if v['total'] > 0 else 0
    flag = " WARNING" if wr < 50 else ""
    print(f"  {m}: {v['wins']}/{v['total']} = {wr:.1f}%{flag}")

# Also show avg confidence
avg_conf = sum(float(p.get('predictedProbability', 0)) for p in preds if p.get('predictedProbability')) / len(preds) if preds else 0
print(f"  Avg prediction confidence: {avg_conf*100:.1f}%")

# Step 2: Signal Journey Stats
print("\n--- Signal Stats ---")
sig_data = safe_fetch("/signals/journey/stats")
print(json.dumps(sig_data, indent=2))

# Step 3: Strategy Health
print("\n--- Strategies ---")
strat_data = safe_fetch("/strategies/enabled?limit=20")
strats = strat_data.get('strategies', strat_data if isinstance(strat_data, list) else [])
if isinstance(strats, dict):
    strats = strats.get('strategies', [strats])
print(f"Enabled strategies: {len(strats) if isinstance(strats, list) else '?'}")

if isinstance(strats, list):
    for s in strats:
        name = s.get("name", "?")
        enabled = s.get("isEnabled", s.get("enabled", "?"))
        print(f"  {name}: enabled={enabled}")

# Step 4: Market Context
print("\n--- Market Context ---")
mkt_data = safe_fetch("/market-context")
print(json.dumps(mkt_data, indent=2)[:3000])
