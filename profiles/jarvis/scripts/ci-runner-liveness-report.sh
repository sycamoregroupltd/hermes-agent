#!/usr/bin/env bash
# Profile-local shim. Hermes cron resolves script names profile-locally FIRST, so a
# job naming a global-only script fails every run (canonical-copy rule t_7fec9a7c).
export RTB_SCRIPT=/home/frank/.hermes/scripts/ci-runner-liveness.sh
export RTB_KEY=ci-runner-liveness
export RTB_TITLE="CI runners degraded — required check cannot run"
export RTB_BOARD=sycode-trading
exec /usr/bin/env python3 /home/frank/.hermes/scripts/report-to-board.py
