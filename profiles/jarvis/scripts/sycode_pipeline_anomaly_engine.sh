#!/usr/bin/env bash
# In-dir cron shim for the SycodeTrading Real-Time Pipeline Anomaly Detection
# Engine (t_fa200457). Hermes cronjob `script` resolver REJECTS symlinks and
# out-of-dir paths, so this real in-dir shim must exec the canonical engine
# living in the trading-data-oracle profile scripts dir. Canonical producer:
#   /home/frank/.hermes/profiles/trading-data-oracle/scripts/sycode_pipeline_anomaly_engine.py
set -euo pipefail
exec python3 /home/frank/.hermes/profiles/trading-data-oracle/scripts/sycode_pipeline_anomaly_engine.py "$@"
