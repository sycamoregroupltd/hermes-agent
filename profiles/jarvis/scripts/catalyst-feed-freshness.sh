#!/usr/bin/env bash
# exec shim -> canonical (canonical = regular file in ~/.hermes/scripts; profile copies are
# shims). Restored 2026-08-16T20:06Z after fleet-status/cron list showed MISSING script at
# this profile path while the hourly ingest timer was healthy.
exec /home/frank/.hermes/scripts/catalyst-feed-freshness.sh "$@"
