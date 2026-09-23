#!/usr/bin/env bash
# hermes-state-prune.sh — fleet-wide state.db retention (Frank directive 2026-07-28: "prune everything that we're not using").
#
# Policy (aggressive retention, tuned for a front-door profile that logs every session):
#   - delete messages+sessions ENDED older than KEEP_DAYS (default 30)
#   - blank tool-role message bodies older than TOOL_KEEP_DAYS (default 7); keeps the
#     conversation spine but drops the giant terminal/kanban dumps that dominate FTS size
#   - clean orphaned FTS rows, then VACUUM to reclaim space (SQLite does NOT auto-shrink)
#
# SAFETY:
#   - Never run against a profile DB while that profile's gateway/agents are actively
#     writing (SQLite lock contention + risk of corrupting in-flight sessions).
#     Prefer fleet-paused, or per-profile right after stopping its gateway.
#   - VACUUM needs ~2x the DB size in free space briefly. Big DBs (4GB+) take minutes.
#   - Runs profile-by-profile; logs before/after sizes. Idempotent: re-running only
#     removes things already past the window.
#
# Schedule weekly via a no_agent cron (script-only, zero tokens):
#   cronjob action=create name=hermes-state-prune-weekly schedule="0 4 * * 0" \
#           no_agent=true script=hermes-state-prune.sh
# NOTE: the cronjob tool requires this file at ~/.hermes/profiles/<profile>/scripts/,
#       NOT ~/jarvis/scripts/ or any other path (a dead-pin is rejected).
set -uo pipefail

KEEP_DAYS=${KEEP_DAYS:-30}
TOOL_KEEP_DAYS=${TOOL_KEEP_DAYS:-7}
NOW=$(date +%s)
CUTOFF=$((NOW - KEEP_DAYS*86400))
TOOL_CUTOFF=$((NOW - TOOL_KEEP_DAYS*86400))
LOG=/home/frank/jarvis/logs/state-prune.log
mkdir -p "$(dirname "$LOG")"

# Guard: only one prune at a time (VACUUM is exclusive).
exec 9>/tmp/hermes-state-prune.lock
flock -n 9 || { echo "$(date -Is) SKIP: another prune is running" >> "$LOG"; exit 0; }

TOTAL_BEFORE=0; TOTAL_AFTER=0
for db in ~/.hermes/profiles/*/state.db; do
  prof=$(basename "$(dirname "$db")")
  [ -f "$db" ] || continue
  before=$(stat -c%s "$db")
  TOTAL_BEFORE=$((TOTAL_BEFORE+before))
  sqlite3 -cmd ".timeout 60000" "$db" "
    PRAGMA journal_mode=DELETE;
    DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE ended_at IS NOT NULL AND ended_at < $CUTOFF);
    DELETE FROM sessions WHERE ended_at IS NOT NULL AND ended_at < $CUTOFF;
    UPDATE messages SET content='', reasoning=NULL, reasoning_content=NULL, codex_reasoning_items=NULL, codex_message_items=NULL
      WHERE role='tool' AND timestamp < $TOOL_CUTOFF AND length(coalesce(content,'')) > 200;
    DELETE FROM messages_fts WHERE rowid NOT IN (SELECT id FROM messages);
    VACUUM;
  " 2>>"$LOG" || { echo "$(date -Is) $prof PRUNE-FAILED" >> "$LOG"; continue; }
  after=$(stat -c%s "$db")
  TOTAL_AFTER=$((TOTAL_AFTER+after))
  echo "$(date -Is) $prof $((before/1048576))MB -> $((after/1048576))MB" >> "$LOG"
done

echo "total: $((TOTAL_BEFORE/1073741824))GB -> $((TOTAL_AFTER/1073741824))GB"
tail -5 "$LOG"
