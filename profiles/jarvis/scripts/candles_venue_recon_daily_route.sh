#!/bin/sh
# CANONICAL SOURCE — jarvis-profile in-dir wrapper for cron job 6b944f2cfd19
# (candles-venue-recon-daily, t_2c60ff8d). Hermes cron rejects symlinks and
# out-of-dir script paths, so this real wrapper execs the shared canonical
# route entrypoint at ~/.hermes/scripts/. Cadence: 40 2 * * * (02:40 UTC
# daily). Consumer: sycode-trading kanban incident route
# (idempotency_key=sycode-residual-6b944f2cfd19). Healthy ticks silent;
# breach/operational-failure fail-visible.
set -eu
exec python3 "$HOME/.hermes/scripts/sycode_residual_monitor_route.py" --monitor candles-venue-recon-daily "$@"
