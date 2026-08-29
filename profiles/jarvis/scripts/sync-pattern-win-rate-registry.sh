#!/usr/bin/env bash
# Profile-local shim for sync-pattern-win-rate-registry — migrated from
# sycode-trading-pm gateway to jarvis scheduler (t_cbdd35f4, 2026-08-29).
# Execs the canonical global script so there is exactly one source of truth.
set -euo pipefail
exec /home/frank/.hermes/scripts/sync-pattern-win-rate-registry.sh "$@"
