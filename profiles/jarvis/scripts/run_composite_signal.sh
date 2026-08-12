#!/usr/bin/env bash
# run_composite_signal.sh — Regime composite signal (no_agent cron, every 15m)
#
# Cron job 79b3e3471877 (regime-signal) runs this script every 15 minutes.
# Produces stdout delivered back as the cron report.
#
# Current implementation: runs the canonical macro_regime_adaptor.py as the
# primary signal component. Future extensions should aggregate additional
# signal sources (technical, sentiment, on-chain) into the composite output.
#
# Canonical copy rule: this file is the cron-execution entry point.
# The regime classification logic lives in ~/.hermes/scripts/macro_regime_adaptor.py

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CANONICAL="/home/frank/.hermes/scripts/macro_regime_adaptor.py"
TIMESTAMP=$(date -Iseconds)

echo "================================================"
echo " Composite Signal Report  —  ${TIMESTAMP}"
echo "================================================"
echo ""

# --- Composite stages (extend here as new signals are added) ---
# Stage 1: Macro Regime Classification
echo "--- Stage 1/1: Macro Regime ---"
if [ -x "${CANONICAL}" ]; then
    python3 "${CANONICAL}" 2>&1
    REGIME_EXIT=$?
    echo ""
    if [ ${REGIME_EXIT} -ne 0 ]; then
        echo "[WARN] Macro regime adaptor exited with code ${REGIME_EXIT}"
    fi
else
    echo "[ERROR] Canonical macro_regime_adaptor.py not found at: ${CANONICAL}"
    echo "[ERROR] Regime classification unavailable until the canonical script is restored."
    echo ""
fi

echo "================================================"
echo " Composite Signal Report  —  END — ${TIMESTAMP}"
echo "================================================"
