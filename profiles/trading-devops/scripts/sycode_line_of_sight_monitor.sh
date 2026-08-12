#!/usr/bin/env bash
# In-dir cron shim for the sycode line-of-sight (lineage) monitor.
# Hermes cronjob `script` resolver REJECTS symlinks and out-of-dir paths
# (see trading-devops SOUL), so this real in-dir shim must exec the canonical
# producer at ~/.hermes/scripts/...
# Canonical: /home/frank/.hermes/scripts/sycode_line_of_sight_monitor.py
# Re-registered 2026-08-11 by kanban t_69905e8a (UDIRF cron restore).
set -euo pipefail
exec python3 /home/frank/.hermes/scripts/sycode_line_of_sight_monitor.py "$@"
