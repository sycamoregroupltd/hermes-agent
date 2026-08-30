#!/usr/bin/env bash
# SHIM — approved exec wrapper. Canonical source is
# ~/sycode-trading/monitoring/scripts/sycode_db_latency_slo_collector.py
set -uo pipefail
CANONICAL="/home/frank/sycode-trading/monitoring/scripts/sycode_db_latency_slo_collector.py"
exec python3 "$CANONICAL"
