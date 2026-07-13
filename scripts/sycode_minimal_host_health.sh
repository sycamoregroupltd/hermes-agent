#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# Replacement for removed sycode_health_monitor.sh host-crontab line.
# 2026-07-03 t_431c3ed8: minimal docker-health + disk check; silent when ok.
set -uo pipefail

bad=""
if command -v docker >/dev/null 2>&1; then
  unhealthy=$(docker ps --filter health=unhealthy --format '{{.Names}}={{.Status}}' 2>/dev/null || true)
  [ -n "$unhealthy" ] && bad="${bad}UNHEALTHY_CONTAINERS ${unhealthy}
"
else
  bad="${bad}docker command not found
"
fi

disk_out=$(DGX_DISK_WARN_GB="${DGX_DISK_WARN_GB:-300}" DGX_DISK_CRIT_GB="${DGX_DISK_CRIT_GB:-150}" /home/frank/.hermes/scripts/dgx_disk_space_watchdog.sh 2>/dev/null || true)
[ -n "$disk_out" ] && bad="${bad}${disk_out}
"

if [ -n "$bad" ]; then
  printf '🔴 SYCODE HOST HEALTH: minimal docker/disk watchdog fired\n%s' "$bad"
fi
