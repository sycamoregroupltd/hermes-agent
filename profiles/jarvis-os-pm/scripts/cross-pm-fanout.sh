#!/usr/bin/env bash
# SHIM — approved exec wrapper. Canonical source is ~/.hermes/scripts/cross-pm-fanout.sh.
# CANONICAL-COPY RULE (t_65a992ed): this profile-local cron script is an exec shim only.
# Scheduler resolves scripts under $HERMES_HOME/scripts; canonical implementation lives at:
# /home/frank/.hermes/scripts/cross-pm-fanout.sh
# Edit the canonical file above, not this wrapper. Drift watch alerts if this wrapper stops pointing there.
set -euo pipefail
exec bash '/home/frank/.hermes/scripts/cross-pm-fanout.sh' "$@"
