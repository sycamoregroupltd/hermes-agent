#!/usr/bin/env python3
import json
with open('/tmp/analytics_strategies.json') as f:
    d = json.load(f)
strats = d.get('strategies', [])
print(f'Total strategies: {d.get("count", len(strats))}')
for i, s in enumerate(strats):
    meta = s.get('meta', {})
    source = meta.get('source', 'unknown')
    ml_val = meta.get('mlValidation', {})
    ml_accepted = ml_val.get('accepted', '?')
    risk = s.get('riskProfile', {})
    max_risk = risk.get('maxPortfolioRiskPct', '?')
    symbols = s.get('signalFilter', {}).get('symbols', [])
    tfs = s.get('signalFilter', {}).get('timeframes', [])
    dirs = s.get('signalFilter', {}).get('directions', [])
    print(f'{i+1}. {s["name"]}')
    print(f'   enabled={s.get("enabled")} mode={s.get("tradingMode")} source={source} ml_accepted={ml_accepted} maxRisk={max_risk}%')
    print(f'   symbols={symbols} tfs={tfs} dirs={dirs}')
