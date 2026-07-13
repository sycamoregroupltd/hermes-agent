#!/usr/bin/env bash
# Fleet Monitor — reads both sycode-trading and jarvis-os kanban boards
# Output is injected as context into the cron agent prompt
set -euo pipefail

KANBAN_HOME="${HOME}/.hermes/kanban/boards"
CHECKPOINT_DIR="${HOME}/.hermes/profiles/devops"
CHECKPOINT_FILE="${CHECKPOINT_DIR}/fleet-monitor-checkpoint.txt"
NOW=$(date +%s)

mkdir -p "${CHECKPOINT_DIR}"

echo "=== FLEET MONITOR — $(date -u -d @${NOW} '+%Y-%m-%dT%H:%M:%SZ') ==="
echo ""

echo "--- sycode-trading (legacy DGX) ---"
sqlite3 "${KANBAN_HOME}/sycode-trading/kanban.db" \
  "SELECT status, COUNT(*) FROM tasks GROUP BY status;" 2>&1
echo "Ready tasks:"
sqlite3 "${KANBAN_HOME}/sycode-trading/kanban.db" \
  "SELECT id, assignee, title FROM tasks WHERE status='ready';" 2>&1
echo "Running tasks:"
sqlite3 "${KANBAN_HOME}/sycode-trading/kanban.db" \
  "SELECT id, assignee, title, started_at FROM tasks WHERE status='running';" 2>&1

echo ""
echo "--- jarvis-os ---"
sqlite3 "${KANBAN_HOME}/jarvis-os/kanban.db" \
  "SELECT status, COUNT(*) FROM tasks GROUP BY status;" 2>&1
echo "Ready tasks:"
sqlite3 "${KANBAN_HOME}/jarvis-os/kanban.db" \
  "SELECT id, assignee, title FROM tasks WHERE status='ready';" 2>&1
echo "Running tasks:"
sqlite3 "${KANBAN_HOME}/jarvis-os/kanban.db" \
  "SELECT id, assignee, title, started_at FROM tasks WHERE status='running';" 2>&1

# New completions since last checkpoint
if [ -f "${CHECKPOINT_FILE}" ]; then
  LAST_CHECK=$(cat "${CHECKPOINT_FILE}")
  echo ""
  echo "--- New completions since last check (@${LAST_CHECK}) ---"
  
  echo "=== jarvis-os new done ==="
  sqlite3 "${KANBAN_HOME}/jarvis-os/kanban.db" \
    "SELECT id, assignee, title, completed_at, datetime(completed_at, 'unixepoch') FROM tasks WHERE status='done' AND completed_at > ${LAST_CHECK} ORDER BY completed_at DESC;" 2>&1
  
  echo "=== sycode-trading new done ==="
  sqlite3 "${KANBAN_HOME}/sycode-trading/kanban.db" \
    "SELECT id, assignee, title, completed_at, datetime(completed_at, 'unixepoch') FROM tasks WHERE status='done' AND completed_at > ${LAST_CHECK} ORDER BY completed_at DESC;" 2>&1
else
  echo "FIRST RUN — no checkpoint yet. Establishing baseline."
fi

# Update checkpoint to current time
echo "${NOW}" > "${CHECKPOINT_FILE}"
echo ""
echo "Checkpoint updated to @${NOW}"
