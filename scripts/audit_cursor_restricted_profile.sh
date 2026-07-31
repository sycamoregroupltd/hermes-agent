#!/usr/bin/env bash
# Audit the restricted Cursor profile after account-holder authentication.
#
# This audit deliberately reads no credential contents and invokes no Cursor
# command. It verifies only filesystem shape, modes, and the deny-first config
# before any later disposable provider canary is considered.

set -euo pipefail

readonly PROFILE_ROOT="/home/frank/.hermes/provider-profiles/cursor-readonly-v1"
readonly PROFILE_CURSOR_DIR="$PROFILE_ROOT/.cursor"
readonly PROFILE_CONFIG="$PROFILE_CURSOR_DIR/cli-config.json"

usage() {
  cat <<'USAGE'
Usage: audit_cursor_restricted_profile.sh [--audit]

Without --audit this command is a dry-run. --audit performs only local
filesystem/config checks. It does not call Cursor, inspect token contents,
send a network request, start a model, route a Hermes task, or enable a worker.

The audit intentionally rejects MCP, plugin, project, chat, and worktree
artifacts in the restricted profile. It reports aggregate file counts only,
never filenames or credential values.
USAGE
}

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--audit" && "$1" != "--help" ) ]]; then
  usage >&2
  exit 64
fi

if [[ ${1:-} == "--help" ]]; then
  usage
  exit 0
fi

if [[ ${1:-} != "--audit" ]]; then
  printf 'profile_root=%s\n' "$PROFILE_ROOT"
  printf '%s\n' 'provider_calls=0'
  printf '%s\n' 'credential_contents_read=0'
  printf '%s\n' 'result=DRY_RUN; --audit performs local profile checks only.'
  exit 0
fi

test -d "$PROFILE_ROOT"
test -d "$PROFILE_CURSOR_DIR"
test -f "$PROFILE_CONFIG"
test "$(stat -c %a "$PROFILE_ROOT")" = 700
test "$(stat -c %a "$PROFILE_CURSOR_DIR")" = 700
test "$(stat -c %a "$PROFILE_CONFIG")" = 600

python3 - "$PROFILE_CONFIG" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    value = json.load(handle)
expected = {
    "permissions": {
        "allow": [],
        "deny": ["Read(**)", "Write(**)", "Shell(**)", "WebFetch(**)", "Mcp(**)"],
    }
}
if value != expected:
    raise SystemExit("restricted Cursor deny-first configuration drifted")
PY

for forbidden in \
  "$PROFILE_CURSOR_DIR/mcp.json" \
  "$PROFILE_CURSOR_DIR/plugins" \
  "$PROFILE_CURSOR_DIR/projects" \
  "$PROFILE_CURSOR_DIR/chats" \
  "$PROFILE_CURSOR_DIR/worktrees"; do
  if [[ -e "$forbidden" ]]; then
    printf '%s\n' 'Restricted profile contains a forbidden capability/state category.' >&2
    exit 73
  fi
done

if find "$PROFILE_ROOT" -xdev -type f ! -perm 600 -print -quit | grep -q .; then
  printf '%s\n' 'Restricted profile contains a file that is not owner-private.' >&2
  exit 73
fi

if find "$PROFILE_ROOT" -xdev -type d ! -perm 700 -print -quit | grep -q .; then
  printf '%s\n' 'Restricted profile contains a directory that is not owner-private.' >&2
  exit 73
fi

file_count=$(find "$PROFILE_ROOT" -xdev -type f -printf . | wc -c)
dir_count=$(find "$PROFILE_ROOT" -xdev -type d -printf . | wc -c)
printf 'profile_file_count=%s\n' "$file_count"
printf 'profile_directory_count=%s\n' "$dir_count"
printf '%s\n' 'CURSOR_RESTRICTED_PROFILE_AUDIT=PASS'
