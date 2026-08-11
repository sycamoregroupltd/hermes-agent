#!/bin/bash
# ============================================================================
# cron-data-processor.sh — Template for no_agent=true cron data processing
#
# Purpose: Process structured data (JSON, CSV, SQLite) for cron jobs that
#          need Python-level logic without the LLM session approval surface.
#
# Usage in cron job creation:
#   cronjob(action='create', name='my-data-job', schedule='every 5m',
#           no_agent=true, script='cron-data-processor.sh',
#           deliver='local')
#
# Chaining to an LLM job:
#   cronjob(action='create', name='my-decision-job', schedule='every 5m',
#           context_from=['<data-job-id>'],
#           prompt='Process the data from context and decide...',
#           deliver='discord:...')
#
# Output convention: Print compact JSON to stdout. The output is captured
# as the job result and available to downstream jobs via context_from.
# If there is nothing to report, exit 0 with empty stdout (silent pattern).
# ============================================================================

set -euo pipefail

# --- Configuration ---
# Override these in the heredoc below, or pass via env vars in cronjob config.

# --- Python data processing block ---
# All data processing goes here. Use Python for JSON, SQLite, CSV, etc.
# This runs as a standalone script — no LLM session overhead, no approval guards.
python3 <<'PYEOF'
import json
import sys

# ── Example: Read and process a JSON file ──────────────────────────────
# Example: process hermes kanban data from SQLite
# import sqlite3
# conn = sqlite3.connect("/home/frank/.hermes/kanban/boards/jarvis-os/kanban.db")
# cursor = conn.cursor()
# cursor.execute("SELECT id, title, status FROM tasks WHERE status='ready'")
# rows = cursor.fetchall()
# conn.close()
# result = {"ready_count": len(rows), "tasks": [{"id": r[0], "title": r[1]} for r in rows]}

# ── Example: Parse JSON from stdin ────────────────────────────────────
# data = json.load(sys.stdin)
# result = {"count": len(data)}

# ── Default: no-op (replace this block) ────────────────────────────────
result = {"status": "noop", "message": "Replace this script with actual processing logic"}

# ── Output ─────────────────────────────────────────────────────────────
print(json.dumps(result))
PYEOF

# --- Exit cleanly ---
# Exit code 0 = success. Non-zero = error (logged in cronjob last_status).
# Empty stdout = silent delivery (no message sent to user).
exit 0
