#!/usr/bin/env python3
import sys, json
d = json.load(sys.stdin)
print(f'Signal Stats: {json.dumps(d, indent=2)}')
