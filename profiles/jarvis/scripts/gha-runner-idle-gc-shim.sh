#!/usr/bin/env bash
# Auto-generated shim: routes 'gha-runner-idle-gc' to the canonical script.
# Canonical logic lives at /home/frank/.hermes/scripts/gha-runner-idle-gc.sh;
# do not edit this shim's logic, only the canonical copy.
set -uo pipefail
export GC_MODE="${GC_MODE:-APPLY}"
export GC_MIN_AGE_SECS="${GC_MIN_AGE_SECS:-3600}"
exec bash /home/frank/.hermes/scripts/gha-runner-idle-gc.sh
