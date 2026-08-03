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

# --- Obsidian vaults: 10,943 notes, the fleet's entire knowledge base, and they live OUTSIDE
# .hermes so nothing above covers them. They had NO off-box copy and NO git remote (2026-08-03).
# DELIBERATE CHOICE: backed up to the Mac over SSH rather than pushed to GitHub — a secret scan
# found live-looking credentials inside Orchestration/runbooks/artifacts/ (kanban DB dumps), so a
# cloud host is the wrong destination until those are cleaned out (carded separately).
# The .db/.corrupt/.bak dumps are excluded here too: 583M of regenerable artifacts that also
# happen to be the credential-bearing files.
for vault in obsidian-fleet-vault obsidian; do
    [ -d "$HOME/$vault" ] || continue
    tar --use-compress-program='gzip -1' -cf "$DEST/$vault.tar.gz" -C "$HOME" \
        --exclude='*/.git/objects/pack/tmp_*' \
        --exclude='*.db' --exclude='*.db-*' --exclude='*.sqlite' \
        --exclude='*.corrupt' --exclude='*.corrupt.*' --exclude='*.bak' \
        --exclude='*/node_modules' \
        "$vault" 2>/dev/null || echo "WARNING: vault backup failed for $vault"
done

echo "$TS" > "$HOME/fleet-backups/LATEST"

# Push off-box (Mac alias from ~/.ssh/config; BatchMode)
# The rsync EXIT CODE is checked. It previously was not: the script printed "pushed" and then
# "[SILENT] ... ok" whatever rsync did, so a failed push (e.g. remote disk full) was indistinguishable
# from a good one. Under the [SILENT] convention that meant a dead backup looked healthy — the same
# fabricated-success class this whole backup fix exists to kill.
if ssh mac true 2>/dev/null; then
    if rsync -a "$HOME/fleet-backups/$TS" "$HOME/fleet-backups/LATEST" mac:dgx-fleet-backups/; then
        echo "pushed $TS to mac:dgx-fleet-backups/"
    else
        rc=$?
        echo "BACKUP PUSH FAILED: rsync exited $rc — $TS exists locally but NOT off-box."
        echo "  Check remote free space: ssh mac df -h \~"
        exit 1
    fi
else
    echo "WARNING: mac unreachable — backup is on-box only at $DEST"
    exit 1
fi

# Retention — BOTH sides. The remote was previously never pruned, which was survivable while the
# payload was small but is not now: adding the two vault tars took a night from ~0.8G to ~4.6G, and
# the Mac had 74G free, i.e. ~16 nights to a full disk and a silently failing backup.
# Remote keeps 7 (≈32G) rather than 14 (≈64G) purely for headroom on that volume.
find "$HOME/fleet-backups" -maxdepth 1 -type d -name "20*" -mtime +14 -exec rm -rf {} + 2>/dev/null || true
ssh mac 'find ~/dgx-fleet-backups -maxdepth 1 -type d -name "20*" -mtime +7 -exec rm -rf {} + 2>/dev/null' \
    || echo "WARNING: remote retention prune failed — check ssh mac df -h \~"

# Report remaining remote headroom so exhaustion is visible BEFORE it breaks the backup.
avail=$(ssh mac "df -g ~ | awk 'NR==2{print \$4}'" 2>/dev/null || echo "")
if [ -n "$avail" ] && [ "$avail" -lt 20 ] 2>/dev/null; then
    echo "BACKUP REMOTE LOW SPACE: mac has ${avail}G free; a night is ~5G. Prune dgx-fleet-backups or reduce retention."
fi
echo "[SILENT] nightly backup ok ($TS)"
