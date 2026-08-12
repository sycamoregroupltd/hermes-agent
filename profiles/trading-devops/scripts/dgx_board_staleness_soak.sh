#!/usr/bin/env bash
# Soak wrapper for the DGX board-staleness DISPATCH GAP probe.
# The Hermes cron runner rejects symlinks and any path outside this scripts
# dir, so instead of symlinking we exec the canonical probe from here. This
# keeps the probe logic single-sourced at /home/frank/.hermes/scripts/
# (edit that canonical file; do NOT edit this wrapper's behavior).
exec python3 /home/frank/.hermes/scripts/dgx_board_sweep_staleness.py "$@"
