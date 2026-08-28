#!/bin/sh
# CANONICAL SOURCE — residual consumer wrapper for job 45e0b154b41c (t_dd27733b).
# Cadence: */30 * * * *. Consumer: sycode-trading kanban incident route.
# Healthy ticks silent. Breach/ops failure fail-visible.
set -eu
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/sycode_residual_monitor_route.py" --monitor candle-per-symbol-freshness "$@"
