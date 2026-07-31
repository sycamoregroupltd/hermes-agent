#!/usr/bin/env bash
# Launch the one-time, account-holder-owned Cursor browser login in isolation.
#
# This is not a Hermes worker or provider adapter. It has no model invocation,
# task routing, session resume, API-key input, plugin loading, MCP approval, or
# automatic browser control. The sole --login action runs Cursor's login command
# with browser opening suppressed so the account holder can open the displayed
# URL in their normal trusted browser and complete the provider-owned OAuth flow.

set -euo pipefail

readonly PROFILE_ROOT="/home/frank/.hermes/provider-profiles/cursor-readonly-v1"
readonly PROFILE_CONFIG="$PROFILE_ROOT/.cursor/cli-config.json"
readonly CURSOR_RUNTIME_ROOT="/home/frank/.local/share/cursor-agent"
readonly SANDBOX_PROFILE="/profile"
readonly SANDBOX_RUNTIME="/opt/cursor-agent"
readonly SANDBOX_WORKSPACE="/workspace"

usage() {
  cat <<'USAGE'
Usage: launch_cursor_restricted_login.sh [--login]

Without --login this command is a dry-run. --login starts only Cursor's
provider-owned interactive authentication flow inside the reviewed profile.

The account holder must complete the URL shown by Cursor in their normal,
trusted browser. This launcher deliberately has no API-key option and never
opens, reads, copies, writes, or mounts the normal ~/.cursor profile.

This does not admit Cursor to Hermes, invoke a model, create a worker, enable
routing, or authorize any later provider canary.
USAGE
}

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--login" && "$1" != "--help" ) ]]; then
  usage >&2
  exit 64
fi

if [[ ${1:-} == "--help" ]]; then
  usage
  exit 0
fi

if ! command -v bwrap >/dev/null 2>&1; then
  printf '%s\n' 'Bubblewrap (bwrap) is not installed; refusing login launch.' >&2
  exit 69
fi

if [[ ! -r "$PROFILE_CONFIG" ]]; then
  printf '%s\n' 'Reviewed restricted Cursor profile is not prepared; refusing login launch.' >&2
  exit 69
fi

agent_source=$(readlink -f "$(command -v agent 2>/dev/null || true)")
case "$agent_source" in
  "$CURSOR_RUNTIME_ROOT"/versions/*/cursor-agent) ;;
  *)
    printf '%s\n' 'Cursor Agent executable is not in the reviewed Cursor runtime root; refusing login launch.' >&2
    exit 69
    ;;
esac

if [[ ${1:-} != "--login" ]]; then
  printf 'profile_root=%s\n' "$PROFILE_ROOT"
  printf 'agent_source=%s\n' "$agent_source"
  printf '%s\n' 'normal_profile=unmounted'
  printf '%s\n' 'provider_calls=0'
  printf '%s\n' 'result=DRY_RUN; --login is account-holder-only OAuth, not worker activation.'
  exit 0
fi

# The runtime mount contains Cursor's executable only. The profile bind is the
# sole writable host path. --share-net is required for Cursor's own OAuth
# exchange; it does not expose a host browser or the normal user home.
readonly agent_relative="${agent_source#"$CURSOR_RUNTIME_ROOT"}"
readonly sandbox_agent="$SANDBOX_RUNTIME$agent_relative"

runtime_mounts=(
  --ro-bind /usr /usr
  --ro-bind /bin /bin
  --ro-bind /lib /lib
  --ro-bind /etc /etc
)
if [[ -d /lib64 ]]; then
  runtime_mounts+=(--ro-bind /lib64 /lib64)
fi

# Cursor creates profile-owned cache/config directories at startup.  Keep every
# such file private to Frank from the first write, including a later OAuth token.
umask 077

exec bwrap \
  --die-with-parent \
  --new-session \
  --unshare-all \
  --share-net \
  "${runtime_mounts[@]}" \
  --ro-bind "$CURSOR_RUNTIME_ROOT" "$SANDBOX_RUNTIME" \
  --bind "$PROFILE_ROOT" "$SANDBOX_PROFILE" \
  --tmpfs "$SANDBOX_WORKSPACE" \
  --tmpfs /tmp \
  --proc /proc \
  --dev /dev \
  --unsetenv CURSOR_API_KEY \
  --unsetenv AGENT_CLI_CREDENTIAL_STORE \
  --setenv HOME "$SANDBOX_PROFILE" \
  --setenv XDG_CONFIG_HOME "$SANDBOX_PROFILE/.config" \
  --setenv XDG_CACHE_HOME "$SANDBOX_PROFILE/.cache" \
  --setenv XDG_DATA_HOME "$SANDBOX_PROFILE/.local/share" \
  --setenv NO_OPEN_BROWSER 1 \
  --chdir "$SANDBOX_WORKSPACE" \
  "$sandbox_agent" login
