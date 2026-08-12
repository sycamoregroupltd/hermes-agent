#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# Silent-when-ok disk watchdog for DGX data volume.
set -uo pipefail

TARGET="${DGX_DISK_TARGET:-/home/frank}"
WARN_GB="${DGX_DISK_WARN_GB:-300}"
CRIT_GB="${DGX_DISK_CRIT_GB:-150}"

line=$(df -BG "$TARGET" 2>/dev/null | awk 'NR==2 {print $1, $4, $5, $6}') || {
  echo "🔴 DGX DISK: df failed for $TARGET"
  exit 0
}
set -- $line
fs="$1"; avail_raw="$2"; usep="$3"; mount="$4"
avail_gb="${avail_raw%G}"

if [ -z "$avail_gb" ]; then
  echo "🔴 DGX DISK: could not parse df output for $TARGET: $line"
  exit 0
fi

if [ "$avail_gb" -lt "$CRIT_GB" ]; then
  echo "🔴 DGX DISK CRITICAL: ${avail_gb}G free on $mount ($fs, $usep used); threshold <${CRIT_GB}G"
elif [ "$avail_gb" -lt "$WARN_GB" ]; then
  echo "⚠️ DGX DISK WARNING: ${avail_gb}G free on $mount ($fs, $usep used); threshold <${WARN_GB}G"
fi
