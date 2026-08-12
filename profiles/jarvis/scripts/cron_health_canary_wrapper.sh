#!/usr/bin/env bash
# Profile-local exec shim (SHIM pattern — see fleet memory: cron scheduler
# resolves `script` to the profile-local scripts dir, so jobs referencing
# canonical scripts MUST have a profile-local exec shim redirecting here).
# t_634a8026 cron-health canary wrapper.
exec /home/frank/.hermes/scripts/cron_health_canary_wrapper.sh "$@"
