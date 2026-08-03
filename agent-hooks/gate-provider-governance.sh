#!/usr/bin/env bash
# provider-governance hook: Enforce approved fleet provider chain using Hermes-native patterns.
# Triggers on pre_llm_call, on_session_start. Alerts on drift, blocks for critical profiles.
# Uses exact chain from provider-governance skill. Integrates with elon-governor-oversight.
# Non-blocking for normal profiles (alert only). Fail-quiet on errors.
set -uo pipefail
payload=$(cat 2>/dev/null)
ok() { echo '{}'; exit 0; }
block() { python3 -c "
import json,sys
print(json.dumps({'decision':'block','reason':sys.argv[1]}))
" "$1"; exit 0; }
alert() {
  hermes send -t telegram -q "🔴 PROVIDER GOVERNANCE: $1" >/dev/null 2>&1 || true
  echo "[$(date -u +%H:%M)] provider-governance: $1" >> /home/frank/.hermes/logs/provider-governance.log 2>/dev/null || true
}

# Parse payload for profile, model, provider
profile=$(echo "$payload" | python3 -c "
import json,sys
try:
  d=json.load(sys.stdin)
  print(d.get('profile') or d.get('profile_name') or d.get('hermes_profile') or '-')
except: print('-')
" 2>/dev/null || echo '-')

model=$(echo "$payload" | python3 -c "
import json,sys
try:
  d=json.load(sys.stdin)
  print(d.get('model') or d.get('default_model') or d.get('model_name') or '-')
except: print('-')
" 2>/dev/null || echo '-')

provider=$(echo "$payload" | python3 -c "
import json,sys
try:
  d=json.load(sys.stdin)
  p = d.get('provider') or d.get('model_provider') or d.get('provider_name') or '-'
  print(p)
except: print('-')
" 2>/dev/null || echo '-')

# Exact approved chain from provider-governance skill (2026-06-23)
# Exact approved chain from provider-governance skill (2026-07-11 HARDENED, t_df3f87ff).
# REMOVED ollama-cloud / ollama-local: they were blanket-approved, which let the
# 2026-07-08 silent qwen3:8b masquerade pass this gate undetected. Local ollama is
# now permitted ONLY when serving a genuinely-LOCAL model name (see local_allowed
# below), never a cloud model name. Cloud requests must never silently resolve to
# local ollama.
approved_cloud_prefixes="xai-oauth openai-codex nous"
approved_chain=(
  "xai-oauth/grok-4.3"
  "openai-codex/gpt-5.5"
  "nous"
)

is_critical=false
case "$profile" in
  elon|*pm|*governor|jarvis|research*|builder|devops|guardian|os-*) is_critical=true ;;
esac

# ---------------------------------------------------------------------------
# FAIL-LOUD #1: masquerade detection (root cause of t_df3f87ff).
# A CLOUD-model name (deepseek-v4, glm-5.2, gemini-*, gpt-5.5, ...) must NEVER be
# served by provider=ollama-local / ollama-cloud. If it is, the resolver silently
# relabeled a cloud request as a local model. Block + loud alert for ALL profiles
# (critical or not) — this is exactly the failure that degraded the fleet.
# ---------------------------------------------------------------------------
cloud_model_patterns="deepseek|glm-|gemini|gpt-|claude|grok|command-|mistral|llama-|nova|sonnet|opus"
is_masquerade=false
if [[ "$provider" == *"ollama-local"* ]] || [[ "$provider" == *"ollama-cloud"* ]]; then
  if echo "$model" | grep -qiE "($cloud_model_patterns)"; then
    is_masquerade=true
  fi
fi

if $is_masquerade; then
  msg="MASQUERADE BLOCK (t_df3f87ff): local ollama serving CLOUD model name model=$model provider=$provider profile=$profile. Cloud requests must not silently resolve to local ollama."
  alert "$msg"
  block "$msg"
fi

# Check if current provider matches approved cloud list
approved_providers="$approved_cloud_prefixes"
provider_match=false
for ap in $approved_providers; do
  if [[ "$provider" == *"$ap"* ]]; then provider_match=true; break; fi
done

# Local ollama is allowed ONLY for genuinely-local models (no cloud name in model).
local_allowed=false
if [[ "$provider" == *"ollama-local"* ]] || [[ "$provider" == *"ollama-cloud"* ]]; then
  if ! echo "$model" | grep -qiE "($cloud_model_patterns)"; then
    local_allowed=true
  fi
fi

# Check if combined provider/model matches approved chain
provider_ok=false
combined="${provider}/${model}"
for entry in "${approved_chain[@]}"; do
  if [[ "$combined" == *"$entry"* ]] || [[ "$provider" == *"$entry"* ]] || [[ "$model" == *"$entry"* ]]; then
    provider_ok=true
    break
  fi
done

if [ "$profile" = "-" ] || [ "$provider" = "-" ]; then ok; fi

# Block/alert when NOT an approved cloud chain AND NOT a legitimately-local model.
# (Before hardening this was `! provider_ok || ! provider_match`; we keep the
#  provider_match requirement so dead/unapproved cloud providers like `groq`
#  are once again rejected for critical profiles instead of silently passing.)
if ! $provider_ok || ! $provider_match; then
  if ! $local_allowed; then
    msg="Unauthorised provider/model for profile=$profile: provider=$provider model=$model (approved chain: ${approved_chain[*]}; local ollama allowed only for local model names, never cloud model names)"
    if $is_critical; then
      alert "$msg -- BLOCKING for critical profile"
      block "$msg"
    else
      alert "$msg"
    fi
  fi
fi

# Always allow if passed or non-critical
ok
