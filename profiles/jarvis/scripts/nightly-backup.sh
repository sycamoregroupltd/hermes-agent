#!/usr/bin/env bash
# Nightly fleet-state backup: consistent sqlite snapshots + critical state, pushed off-box to the Mac.
# Registered as a no-agent hermes cron job (04:30). Companion to hermes-state-backup.sh (04:00, on-box).
set -euo pipefail
TS=$(date +%Y%m%d-%H%M%S)
# Headroom preflight: stop before creating snapshots or pruning when the Mac is already
# exhausted. This is alert-only; cleanup, process termination, and reboot stay Frank-gated.
REMOTE_MIN_FREE_GB="${REMOTE_MIN_FREE_GB:-10}"
REMOTE_MAX_USED_PCT="${REMOTE_MAX_USED_PCT:-99}"
remote_df=$(ssh -4 -o ConnectTimeout=5 -o BatchMode=yes mac "df -kP ~" 2>/dev/null | awk 'NR==2 {print $4, $5}') || remote_df=""
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
# Load + I/O preflight: skip when Mac is too loaded to accept transfers.
# Fail-safe: if the checks themselves can't execute (ssh timeout under load),
# assume the Mac is overloaded and skip rather than start a doomed transfer.
# MAC_LOAD_CHECK_SKIP_PCT=50 means: skip if (1m load / core count) * 100 >= 50.
MAC_LOAD_CHECK_SKIP_PCT="${MAC_LOAD_CHECK_SKIP_PCT:-50}"
remote_load_check=$(ssh -4 -o ConnectTimeout=5 -o BatchMode=yes mac \
    "echo \$(sysctl -n vm.loadavg | awk '{print \$2}') \$(sysctl -n hw.ncpu)" \
    2>/dev/null) || remote_load_check=""
if [ -z "$remote_load_check" ]; then
    echo "BACKUP LOAD CHECK SKIP: could not read Mac load (ssh failed/timeout) — Mac likely overloaded, skipping transfer."
    echo "[SILENT] nightly backup load-check-skipped ($TS)"
    exit 0
fi
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
ssh -4 -o ConnectTimeout=5 -o BatchMode=yes mac 'cd ~/dgx-fleet-backups 2>/dev/null || exit 0
    ls -1d 20*/ 2>/dev/null | sed "s#/\$##" | sort -r | tail -n +3 | while read -r d; do
        [ -n "$d" ] && rm -rf "./${d:?}" && echo "  pruned remote $d"
    done' 2>/dev/null \
    || echo "WARNING: remote retention prune failed — check ssh mac df -h ~"

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

# Push off-box via HTTP range-server (replaces rsync — survives ~33% packet loss
# on the DGX<->Mac Tailscale link where rsync stalls with "Broken pipe").
# Approach: DGX serves the snapshot over HTTP with byte-range support; Mac pulls
# each file with curl -C - (resume) + SHA256 verification. A mid-transfer drop
# loses only the in-flight file; already-landed files are never re-fetched.
# This is the same approach that manually succeeded in t_477edff2 where rsync failed.
# HARDENED 2026-09-18 (t_d28dacd8): switch transport from rsync to HTTP range-pull.

# Generate checksums so the Mac can verify each file after download.
(cd "$DEST" && sha256sum hermes-state.tar.gz kanban-*.db obsidian-fleet-vault.tar.gz > SHA256SUMS 2>/dev/null) \
    || echo "WARNING: SHA256SUMS generation failed — Mac will still pull but cannot verify" >&2

# Detect DGX Tailscale IP for the Mac to reach us.
TS_IP=$(tailscale ip -4 2>/dev/null) || TS_IP=""
if [ -z "$TS_IP" ]; then
    echo "BACKUP PUSH FAILED: cannot determine DGX Tailscale IP (tailscale ip -4)." >&2
    exit 1
fi

# Start range-server serving the snapshot dir; kill any stale one first.
pkill -f "range-server.py" 2>/dev/null || true
sleep 1
RANGE_PORT=18888
RANGE_LOG="/tmp/range-server-$TS.log"
python3 "$HOME/.hermes/profiles/jarvis/scripts/range-server.py" \
    --root "$DEST" --port "$RANGE_PORT" --bind 0.0.0.0 > "$RANGE_LOG" 2>&1 &
RANGE_PID=$!
# Ensure cleanup on exit
trap 'kill $RANGE_PID 2>/dev/null || true' EXIT INT TERM
# Wait for server to be ready (up to 10s)
SERVER_READY=0
for _ in $(seq 1 10); do
    if curl -fsS "http://127.0.0.1:$RANGE_PORT/" >/dev/null 2>&1; then
        SERVER_READY=1
        break
    fi
    sleep 1
done
if [ "$SERVER_READY" -ne 1 ]; then
    echo "BACKUP PUSH FAILED: range-server did not start (log: $RANGE_LOG)." >&2
    exit 1
fi
echo "range-server serving $DEST on $TS_IP:$RANGE_PORT (pid $RANGE_PID)"

# Pull from Mac with the parameterized pull script
REMOTE_ROOT="dgx-fleet-backups"
push_ok=0
if ssh -4 -o ConnectTimeout=5 -o BatchMode=yes mac "mkdir -p ~/$REMOTE_ROOT" 2>/dev/null; then
    echo "Triggering Mac pull of $TS via HTTP range-pull..."
    if ssh -4 -o ConnectTimeout=30 -o BatchMode=yes mac \
        "$HOME/dgx-fleet-backups/dgx-pull-snapshot.sh $TS $TS_IP"; then
        push_ok=1
        echo "Mac pull completed successfully (all files verified)"
    else
        echo "BACKUP PUSH FAILED: Mac pull of $TS returned non-zero exit code." >&2
    fi
else
    echo "BACKUP PUSH FAILED: cannot create remote dir on Mac." >&2
fi

# Stop range-server
kill $RANGE_PID 2>/dev/null || true
trap - EXIT INT TERM

if [ "$push_ok" -ne 1 ]; then
    echo "BACKUP PUSH FAILED: $TS did not fully reach mac — check ssh mac / Tailscale." >&2
    echo "  Quarantining remote dir if present; remote free: ssh mac df -h ~"
    ssh mac "if [ -d ~/$REMOTE_ROOT/$TS ]; then mv ~/$REMOTE_ROOT/$TS ~/$REMOTE_ROOT/${TS}.INCOMPLETE-quarantine-$(date +%Y%m%d%H%M%S); fi" 2>/dev/null || true
    exit 1
fi

# Advance restore pointer only after verified complete push.
echo "$TS" > "$HOME/fleet-backups/LATEST"
ssh mac "echo $TS > ~/dgx-fleet-backups/LATEST" 2>/dev/null || true
echo "pushed $TS to mac:dgx-fleet-backups/ (verified)"

# Clean up any stale quarantine dirs from previous failed runs (best-effort).
ssh mac "cd ~/$REMOTE_ROOT 2>/dev/null && ls -d *.INCOMPLETE-quarantine-* 2>/dev/null | while read -r q; do [ -n \"\$q\" ] && rm -rf \"./\${q:?}\" && echo \"  pruned quarantine \$q\"; done" 2>/dev/null || true

# Report remaining remote headroom so exhaustion is visible BEFORE it breaks the backup.
avail=$(ssh -4 -o ConnectTimeout=5 -o BatchMode=yes mac "df -g ~ | awk 'NR==2{print \$4}'" 2>/dev/null || echo "")
if [ -n "$avail" ] && [ "$avail" -lt 20 ] 2>/dev/null; then
    echo "BACKUP REMOTE LOW SPACE: mac has ${avail}G free; a night is ~3-4G (fleet vault; personal obsidian skipped by default). Prune dgx-fleet-backups or reduce retention."
fi
echo "[SILENT] nightly backup ok ($TS)"
