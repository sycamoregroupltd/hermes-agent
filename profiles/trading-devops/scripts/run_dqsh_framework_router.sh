#!/usr/bin/env bash
# In-dir cron wrapper for the DQSH Data-Integrity Kanban Router (t_acb010ea).
#
# Hermes cronjob `script` resolver REJECTS symlinks and out-of-dir paths, so
# this real in-dir shell shim execs the canonical framework directly.
# Canonical producer: /home/frank/.hermes/scripts/sycode_data_quality_framework.py
#
# Board pin (systemic fix t_4f419b25): diag cards are created on the jarvis-os
# board and every dedup lookup decodes the board from the idempotency key
# (diag:<board>:<type>:<metric>), so creation + dedup can never diverge.
#
# Routing/evidence only: this lane is the kanban-routing + observability layer
# of the DQSH family. Self-healing remediation stays with the DQSH daemon cron
# (c7226b0fbbe5, run_dqsh.sh, 15m) so a new 30m schedule does not double-run
# mutating remediation scripts.
#
# Exit-code policy: the framework exits 2 when breaches were found and routed
# (a HANDLED condition — the kanban card IS the alert). Convert 2 -> 0 so the
# cron health layer does not flag every expected breach cycle as failed, while
# still propagating true operational failures (exit 1).
set -uo pipefail
export HERMES_KANBAN_BOARD=jarvis-os
python3 /home/frank/.hermes/scripts/sycode_data_quality_framework.py
rc=$?
if [ "$rc" -eq 2 ]; then
  exit 0
fi
exit "$rc"
