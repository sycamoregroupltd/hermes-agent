#!/bin/bash
# CANONICAL SOURCE — profile-local copy at profiles/jarvis/scripts/ (keep identical).
# Enables the selective-dispatch consumer path in fleet-dispatch.sh (v2a activation
# 2026-08-10, reviewed+approved by fable seat — pairs with codex_breaker_selective_wrapper.sh;
# without this flag the resumed loop dispatches in NORMAL mode during codex exhaustion,
# which can spawn codex-routed workers into a dead provider).
# Rollback: hermes cron edit a9def8c365df --script fleet-dispatch.sh
export FLEET_SELECTIVE_DISPATCH_ENABLED=1
exec /bin/bash /home/frank/.hermes/profiles/jarvis/scripts/fleet-dispatch.sh "${1:-4}"
