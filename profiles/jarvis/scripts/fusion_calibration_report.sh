#!/usr/bin/env bash
# CANONICAL-COPY RULE: keep logic in /home/frank/.hermes/scripts/fusion_calibration_report.sh.
# This profile-local shim exists because Hermes cron resolves --script under the running profile's scripts/ dir.
exec /usr/bin/env bash /home/frank/.hermes/scripts/fusion_calibration_report.sh "$@"
