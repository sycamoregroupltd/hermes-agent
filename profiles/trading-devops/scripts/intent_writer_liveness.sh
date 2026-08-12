#!/usr/bin/env bash
# In-dir cron shim for the intent-writer liveness monitor.
# Hermes cronjob `script` resolver REJECTS symlinks and out-of-dir paths
# (see trading-devops SOUL), so this real in-dir shim must exec the canonical
# producer at ~/.hermes/scripts/...
# Canonical: /home/frank/.hermes/scripts/intent-writer-liveness.sh
# Re-registered 2026-08-11 by kanban t_69905e8a (UDIRF cron restore).
set -euo pipefail
exec /home/frank/.hermes/scripts/intent-writer-liveness.sh "$@"
