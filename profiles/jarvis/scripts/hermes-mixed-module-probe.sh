#!/usr/bin/env bash
# Profile-local guard-bundle adapter for the shared read-only mixed-module probe.
set -euo pipefail
exec /home/frank/.hermes/scripts/hermes-mixed-module-probe.sh "$@"
