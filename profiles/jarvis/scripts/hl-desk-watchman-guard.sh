#!/usr/bin/env bash
# exec shim -> canonical (rule: canonical = regular file in ~/.hermes/scripts,
# profile copies are shims, never symlinks). Restored 2026-08-15 by fable-desk-head
# after cron validator flagged MISSING at this path.
exec /home/frank/.hermes/scripts/hl-desk-watchman-guard.sh "$@"
