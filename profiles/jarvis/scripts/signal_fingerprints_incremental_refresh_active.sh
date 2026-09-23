#!/usr/bin/env bash
# Profile-local exec-shim for signal-fingerprints-incremental-refresh.
# Rehomed from paused jarvis-os-pm store onto the live jarvis ticker
# (jarvis-os/t_76deccce). Regular file, not a symlink — scheduler_script
# resolves relative scripts only under this profile's scripts/ directory.
set -euo pipefail
exec /home/frank/.hermes/scripts/signal_fingerprints_incremental_refresh_active.sh "$@"
