#!/bin/bash
set -euo pipefail
BACKUP_DIR="${1:-$HOME/.hermes/backups}"
TS=$(date +%Y%m%d-%H%M%S)
DEST="$BACKUP_DIR/hermes-state-$TS.tar.gz"

mkdir -p "$BACKUP_DIR"

# Critical state only (avoid huge model caches)
tar --exclude='*.log' --exclude='cache/*' --exclude='image_cache/*' --exclude='audio_cache/*' \
    --exclude='state-snapshots/*' \
    -czf "$DEST" \
    -C "$HOME/.hermes" \
    cron/jobs.json kanban/boards/*/kanban.db profiles/*/auth.json \
    profiles/*/cron/jobs.json skills/ .env config.yaml 2>/dev/null || true

# Also snapshot the main kanban if present
[ -f "$HOME/.hermes/kanban.db" ] && cp "$HOME/.hermes/kanban.db" "$BACKUP_DIR/kanban-root-$TS.db" || true

echo "Hermes state backup created: $DEST"
ls -lh "$DEST" 2>/dev/null || true

# Keep last 7
find "$BACKUP_DIR" -name 'hermes-state-*.tar.gz' -mtime +7 -delete 2>/dev/null || true
