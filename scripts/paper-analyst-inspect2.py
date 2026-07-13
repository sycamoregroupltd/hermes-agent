#!/usr/bin/env python3
import json, sys

with open('/tmp/analytics_signals.json') as f:
    sig = json.load(f)
print("=== SIGNAL JOURNEY STATS ===")
print(json.dumps(sig, indent=2))

print()

with open('/tmp/analytics_strategies.json') as f:
    strat = json.load(f)
print("=== STRATEGIES ===")
print(json.dumps(strat, indent=2)[:3000])

print()

with open('/tmp/analytics_market.json') as f:
    mkt = json.load(f)
print("=== MARKET CONTEXT ===")
print(json.dumps(mkt, indent=2)[:3000])
