#!/bin/bash
# CANONICAL SOURCE — profile-local copy at profiles/jarvis/scripts/ (keep identical).
# Activates codex-exhaustion selective dispatch v2a (implementation review APPROVED
# by fable seat 2026-08-10 — reviewed nous_spawnable fail-closed exclusions, TTL
# enforcement at consumption, allowlist schema validation, and board-level
# selective_skip_frontier in fleet-dispatch.sh; evidence in vault note
# research/2026-08-10-orthogonal... and RESHAPE plan of record).
# Rollback: hermes cron edit 112dbf08692a --script codex_exhaustion_circuit_breaker.py
export CODEX_SELECTIVE_DISPATCH_ENABLED=1
exec /usr/bin/python3 /home/frank/.hermes/profiles/jarvis/scripts/codex_exhaustion_circuit_breaker.py "$@"
