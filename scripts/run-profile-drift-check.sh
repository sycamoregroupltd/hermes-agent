#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
set -euo pipefail
exec /usr/bin/env python3 /home/frank/uaa-rules/profile_drift_check.py --file-cards --board jarvis-os
