#!/usr/bin/env bash
# Wrapper so the trading-devops cron job (c7643a8a45c2) can run the python monitor
set -uo pipefail
exec python3 /home/frank/.hermes/scripts/sycode_fingerprint_lag_monitor.py "$@"
