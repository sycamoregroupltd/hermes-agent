#!/usr/bin/env bash
# In-dir cron shim for the signal-journey censor defect watchdog
# (t_f379ecf5). Hermes cronjob `script` resolver REJECTS symlinks and
# out-of-dir paths (see trading-devops SOUL), so this real in-dir shim
# must exec the canonical monitor living in the trading-data-oracle profile
# scripts dir. Canonical producer:
#   /home/frank/.hermes/profiles/trading-data-oracle/scripts/sycode_signal_journey_censor_monitor.py
set -euo pipefail
exec python3 /home/frank/.hermes/profiles/trading-data-oracle/scripts/sycode_signal_journey_censor_monitor.py "$@"
