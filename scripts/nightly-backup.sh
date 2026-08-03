#!/usr/bin/env bash
# Nightly fleet-state backup: consistent sqlite snapshots + critical state, pushed off-box to the Mac.
# Registered as a no-agent hermes cron job (04:30). Companion to hermes-state-backup.sh (04:00, on-box).
set -euo pipefail
TS=$(date +%Y%m%d-%H%M%S)
DEST="$HOME/fleet-backups/$TS"
mkdir -p "$DEST"

for b in upero sycode-ai sycode-trading jarvis-os; do
    db="$HOME/.hermes/kanban/boards/$b/kanban.db"
    [ -f "$db" ] && sqlite3 "$db" ".backup $DEST/kanban-$b.db"
done

tar --use-compress-program='gzip -1' -cf "$DEST/hermes-state.tar.gz" -C "$HOME" \
    --exclude=.hermes/hermes-agent --exclude=.hermes/logs --exclude=.hermes/cron/output \
    --exclude=.hermes/backups --exclude=.hermes/state-snapshots \
    --exclude=.hermes/lsp --exclude=.hermes/checkpoints \
    --exclude=.hermes/node --exclude=.hermes/bin \
    --exclude='.hermes/profiles/*/sessions' --exclude='.hermes/profiles/*/home' \
    --exclude='.hermes/profiles/*/lsp' \
    --exclude='.hermes/profiles/*/bin' \
    --exclude='.hermes/profiles/*/state-snapshots' --exclude='.hermes/profiles/*/checkpoints' \
    --exclude='.hermes/profiles/*/skills/.hub/index-cache' \
    --exclude='.hermes/profiles/*/logs' --exclude='.hermes/profiles/*/cron/output' \
    --exclude='.hermes/profiles/*/sandboxes' --exclude='.hermes/profiles/*/cache' \
    --exclude='.hermes/profiles/*/image_cache' --exclude='.hermes/profiles/*/audio_cache' \
    --exclude='.hermes/profiles/*/output' \
    --exclude='*/node_modules' --exclude='*/.next/cache' \
    --exclude='*/.vscode-server-extensions' --exclude='*/.turbo' \
    --exclude='*/coverage' --exclude='*/dist' --exclude='*.tsbuildinfo' \
    --exclude='.hermes/kanban' \
    --exclude='.hermes/worktrees' --exclude='.hermes/venvs' --exclude='.hermes/staging' \
    --exclude='.hermes/audit' --exclude='.hermes/state-snapshots' \
    --exclude='.hermes/profiles/*/state-snapshots' \
    --exclude='.hermes/profiles/*/state.db' --exclude='.hermes/profiles/*/state.db-*' \
    --exclude='.hermes/hermes-agent-wt-*' \
    .hermes 2>/dev/null || true

echo "$TS" > "$HOME/fleet-backups/LATEST"

# Push off-box (Mac alias from ~/.ssh/config; BatchMode)
if ssh mac true 2>/dev/null; then
    rsync -a "$HOME/fleet-backups/$TS" "$HOME/fleet-backups/LATEST" mac:dgx-fleet-backups/
    echo "pushed $TS to mac:dgx-fleet-backups/"
else
    echo "WARNING: mac unreachable — backup is on-box only at $DEST"
    exit 1
fi

# Retention: 14 days local
find "$HOME/fleet-backups" -maxdepth 1 -type d -name "20*" -mtime +14 -exec rm -rf {} + 2>/dev/null || true
echo "[SILENT] nightly backup ok ($TS)"
