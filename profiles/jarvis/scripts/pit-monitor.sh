#!/usr/bin/env bash
# CANONICAL-COPY RULE: Jarvis cron exec shim for the Sycode-Trading PIT monitor.
# Canonical implementation lives in sycode-trading-pm profile scripts because the original
# implementation task t_f8c1e76e installed it there. This shim lets the active Jarvis
# gateway/ticker run the watchdog instead of the non-ticking trading-devops cron store.
exec /home/frank/.hermes/profiles/sycode-trading-pm/scripts/pit-monitor.sh "$@"
