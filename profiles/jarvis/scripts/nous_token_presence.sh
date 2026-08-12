#!/usr/bin/env bash
# 24h token-survival watch (upgrade card t_5e4da626 acceptance): exit 1 when the jarvis
# nous pool is empty — cron error state IS the alert (exit-code liveness doctrine).
python3 - <<'PY'
import json, sys
n = (json.load(open('/home/frank/.hermes/profiles/jarvis/auth.json')).get('credential_pool') or {}).get('nous') or []
print(f"jarvis nous creds: {len(n)}")
sys.exit(0 if len(n) > 0 else 1)
PY
