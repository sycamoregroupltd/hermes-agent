#!/usr/bin/env bash
# In-dir cron shim for elon-weekly-self-improve (t_bf9e633f).
# Hermes cron `script` resolver REJECTS symlinks and out-of-dir paths, so this
# REAL file must live under the trading-devops scripts directory and exec the
# canonical producer.
set -euo pipefail
export PATH="$HOME/.local/bin:/home/frank/.local/bin:/usr/local/bin:/usr/bin:/bin"
exec python3 /home/frank/.hermes/scripts/elon_weekly_self_improve_mint.py "$@"
