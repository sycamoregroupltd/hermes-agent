#!/usr/bin/env bash
# jarvis-hosted no-agent cron wrapper for the read-only Hermes docs drift watcher.
# Migrated from jarvis-os-pm by t_86c82617 so Discord delivery uses the live Jarvis gateway config.
set -euo pipefail

exec /usr/bin/env python3 /home/frank/.hermes/profiles/jarvis-os-pm/scripts/hermes_docs_drift_watcher.py \
  --url https://hermes-agent.nousresearch.com/docs/llms.txt \
  --state-dir /home/frank/.hermes/profiles/jarvis-os-pm/state/hermes-docs-drift \
  --min-interval-seconds 21600 \
  "$@"
