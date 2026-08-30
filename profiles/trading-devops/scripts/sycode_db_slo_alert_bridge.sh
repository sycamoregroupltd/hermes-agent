#!/usr/bin/env bash
# SHIM — approved exec wrapper. Canonical source is
# ~/sycode-trading/monitoring/scripts/sycode_db_slo_alert_bridge.py
set -uo pipefail
CANONICAL="/home/frank/sycode-trading/monitoring/scripts/sycode_db_slo_alert_bridge.py"
exec python3 "$CANONICAL"
