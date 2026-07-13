#!/usr/bin/env python3
import json
with open('/tmp/analytics_ml.json') as f:
    d = json.load(f)
preds = d.get('predictions', [])
if preds:
    p = preds[0]
    for k in sorted(p.keys()):
        print(f'{k}: {p[k]}')
    print(f'Total predictions: {len(preds)}')
else:
    print('No predictions found')
    print(json.dumps(d, indent=2)[:1000])
