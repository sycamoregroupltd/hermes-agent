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

# LATEST is deliberately NOT written here. It is written ONLY after the off-box push has been
# VERIFIED (below). Writing it up-front made the restore pointer name a stamp that might never
# have landed — on 2026-08-14 the push failed and LATEST still advanced to the failed stamp.
# LATEST means "last backup verified complete off-box", nothing weaker.

# Push off-box (Mac alias from ~/.ssh/config; BatchMode)
# The rsync EXIT CODE is checked. It previously was not: the script printed "pushed" and then
# "[SILENT] ... ok" whatever rsync did, so a failed push (e.g. remote disk full) was indistinguishable
# from a good one. Under the [SILENT] convention that meant a dead backup looked healthy — the same
# fabricated-success class this whole backup fix exists to kill.
# This host drops ~33% of IPv6 packets, which is the most likely cause of the 2026-08-14
# "client_loop: send disconnect: Broken pipe" (rsync rc=10). Force IPv4 and keep the link alive.
SSH_OPTS='ssh -4 -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=20 -o ServerAliveCountMax=6'
# The Mac link transfers ~5.5GB at only 1-1.4MB/s (~65-80 minutes). A 3600s per-file
# I/O timeout is intentionally generous for that duration while still detecting a dead stall;
# the cron store timeout is 10800s, leaving room for connection setup, retries, and verification.
# Installed rsync 3.2.7 has no literal --contimems or --retries options: --contimeout=60
# is the valid connection-timeout equivalent, and this bounded loop supplies 3 retries.
# --partial-dir isolates resumable fragments; --bwlimit=0 explicitly leaves throughput uncapped.
push_ok=0
if $SSH_OPTS mac true 2>/dev/null; then
    for attempt in 1 2 3; do
        if rsync -a --partial --partial-dir=.rsync-partial --timeout=3600 \
                --contimeout=60 --bwlimit=0 -e "$SSH_OPTS" \
                "$HOME/fleet-backups/$TS" mac:dgx-fleet-backups/; then
            push_ok=1
            break
        fi
        rc=$?
        echo "WARNING: rsync attempt $attempt/3 failed (rc=$rc) — retrying in 60s" >&2
        [ "$attempt" -lt 3 ] && sleep 60
    done
    if [ "$push_ok" -eq 1 ]; then
        # rsync rc=0 is necessary but NOT sufficient. Assert the remote payload matches the
        # local one: an empty or truncated remote dir must never be accepted as a backup.
        l_n=$(find "$DEST" -maxdepth 1 -type f | wc -l)
        l_kb=$(du -sk "$DEST" | cut -f1)
        r_raw=$($SSH_OPTS mac "ls -1 dgx-fleet-backups/$TS 2>/dev/null | wc -l; du -sk dgx-fleet-backups/$TS 2>/dev/null | cut -f1" 2>/dev/null)
        r_n=$(printf '%s\n' "$r_raw" | sed -n 1p | tr -dc '0-9'); r_n=${r_n:-0}
        r_kb=$(printf '%s\n' "$r_raw" | sed -n 2p | tr -dc '0-9'); r_kb=${r_kb:-0}
        if [ "$r_n" -ge "$l_n" ] && [ "$r_kb" -ge $(( l_kb * 97 / 100 )) ]; then
            echo "pushed $TS to mac:dgx-fleet-backups/ (verified ${r_n} files, ${r_kb}KB)"
        else
            push_ok=0
            echo "BACKUP PUSH INCOMPLETE: remote has ${r_n} files/${r_kb}KB vs local ${l_n}/${l_kb}KB — NOT accepted."
        fi
    else
        echo "BACKUP PUSH FAILED: all 3 rsync attempts failed — $TS exists locally but NOT off-box."
        echo "  Check remote free space: ssh mac df -h \~"
    fi
else
    echo "WARNING: mac unreachable — backup is on-box only at $DEST"
fi

# The pointer advances ONLY on a verified push, and local/remote are written together so the
# freshness monitor's local-stamp-then-check-remote logic stays valid.
if [ "$push_ok" = 1 ]; then
    echo "$TS" > "$HOME/fleet-backups/LATEST"
    rsync -a -e "$SSH_OPTS" "$HOME/fleet-backups/LATEST" mac:dgx-fleet-backups/ \
        || echo "WARNING: payload landed but LATEST pointer push failed for $TS"
fi

# Retention — BOTH sides. The remote was previously never pruned, which was survivable while the
# payload was small but is not now: adding the two vault tars took a night from ~0.8G to ~4.6G, and
# the Mac had 74G free, i.e. ~16 nights to a full disk and a silently failing backup.
# Remote keeps 2 complete generations (~10G) because this Mac volume reached
# 100% with only 112MiB free on 2026-08-31. Two generations preserve rollback
# depth while maintaining enough headroom for the next ~5G transfer.
# Retention is COUNT-based (keep newest N by name), not mtime-based. Directory names are sortable
# timestamps and are immune to the mtime bulk-touching that let 15 June dirs survive a -mtime +14
# prune indefinitely. This runs on EVERY path, including a failed push — it used to sit below an
# `exit 1`, so the nights that most needed pruning were exactly the nights that skipped it.
prune_keep() {  # $1=dir  $2=keep count  $3=label
    local victims
    victims=$(cd "$1" 2>/dev/null && ls -1d 20*/ 2>/dev/null | sed 's#/$##' | sort -r | tail -n +$(( $2 + 1 )))
    [ -z "$victims" ] && return 0
    printf '%s\n' "$victims" | while read -r d; do
        [ -n "$d" ] && rm -rf "${1:?}/${d:?}" && echo "  pruned $3 $d"
    done
}
prune_keep "$HOME/fleet-backups" 14 local
$SSH_OPTS mac 'cd ~/dgx-fleet-backups 2>/dev/null || exit 0
    ls -1d 20*/ 2>/dev/null | sed "s#/\$##" | sort -r | tail -n +3 | while read -r d; do
        [ -n "$d" ] && rm -rf "./${d:?}" && echo "  pruned remote $d"
    done' 2>/dev/null \
    || echo "WARNING: remote retention prune failed — check ssh mac df -h \~"

# Report remaining remote headroom so exhaustion is visible BEFORE it breaks the backup.
avail=$($SSH_OPTS mac "df -g ~ | awk 'NR==2{print \$4}'" 2>/dev/null || echo "")
if [ -n "$avail" ] && [ "$avail" -lt 20 ] 2>/dev/null; then
    echo "BACKUP REMOTE LOW SPACE: mac has ${avail}G free; a night is ~5G. Prune dgx-fleet-backups or reduce retention."
fi

# Exit code is the ONLY liveness signal a no-agent cron job propagates — stdout is never parsed.
# A failed push must reach it, and it must do so AFTER retention has run.
if [ "$push_ok" != 1 ]; then
    echo "BACKUP NOT VERIFIED OFF-BOX for $TS — on-box copy remains at $DEST."
    exit 1
fi
echo "[SILENT] nightly backup ok ($TS)"
