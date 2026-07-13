#!/usr/bin/env bash
# Cross-exchange funding rate collector cron wrapper.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="/home/frank/.hermes/venvs/trading-ml/bin/activate"
COLLECTOR="/home/frank/sycode-trading/execution/funding_rate_collector.py"

source "$VENV"
exec python3 "$COLLECTOR" --max-workers 3
