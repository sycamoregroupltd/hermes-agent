#!/usr/bin/env bash
# CANONICAL-COPY RULE: keep logic in /home/frank/.hermes/scripts/board_pm_triage_visibility.py.
# This profile-local shim exists because Hermes cron resolves --script under the running profile's scripts/ dir.
exec /usr/bin/env python3 /home/frank/.hermes/scripts/board_pm_triage_visibility.py \
  --board yorkstone-supplies \
  --pm-profile yorkstone-supplies-pm \
  --source jarvis:board-pm-triage-yorkstone-supplies
