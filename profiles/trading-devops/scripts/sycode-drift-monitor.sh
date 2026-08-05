#!/bin/bash
# ============================================================================
# Drift Monitor Cron Wrapper — for cronwatch no_agent=True pattern
# ============================================================================
# Extracts DATABASE_URL from the server .env safely (line-by-line, ignoring
# lines that can't be sourced), sets MLflow tracking URI for host access,
# then runs the drift monitor in quiet (alerts-only) mode.
#
# Quiet mode: only outputs content when there's actionable info
# (drift alerts, errors). Silent when healthy.
# ============================================================================
set -euo pipefail

ENV_FILE="$HOME/sycode-trading/server/.env"
SCRIPT_DIR="$HOME/sycode-trading/tools/drift-monitor"

# Extract DATABASE_URL from .env safely — skip lines with special chars
if [ -f "$ENV_FILE" ]; then
    DB_LINE=$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1)
    if [ -n "$DB_LINE" ]; then
        export "${DB_LINE}"
    fi
fi

# Override MLflow tracking URI to host-accessible address (Docker maps 5051->5000)
export MLFLOW_TRACKING_URI="http://localhost:5051"

# Run the drift monitor in quiet mode
TIMESTAMP=$(date -u '+%Y%m%d_%H%M%S')
REPORT_DIR="${SCRIPT_DIR}/reports"
mkdir -p "${REPORT_DIR}"
DRIFT_REPORT="${REPORT_DIR}/drift_${TIMESTAMP}.json"

exec python3 "${SCRIPT_DIR}/drift_monitor.py" \
    --quiet \
    --output "${DRIFT_REPORT}" \
    2>&1
