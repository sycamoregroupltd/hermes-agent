#!/usr/bin/env bash
# Nightly fleet-state backup: consistent sqlite snapshots + critical state, pushed off-box to the Mac.
# Registered as a no-agent hermes cron job (04:30). Companion to hermes-state-backup.sh (04:00, on-box).
set -euo pipefail
TS=$(date +%Y%m%d-%H%M%S)
DEST="$HOME/fleet-backups/$TS"
mkdir -p "$DEST"

# Retention runs FIRST, before the tars and the rsync. It used to be the last step, which meant
# that once this job started exceeding the hermes 3600s cron timeout the prune never executed and
# snapshots accumulated at ~5G/night until the Mac filled (observed 2026-08-20: 13 nights, 65G).
# Pruning up front also frees the space this run is about to consume. Today's $DEST is never
# matched: it is minutes old and both finds require -mtime +7 / +14.
# Retention — BOTH sides. The remote was previously never pruned, which was survivable while the
# payload was small but is not now: adding the two vault tars took a night from ~0.8G to ~4.6G, and
# the Mac had 74G free, i.e. ~16 nights to a full disk and a silently failing backup.
# Remote keeps 7 (≈32G) rather than 14 (≈64G) purely for headroom on that volume.
# HARDENED 2026-08-30 (t_303ae91f): prune by DIRECTORY-NAME timestamp, NOT -mtime. The -mtime-based
# prune never matched the oldest dirs because a later rsync/op bulk-touches a backup dir's mtime, so
# an 8-day-old dir read <7 days old and survived forever — the Mac filled to 98% with retention
# "working" (dry-run returned nothing for 20260822-043050, dir mtime 2026-08-26). Directory names are
# sortable YYYYMMDD-HHMMSS and are never touched by content writes. Count-based: keep newest N.
# Runs FIRST (before the tars and the push) so it frees the space this run is about to consume.
prune_keep() {  # $1=root  $2=keep count  $3=label
    local victims
    victims=$(cd "$1" 2>/dev/null && ls -1d 20*/ 2>/dev/null | sed 's#/$##' | sort -r | tail -n +$(( $2 + 1 )))
    [ -z "$victims" ] && return 0
    printf '%s\n' "$victims" | while read -r d; do
        [ -n "$d" ] && rm -rf "${1:?}/${d:?}" && echo "  pruned $3 $d"
    done
}
prune_keep "$HOME/fleet-backups" 14 local
ssh mac 'cd ~/dgx-fleet-backups 2>/dev/null || exit 0
    ls -1d 20*/ 2>/dev/null | sed "s#/\$##" | sort -r | tail -n +8 | while read -r d; do
        [ -n "$d" ] && rm -rf "./${d:?}" && echo "  pruned remote $d"
    done' 2>/dev/null \
    || echo "WARNING: remote retention prune failed — check ssh mac df -h \~"

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
# The rsync EXIT CODE is checked. It previously was not: the script printed "pushed" and
# then "[SILENT] ... ok" whatever rsync did, so a failed push (e.g. remote disk full) was indistinguishable
# from a good one. Under the [SILENT] convention that meant a dead backup looked healthy — the same
# fabricated-success class this whole backup fix exists to kill.
# HARDENED 2026-08-28 (t_f340551d): the Tailscale path to the Mac is slow (~1-1.4MB/s, ~46ms RTT)
# and intermittently stalls. Observed 2026-08-27 18:50: rsync made zero progress for >51min (remote
# dir 20260827-185023 left EMPTY with LATEST pointing at it) until the 3600s cron timeout killed the
# run. Changes:
#   * --timeout=300          -> abort a stalled transfer after 5min of zero progress instead of hanging forever
#   * --partial              -> keep partially-transferred files on interrupt (openrsync receiver: no --append support)
#   * bounded retry loop     -> rides out transient Tailscale stalls (3 attempts, 60s backoff)
#   * remote size check      -> verify the tars landed with matching sizes; fail loudly if not
if ssh mac true 2>/dev/null; then
    push_ok=0
    for attempt in 1 2 3; do
        if rsync -a --timeout=300 --partial \
            "$HOME/fleet-backups/$TS" "$HOME/fleet-backups/LATEST" mac:dgx-fleet-backups/; then
            push_ok=1
            break
        fi
        rc=$?
        echo "WARNING: rsync attempt $attempt/3 failed (rc=$rc) — retrying in 60s" >&2
        [ "$attempt" -lt 3 ] && sleep 60
    done
    if [ "$push_ok" -ne 1 ]; then
        echo "BACKUP PUSH FAILED: all 3 rsync attempts failed — $TS exists locally but NOT off-box."
        echo "  Check ssh mac / Tailscale link (tailscale status); remote free space: ssh mac df -h ~"
        exit 1
    fi
    # Remote completeness check: the tars must be present with matching sizes.
    missing=0
    for f in hermes-state.tar.gz obsidian-fleet-vault.tar.gz obsidian.tar.gz; do
        if [ -f "$HOME/fleet-backups/$TS/$f" ]; then
            local_sz=$(stat -c %s "$HOME/fleet-backups/$TS/$f")
            remote_sz=$(ssh mac "stat -f %z ~/dgx-fleet-backups/$TS/$f" 2>/dev/null || echo 0)
            if [ "$local_sz" != "$remote_sz" ]; then
                echo "WARNING: remote $f size mismatch (local=$local_sz remote=$remote_sz)" >&2
                missing=1
            fi
        fi
    done
    if [ "$missing" -ne 0 ]; then
        echo "BACKUP PUSH INCOMPLETE: some files did not reach mac:dgx-fleet-backups/$TS — do not trust LATEST until verified."
        exit 1
    fi
    echo "pushed $TS to mac:dgx-fleet-backups/ (verified)"
else
    echo "WARNING: mac unreachable — backup is on-box only at $DEST"
    exit 1
fi

# Report remaining remote headroom so exhaustion is visible BEFORE it breaks the backup.
avail=$(ssh mac "df -g ~ | awk 'NR==2{print \$4}'" 2>/dev/null || echo "")
if [ -n "$avail" ] && [ "$avail" -lt 20 ] 2>/dev/null; then
    echo "BACKUP REMOTE LOW SPACE: mac has ${avail}G free; a night is ~5G. Prune dgx-fleet-backups or reduce retention."
fi
echo "[SILENT] nightly backup ok ($TS)"
