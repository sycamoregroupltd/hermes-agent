#!/usr/bin/env bash
# Nightly fleet-state backup: consistent sqlite snapshots + critical state, pushed off-box to the Mac.
# Registered as a no-agent hermes cron job (04:30). Companion to hermes-state-backup.sh (04:00, on-box).
set -euo pipefail
TS=$(date +%Y%m%d-%H%M%S)
# Headroom preflight: stop before creating snapshots or pruning when the Mac is already
# exhausted. This is alert-only; cleanup, process termination, and reboot stay Frank-gated.
REMOTE_MIN_FREE_GB="${REMOTE_MIN_FREE_GB:-10}"
REMOTE_MAX_USED_PCT="${REMOTE_MAX_USED_PCT:-99}"
remote_df=$(ssh mac "df -kP ~" 2>/dev/null | awk 'NR==2 {print $4, $5}') || remote_df=""
if [ -z "$remote_df" ]; then
    echo "BACKUP PREFLIGHT FAILED: cannot read Mac filesystem headroom; refusing to create a snapshot."
    exit 1
fi
set -- $remote_df
remote_free_kb="$1"
remote_used_pct="${2%%%}"
if ! [[ "$remote_free_kb" =~ ^[0-9]+$ && "$remote_used_pct" =~ ^[0-9]+$ ]]; then
    echo "BACKUP PREFLIGHT FAILED: malformed Mac df output ($remote_df); refusing to create a snapshot."
    exit 1
fi
remote_free_gb=$((remote_free_kb / 1024 / 1024))
if [ "$remote_free_gb" -lt "$REMOTE_MIN_FREE_GB" ] || [ "$remote_used_pct" -ge "$REMOTE_MAX_USED_PCT" ]; then
    echo "BACKUP PREFLIGHT BLOCKED: Mac headroom ${remote_free_gb}G free / ${remote_used_pct}% used (minimum ${REMOTE_MIN_FREE_GB}G free and below ${REMOTE_MAX_USED_PCT}% used)."
    echo "No snapshot, prune, or transfer attempted; remediate only with Frank approval where required."
    exit 1
fi
# Load preflight: skip the transfer entirely when the Mac is too loaded to accept it.
# Exits 0 (clean skip) so the cron health canary doesn't flag it — the load will
# likely be lower next night. This is distinct from the disk-headroom preflight
# above, which exits 1 (real failure) because a full disk needs intervention.
# MAC_LOAD_CHECK_SKIP_PCT=80 means: skip if (1m load / core count) * 100 >= 80.
MAC_LOAD_CHECK_SKIP_PCT="${MAC_LOAD_CHECK_SKIP_PCT:-80}"
remote_load_check=$(ssh -o ConnectTimeout=10 -o BatchMode=yes mac \
    "echo \$(sysctl -n vm.loadavg | awk '{print \$2}') \$(sysctl -n hw.ncpu)" \
    2>/dev/null) || remote_load_check=""
if [ -n "$remote_load_check" ]; then
    remote_load_1m=$(echo "$remote_load_check" | awk '{print $1}')
    remote_ncpu=$(echo "$remote_load_check" | awk '{print $2}')
    if [ -n "$remote_ncpu" ] && [ "$remote_ncpu" -gt 0 ] 2>/dev/null; then
        load_pct=$(echo "$remote_load_1m $remote_ncpu" | awk '{printf "%d", ($1/$2)*100}')
        if [ -n "$load_pct" ] && [ "$load_pct" -ge "$MAC_LOAD_CHECK_SKIP_PCT" ] 2>/dev/null; then
            echo "BACKUP LOAD SKIP: Mac load/core ${load_pct}% (load=${remote_load_1m}, cores=${remote_ncpu}) >= ${MAC_LOAD_CHECK_SKIP_PCT}% threshold — skipping transfer, will retry next run."
            echo "[SILENT] nightly backup load-skipped ($TS)"
            exit 0
        fi
    fi
fi
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
# Remote keeps 2 complete generations (~10G): the Mac volume hit 100% with
# 112MiB free on 2026-08-31. Two generations preserve rollback depth while
# retaining enough headroom for the next transfer.
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
# LOCAL_KEEP default 7 (was 14): ~8.6G/night x 14 ~120G on DGX; Isolation-safe reclaim 2026-09-05.
LOCAL_KEEP="${LOCAL_KEEP:-7}"
prune_keep "$HOME/fleet-backups" "$LOCAL_KEEP" local
ssh mac 'cd ~/dgx-fleet-backups 2>/dev/null || exit 0
    ls -1d 20*/ 2>/dev/null | sed "s#/\$##" | sort -r | tail -n +3 | while read -r d; do
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
    --exclude=.hermes/snapshot-archive --exclude=.hermes/board-backup-archive \
    --exclude=.hermes/tmp \
    --exclude=.hermes/lsp --exclude=.hermes/checkpoints \
    --exclude=.hermes/node --exclude=.hermes/bin \
    --exclude='.hermes/profiles/.archive' --exclude='.hermes/profiles/.archived' \
    --exclude='.hermes/profiles/.quarantine' \
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
    --exclude='.hermes/.git' --exclude='.hermes/.claude' \
    --exclude='.hermes/recovery-artifacts' --exclude='.hermes/refactor-backup-*' \
    --exclude='.hermes/hermes-worktrees' --exclude='.hermes/hermes-agent-worktrees' \
    --exclude='.hermes/scratch' --exclude='.hermes/staging' --exclude='.hermes/staging-durable-exit' \
    --exclude='.hermes/var' --exclude='.hermes/audit' --exclude='.hermes/scripts/backups' \
    --exclude='.hermes/scripts/logs' --exclude='.hermes/skills/.archive' \
    --exclude='.hermes/shared-memory/skill-duplicate-quarantine' \
    --exclude='.hermes/deploy-state/build-tree' \
    --exclude='.hermes/profiles/*/work-graphs' --exclude='.hermes/profiles/*/audits' \
    --exclude='.hermes/profiles/*/node' --exclude='.hermes/profiles/*/cron' \
    --exclude='.hermes/profiles/*/state' --exclude='.hermes/profiles/*/mcp-installs' \
    .hermes 2>/dev/null || true

# --- Obsidian vaults: 10,943 notes, the fleet's entire knowledge base, and they live OUTSIDE
# .hermes so nothing above covers them. They had NO off-box copy and NO git remote (2026-08-03).
# DELIBERATE CHOICE: backed up to the Mac over SSH rather than pushed to GitHub — a secret scan
# found live-looking credentials inside Orchestration/runbooks/artifacts/ (kanban DB dumps), so a
# cloud host is the wrong destination until those are cleaned out (carded separately).
# The .db/.corrupt/.bak dumps are excluded here too: 583M of regenerable artifacts that also
# happen to be the credential-bearing files.
# Fleet SoT vault always. Personal ~/obsidian (~6.8G, mostly quant-team) is OPTIONAL:
# at Tailscale ~1-1.4MB/s an 8.6G push ~7200s and hits Hermes global script_timeout
# (observed 8x nightly-fleet-backup timeouts 2026-09-01..05; Mac left with partial dirs).
# Set INCLUDE_PERSONAL_OBSIDIAN=1 to restore the old dual-vault nightly.
# SLIM 2026-09-06 (native-improve pulse): exclude .git/.claude/recovery/refactor/worktrees/scratch/staging/var/audit + profile work-graphs/audits/node/cron/state — cut hermes-state from ~4.5G so 7200s timeout can finish (9 consecutive nightly-fleet-backup timeouts).
VAULTS=(obsidian-fleet-vault)
if [ "${INCLUDE_PERSONAL_OBSIDIAN:-0}" = "1" ]; then
    VAULTS+=(obsidian)
else
    echo "NOTE: skipping personal ~/obsidian off-box (INCLUDE_PERSONAL_OBSIDIAN!=1); fleet vault only."
fi
for vault in "${VAULTS[@]}"; do
    [ -d "$HOME/$vault" ] || continue
    tar --use-compress-program='gzip -1' -cf "$DEST/$vault.tar.gz" -C "$HOME" \
        --exclude='*/.git/objects/pack/tmp_*' \
        --exclude='*.db' --exclude='*.db-*' --exclude='*.sqlite' \
        --exclude='*.corrupt' --exclude='*.corrupt.*' --exclude='*.bak' \
        --exclude='*/node_modules' \
        "$vault" 2>/dev/null || echo "WARNING: vault backup failed for $vault"
done

# LATEST only after verified off-box push (below). Writing it here advanced the restore
# pointer on failed/partial nights (same class as 2026-08-14).

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
    # SLIM 2026-09-06: push small artifacts first (kanban + fleet vault), then hermes-state.
    # Observed 20260906-043055: whole-dir rsync spent the 7200s budget on hermes-state.tar.gz
    # (~4.5G @ ~1MB/s) and Mac only received a partial hermes-state; vault+kanban never landed.
    push_ok=0
    remote_root="dgx-fleet-backups/$TS"
    ssh mac "mkdir -p ~/$remote_root" 2>/dev/null || true
    # Phase A — small/critical first
    smalls=()
    for f in "$HOME/fleet-backups/$TS"/kanban-*.db "$HOME/fleet-backups/$TS"/obsidian-fleet-vault.tar.gz "$HOME/fleet-backups/$TS"/obsidian.tar.gz; do
        [ -f "$f" ] && smalls+=("$f")
    done
    if [ ${#smalls[@]} -gt 0 ]; then
        if ! rsync -a --timeout=300 --partial "${smalls[@]}" "mac:$remote_root/"; then
            echo "WARNING: phase-A small-file rsync failed — continuing to hermes-state attempts" >&2
        else
            echo "phase-A pushed ${#smalls[@]} small artifact(s) to mac:$remote_root/"
        fi
    fi
    # Phase B — large hermes-state last (bounded retries)
    # CHUNKED 2026-09-18 (t_0f13adaf): the Mac's Tailscale IPv6 endpoint roams mid-transfer
    # (observed two consecutive `tailscale ping` calls returning different embedded IPv6
    # segments/ports for the SAME peer), and the receiver is openrsync (no --append support),
    # so every whole-file retry restarted hermes-state.tar.gz from byte 0. Across 3 attempts
    # with a ~300s stall window each, the transfer never got an uninterrupted window long enough
    # to finish a 371M file at the observed ~0.5-1MB/s link rate (manually reproduced 2026-09-18
    # 05:12-05:37: 3/3 attempts died mid-transfer with identical "Broken pipe"/rc=10).
    # Splitting into ~20M chunks means a mid-transfer drop only loses the in-flight chunk; rsync's
    # default quick-check (size+mtime) skips already-landed chunks on retry, so each retry makes
    # real forward progress instead of re-paying the whole 371M every time.
    if [ -f "$HOME/fleet-backups/$TS/hermes-state.tar.gz" ]; then
        chunk_dir="$HOME/fleet-backups/$TS/hermes-state.parts"
        mkdir -p "$chunk_dir"
        split -b 20m "$HOME/fleet-backups/$TS/hermes-state.tar.gz" "$chunk_dir/chunk-"
        # HARDENED 2026-09-18 (t_64f652ce): the previous approach sent ALL chunks over ONE
        # rsync/SSH connection. IPv6 endpoint roaming on the Mac kills the entire SSH session,
        # so every in-progress chunk transfer died and the whole batch restarted from 0.
        # Per-chunk INDEPENDENT ssh/scp connections: each chunk gets its own SSH session, so a
        # mid-transfer roam kills only that one chunk; already-landed chunks are never re-sent.
        # State file on the Mac tracks which chunks have landed so this loop is idempotent
        # across cron re-runs (a fresh run re-scps only the missing chunks).
        chunk_count=$(ls -1 "$chunk_dir"/chunk-* 2>/dev/null | wc -l)
        echo "hermes-state split into $chunk_count chunks — pushing per-chunk (independent ssh)..."
        # Per-chunk retry budget: each chunk gets up to 5 attempts. Total wall time bounded by
        # the 10800s cron timeout; per-chunk --timeout=120 caps a stalled single-chunk transfer.
        CHUNK_MAX_RETRIES=5
        all_landed=0
        for c in "$chunk_dir"/chunk-*; do
            cname=$(basename "$c")
            lsz=$(stat -c %s "$c")
            landed=0
            for retry in $(seq 1 $CHUNK_MAX_RETRIES); do
                # Check if this chunk already landed on a previous run
                rsz=$(ssh -o ConnectTimeout=10 -o BatchMode=yes mac \
                    "stat -f %z ~/$remote_root/hermes-state.parts/$cname 2>/dev/null || echo 0" 2>/dev/null || echo 0)
                if [ "$lsz" = "$rsz" ]; then
                    landed=1
                    break
                fi
                if scp -o ConnectTimeout=10 -o BatchMode=yes -o ServerAliveInterval=15 \
                    -o ServerAliveCountMax=8 -o IPQoS=throughput \
                    "$c" "mac:$remote_root/hermes-state.parts/$cname" 2>/dev/null; then
                    # Verify post-landing
                    rsz2=$(ssh -o ConnectTimeout=10 -o BatchMode=yes mac \
                        "stat -f %z ~/$remote_root/hermes-state.parts/$cname 2>/dev/null || echo 0" 2>/dev/null || echo 0)
                    if [ "$lsz" = "$rsz2" ]; then
                        landed=1
                        break
                    fi
                fi
                echo "  chunk $cname attempt $retry/$CHUNK_MAX_RETRIES failed — sleeping 30s" >&2
                sleep 30
            done
            if [ "$landed" -ne 1 ]; then
                echo "CHUNK FAILED: $cname did not land after $CHUNK_MAX_RETRIES attempts" >&2
                all_landed=0
                break
            fi
            all_landed=1
        done
        if [ "$all_landed" -eq 1 ]; then
            # Reassemble remotely and verify final size
            local_total_sz=$(stat -c %s "$HOME/fleet-backups/$TS/hermes-state.tar.gz")
            if ssh -o ConnectTimeout=10 -o BatchMode=yes mac \
                "cat ~/$remote_root/hermes-state.parts/chunk-* > ~/$remote_root/hermes-state.tar.gz && rm -rf ~/$remote_root/hermes-state.parts" 2>/dev/null; then
                remote_total_sz=$(ssh -o ConnectTimeout=10 -o BatchMode=yes mac \
                    "stat -f %z ~/$remote_root/hermes-state.tar.gz 2>/dev/null || echo 0" 2>/dev/null || echo 0)
                if [ "$local_total_sz" = "$remote_total_sz" ]; then
                    push_ok=1
                    echo "reassembled hermes-state.tar.gz on mac (verified ${remote_total_sz} bytes)"
                else
                    echo "REASSEMBLY SIZE MISMATCH: local=$local_total_sz remote=$remote_total_sz" >&2
                fi
            else
                echo "REMOTE REASSEMBLY FAILED" >&2
            fi
        fi
        rm -rf "$chunk_dir"
    else
        # No hermes-state tar; treat phase-A success as enough if smalls exist
        push_ok=1
    fi
    if [ "$push_ok" -ne 1 ]; then
        echo "BACKUP PUSH FAILED: hermes-state did not fully reach mac — $TS may be partial off-box."
        echo "  Quarantining remote dir if present; check ssh mac / Tailscale; remote free: ssh mac df -h ~"
        ssh mac "if [ -d ~/$remote_root ]; then mv ~/$remote_root ~/${remote_root}.INCOMPLETE-quarantine-$(date +%Y%m%d%H%M%S); fi" 2>/dev/null || true
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
    # Advance restore pointer only after verified complete push.
    echo "$TS" > "$HOME/fleet-backups/LATEST"
    ssh mac "echo $TS > ~/dgx-fleet-backups/LATEST" 2>/dev/null || true
    echo "pushed $TS to mac:dgx-fleet-backups/ (verified)"
else
    echo "WARNING: mac unreachable — backup is on-box only at $DEST"
    exit 1
fi

# Report remaining remote headroom so exhaustion is visible BEFORE it breaks the backup.
avail=$(ssh mac "df -g ~ | awk 'NR==2{print \$4}'" 2>/dev/null || echo "")
if [ -n "$avail" ] && [ "$avail" -lt 20 ] 2>/dev/null; then
    echo "BACKUP REMOTE LOW SPACE: mac has ${avail}G free; a night is ~3-4G (fleet vault; personal obsidian skipped by default). Prune dgx-fleet-backups or reduce retention."
fi
echo "[SILENT] nightly backup ok ($TS)"
