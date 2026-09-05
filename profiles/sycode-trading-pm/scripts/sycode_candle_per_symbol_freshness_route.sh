#!/bin/sh
# Profile-local cron shim; canonical source is the fleet scripts directory.
set -eu
exec /bin/sh /home/frank/.hermes/scripts/sycode_candle_per_symbol_freshness_route.sh "$@"
