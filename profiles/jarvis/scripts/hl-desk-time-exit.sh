#!/usr/bin/env bash
# exec shim -> canonical (canonical = regular file in ~/.hermes/scripts; profile copies are
# shims). Restored 2026-08-15 ~22:33Z by fable-desk-head after fable-carry caught the cron
# resolving to this missing path — the time-exit had NEVER run.
exec /home/frank/.hermes/scripts/hl-desk-time-exit.sh "$@"
