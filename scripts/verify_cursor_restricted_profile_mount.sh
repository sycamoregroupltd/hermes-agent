#!/usr/bin/env bash
# Verify the filesystem boundary used by the restricted Cursor profile.
#
# This is deliberately a mount proof only. It runs no Cursor executable, sends
# no provider request, starts no browser login, and keeps networking unshared.
# A later login/canary launcher must be separately reviewed before it may use
# this profile with a network connection.

set -euo pipefail

readonly PROFILE_ROOT="/home/frank/.hermes/provider-profiles/cursor-readonly-v1"
readonly PROFILE_CONFIG="$PROFILE_ROOT/.cursor/cli-config.json"

usage() {
  cat <<'USAGE'
Usage: verify_cursor_restricted_profile_mount.sh [--verify]

This validates a Bubblewrap namespace in which:
  * HOME is the dedicated Cursor profile;
  * the normal /home/frank/.cursor path does not exist;
  * the reviewed profile config is readable;
  * the workspace is an empty tmpfs;
  * networking is unshared.

It never invokes Cursor, authenticates, starts a browser, sends a provider
request, changes Hermes routing, or enables a worker.
USAGE
}

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--verify" && "$1" != "--help" ) ]]; then
  usage >&2
  exit 64
fi

if [[ ${1:-} == "--help" ]]; then
  usage
  exit 0
fi

if ! command -v bwrap >/dev/null 2>&1; then
  printf '%s\n' 'Bubblewrap (bwrap) is not installed; refusing boundary verification.' >&2
  exit 69
fi

if [[ ! -r "$PROFILE_CONFIG" ]]; then
  printf '%s\n' 'Reviewed restricted Cursor profile is not prepared; refusing boundary verification.' >&2
  exit 69
fi

if [[ ${1:-} != "--verify" ]]; then
  printf '%s\n' 'result=DRY_RUN; pass --verify to run the local no-network mount proof.'
  exit 0
fi

readonly SANDBOX_HOME="/profile"
readonly SANDBOX_WORKSPACE="/workspace"

# Minimal runtime mounts for /bin/sh. No host home directory, existing Cursor
# profile, agent runtime, plugins, MCP configuration, browser state, or network
# is mounted in this verifier.
readonly BWRAP_RUNTIME_MOUNTS=(
  --ro-bind /usr /usr
  --ro-bind /bin /bin
  --ro-bind /lib /lib
  --ro-bind /etc /etc
)
if [[ -d /lib64 ]]; then
  BWRAP_RUNTIME_MOUNTS+=(--ro-bind /lib64 /lib64)
fi

bwrap \
  --die-with-parent \
  --new-session \
  --unshare-all \
  "${BWRAP_RUNTIME_MOUNTS[@]}" \
  --proc /proc \
  --dev /dev \
  --bind "$PROFILE_ROOT" "$SANDBOX_HOME" \
  --tmpfs "$SANDBOX_WORKSPACE" \
  --setenv HOME "$SANDBOX_HOME" \
  --setenv XDG_CONFIG_HOME "$SANDBOX_HOME/.config" \
  --setenv XDG_CACHE_HOME "$SANDBOX_HOME/.cache" \
  --setenv XDG_DATA_HOME "$SANDBOX_HOME/.local/share" \
  --chdir "$SANDBOX_WORKSPACE" \
  /bin/sh -eu -c '
    test "$HOME" = /profile
    test -r "$HOME/.cursor/cli-config.json"
    test ! -e /home/frank/.cursor
    test "$(pwd)" = /workspace
    test -d /workspace
    test ! -e /workspace/.git
    printf "%s\n" "CURSOR_RESTRICTED_MOUNT_PROOF=PASS"
  '
