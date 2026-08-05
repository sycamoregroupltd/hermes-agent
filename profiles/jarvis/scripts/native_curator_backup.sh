#!/usr/bin/env bash
# Native curator backup — no_agent wrapper for cron job 43d386fc9339.
# Runs hermes curator backup and reports the latest snapshot path + size.
set -euo pipefail

echo "---"
echo "Cron Job: native-curator-backup"
echo "Job ID: 43d386fc9339"
echo "Mode: no_agent (script)"
echo "---"

# Run the backup (outputs to profile-scoped .curator_backups dir)
/home/frank/.local/bin/hermes curator backup --reason "scheduled-auto" > /dev/null 2>&1 || {
    echo "FAILED: hermes curator backup exited non-zero"
    exit 1
}

# Find the latest snapshot directory across both possible locations
LATEST=""
for DIR in \
    "/home/frank/.hermes/profiles/jarvis/skills/.curator_backups" \
    "/home/frank/.hermes/skills/.curator_backups"
do
    if [ -d "$DIR" ]; then
        CAND=$(ls -t "$DIR" | head -1)
        if [ -n "$CAND" ] && { [ -z "$LATEST" ] || [[ "$CAND" > "$LATEST" ]]; }; then
            LATEST="$CAND"
            ACTUAL_DIR="$DIR/$LATEST"
        fi
    fi
done

if [ -z "$ACTUAL_DIR" ] || [ ! -d "$ACTUAL_DIR" ]; then
    echo "FAILED: no curator backup snapshot found"
    exit 1
fi

SIZE=$(du -sh "$ACTUAL_DIR" 2>/dev/null | cut -f1)

echo "native-curator-backup OK at $(date -u +%Y-%m-%dT%H:%MZ)"
echo "Snapshot: $ACTUAL_DIR"
echo "Path: $ACTUAL_DIR"
echo "Size: $SIZE"
echo "Restore: hermes curator rollback <newest-snapshot>"
exit 0
