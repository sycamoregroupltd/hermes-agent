#!/usr/bin/env bash
# Verify that Jarvis is the only gateway dispatcher and no CLI pass is enabled.
# This is read-only; nonzero output identifies the offending pid/source.
set -uo pipefail

fail=0
lock=/home/frank/.hermes/kanban/.dispatcher.lock
expected=/home/frank/.hermes/profiles/jarvis/config.yaml

# The effective flag must be explicit on every live profile config; archived
# profiles are intentionally outside the * glob and are not runtime gateways.
true_configs=()
shopt -s nullglob
for cfg in /home/frank/.hermes/profiles/*/config.yaml; do
  if grep -q 'dispatch_in_gateway: true' "$cfg"; then
    true_configs+=("$cfg")
  fi
done
if ((${#true_configs[@]} != 1)) || [[ "${true_configs[0]:-}" != "$expected" ]]; then
  printf 'dispatcher-singleton-check: offending config flag(s):'
  if ((${#true_configs[@]} == 0)); then
    printf ' none'
  else
    printf ' %s' "${true_configs[@]}"
  fi
  printf '\n'
  fail=1
fi

# fuser reports the process holding the machine-global dispatcher lock.
gateway_pid="$(/usr/bin/systemctl --user show hermes-gateway-jarvis.service -p MainPID --value 2>/dev/null || true)"
holder_raw="$(/usr/bin/fuser "$lock" 2>/dev/null || true)"
read -r -a holders <<< "$holder_raw"
if [[ ! "$gateway_pid" =~ ^[1-9][0-9]*$ ]] || ((${#holders[@]} != 1)) || [[ "${holders[0]:-}" != "$gateway_pid" ]]; then
  printf 'dispatcher-singleton-check: offending pid(s): lock=%s gateway=%s\n' "${holder_raw:-none}" "${gateway_pid:-none}"
  fail=1
fi

# Enabled profile-local cron scripts must not contain a second CLI dispatch
# pass. The embedded gateway pass is deliberately not represented as a job.
cli_sources="$(/usr/bin/python3 - <<'PY'
import json
import re
from pathlib import Path

jobs_path = Path('/home/frank/.hermes/profiles/jarvis/cron/jobs.json')
try:
    raw = json.loads(jobs_path.read_text(encoding='utf-8'))
    jobs = raw if isinstance(raw, list) else raw.get('jobs', raw)
    if isinstance(jobs, dict):
        jobs = jobs.values()
except (OSError, ValueError, AttributeError):
    print('cron-store-unreadable')
    raise SystemExit(0)

pattern = re.compile(r'\bhermes\s+kanban\b[^\n;]*\bdispatch\b')
for job in jobs:
    if not isinstance(job, dict) or not job.get('enabled'):
        continue
    script = str(job.get('script') or '')
    if not script:
        continue
    candidates = [Path(script)]
    if not Path(script).is_absolute():
        candidates = [
            Path('/home/frank/.hermes/profiles/jarvis/scripts') / script,
            Path('/home/frank/.hermes/scripts') / script,
        ]
    for path in candidates:
        try:
            text = path.read_text(encoding='utf-8')
        except OSError:
            continue
        if pattern.search(text):
            print(f"{job.get('id', '?')}:{job.get('name', '?')}:{path}")
            break
PY
)"
if [[ -n "$cli_sources" ]]; then
  printf 'dispatcher-singleton-check: offending enabled CLI dispatch source(s):\n%s\n' "$cli_sources"
  fail=1
fi

if ((fail)); then
  exit 1
fi
printf 'dispatcher-singleton-check: PASS lock_pid=%s gateway_pid=%s config_true=jarvis cli_dispatch_sources=0\n' "$holders" "$gateway_pid"
