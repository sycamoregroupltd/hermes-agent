#!/usr/bin/env bash
# Prepare, but never authenticate or invoke, the restricted Cursor profile.
#
# This is an explicit Hermes onboarding prerequisite, not a worker launcher.
# It deliberately does not read the normal ~/.cursor profile, accept an API
# key, set CURSOR_API_KEY, start `agent login`, run `agent`, or enable a
# provider route. A human completes the provider-owned browser login later
# inside this fresh profile, after the boundary itself has been reviewed.

set -euo pipefail

readonly PROFILE_ROOT="/home/frank/.hermes/provider-profiles/cursor-readonly-v1"
readonly PROFILE_CURSOR_DIR="$PROFILE_ROOT/.cursor"
readonly PROFILE_CONFIG="$PROFILE_CURSOR_DIR/cli-config.json"

usage() {
  cat <<'USAGE'
Usage: prepare_cursor_restricted_profile.sh [--prepare]

Without --prepare this command is a dry-run and prints the fixed profile
contract. --prepare creates only the fresh isolated Cursor configuration.

It never:
  * reads, mounts, copies, or modifies ~/.cursor;
  * starts an OAuth/browser login;
  * accepts or writes an API key;
  * invokes a model, starts a worker, or changes Hermes routing.

The later login/canary must use an independently reviewed Bubblewrap launcher
and be completed by the account holder. Do not bypass Cursor's sandbox or
broaden filesystem mounts to make this profile work.
USAGE
}

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--prepare" && "$1" != "--help" ) ]]; then
  usage >&2
  exit 64
fi

if [[ ${1:-} == "--help" ]]; then
  usage
  exit 0
fi

if ! command -v agent >/dev/null 2>&1; then
  printf '%s\n' 'Cursor Agent CLI (agent) is not installed; refusing to prepare profile.' >&2
  exit 69
fi

if ! command -v bwrap >/dev/null 2>&1; then
  printf '%s\n' 'Bubblewrap (bwrap) is not installed; refusing to prepare profile.' >&2
  exit 69
fi

printf 'profile_root=%s\n' "$PROFILE_ROOT"
printf 'profile_config=%s\n' "$PROFILE_CONFIG"
printf '%s\n' 'normal_profile=unread and unmodified'
printf '%s\n' 'provider_calls=0'
printf '%s\n' 'routing_changes=0'

if [[ ${1:-} != "--prepare" ]]; then
  printf '%s\n' 'result=DRY_RUN; pass --prepare to create only the empty restricted profile.'
  exit 0
fi

if [[ -e "$PROFILE_ROOT" && ! -d "$PROFILE_ROOT" ]]; then
  printf '%s\n' 'Profile root exists but is not a directory; refusing to continue.' >&2
  exit 73
fi

mkdir -p "$PROFILE_CURSOR_DIR"
chmod 700 "$PROFILE_ROOT" "$PROFILE_CURSOR_DIR"

# Cursor documents these permission expressions. The profile is deny-first:
# a later fixed no-op can answer text but cannot inspect a workspace, mutate a
# file, execute a shell command, fetch web content, or reach MCP. The later
# Bubblewrap launcher must map HOME to PROFILE_ROOT before this config is used.
readonly CONFIG_JSON='{
  "permissions": {
    "allow": [],
    "deny": [
      "Read(**)",
      "Write(**)",
      "Shell(**)",
      "WebFetch(**)",
      "Mcp(**)"
    ]
  }
}'

if [[ -e "$PROFILE_CONFIG" ]]; then
  existing=$(sha256sum "$PROFILE_CONFIG" | awk '{print $1}')
  expected=$(printf '%s\n' "$CONFIG_JSON" | sha256sum | awk '{print $1}')
  if [[ "$existing" != "$expected" ]]; then
    printf '%s\n' 'Existing profile configuration differs from the reviewed deny-first contract; refusing to overwrite it.' >&2
    exit 73
  fi
  printf '%s\n' 'result=ALREADY_PREPARED; reviewed configuration is unchanged.'
  exit 0
fi

printf '%s\n' "$CONFIG_JSON" > "$PROFILE_CONFIG"
chmod 600 "$PROFILE_CONFIG"
printf 'config_sha256=%s\n' "$(sha256sum "$PROFILE_CONFIG" | awk '{print $1}')"
printf '%s\n' 'result=PREPARED_NO_AUTH; browser login and canary remain separately gated.'
