#!/usr/bin/env bash
# Hourly config-integrity check, routed through report-to-board.py so a finding
# becomes ONE self-closing card on jarvis-os instead of a red cron nobody reads.
# Silent when clean (report-to-board emits nothing on exit 0).
#
# Lives under profiles/jarvis/scripts/ because `hermes cron` runs the PROFILE-LOCAL
# copy, not ~/.hermes/scripts/ — two copies of a script drift and hand-verified
# fixes then ship nothing. The checker itself stays in ~/.hermes/scripts/ and is
# referenced absolutely, so there is only ever one copy of the logic.
set -uo pipefail
export RTB_SCRIPT=/home/frank/.hermes/scripts/config_integrity_run.sh
export RTB_KEY=config-integrity
export RTB_TITLE="Hermes config changed outside any gate (script/curator write, or a guard died)"
export RTB_BOARD=jarvis-os
exec /usr/bin/env python3 /home/frank/.hermes/scripts/report-to-board.py
