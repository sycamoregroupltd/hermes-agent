#!/usr/bin/env bash
# In-dir cron wrapper for the G-F1 fingerprint lag monitor.
# Hermes cronjob `script` resolver REJECTS symlinks and out-of-dir paths
# (see ~/.hermes/profiles/trading-devops SOUL), so this real in-dir shell
# shim must exec the canonical producer at ~/.hermes/scripts/...
# Canonical: /home/frank/.hermes/scripts/sycode_fingerprint_lag_monitor.py
set -euo pipefail
exec python3 /home/frank/.hermes/scripts/sycode_fingerprint_lag_monitor.py --watchdog
