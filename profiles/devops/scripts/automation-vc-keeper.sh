#!/usr/bin/env bash
# Durable dispatcher for the automation-vc keeper.
#
# This wrapper is intentionally thin and is TRACKED on branch fleet/automation-vc
# (committed by automation_vc_keeper.py via FORCE_INCLUDE). On DGX rebuild, the
# recovery runbook rsyncs both ~/.hermes/scripts/ (the real keeper) and
# ~/.hermes/profiles/devops/scripts/ (this wrapper), so the devops crontab that
# points at this script keeps working. It only delegates to the absolute tracked
# root script; it holds no secrets and no logic of its own.
set -euo pipefail
cd /home/frank/.hermes
exec /home/frank/.hermes/scripts/automation_vc_keeper.py --message "chore(automation-vc): scheduled keeper sync"
