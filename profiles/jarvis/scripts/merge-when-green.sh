#!/usr/bin/env bash
# Profile-local exec shim (hermes cron resolves profile-locally first).
exec /home/frank/.hermes/scripts/merge-when-green.sh "$@"
