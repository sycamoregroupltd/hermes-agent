#!/usr/bin/env bash
# Fusion Engine Calibration Report cron wrapper
# Runs the Python report script and delivers stdout to discord:#quant-reports
set -euo pipefail
# Deprecated v1 direct-run removed 2026-07-11 (t_f7bbb9ab): running
# fusion_calibration_report.py directly re-emits the stale n=16 LOW-CONFIDENCE
# false-positive (pre JOIN-fix + pre Tier-2 merge). All fusion calibration
# reporting now routes through the audited, pinned repo wrapper below.
exec /home/frank/sycode-trading/execution/run_fusion_calibration_report.sh
