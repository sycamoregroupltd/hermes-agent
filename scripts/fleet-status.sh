#!/usr/bin/env bash
# fleet-status.sh — Hardened Jarvis Fleet Status Reporter
# Reads all 4 PM STATUS files LOCALLY (DGX), reports kanban backlog, checks cron health.
# v2.0 2026-06-09 — adds cron health, gateway state, and per-board dispatch status.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGDIR="${SCRIPT_DIR}/logs"
mkdir -p "$LOGDIR"
RUN_TS="$(date -u +%Y%m%d_%H%M%S)"
LOGFILE="$LOGDIR/fleet-status_${RUN_TS}.log"

log()  { echo "[$(date -u +%Y%m%dT%H:%M:%SZ)] $*" | tee -a "$LOGFILE"; }
fatal(){ log "FATAL: $*"; exit 1; }

log "═══ JARVIS FLEET STATUS (4 projects) ═══"

# ── 1. PM STATUS files ────────────────────────────────
for proj in upero sycode-ai sycode-trading jarvis-os; do
    f="/home/frank/jarvis/workspace/goals/$proj/STATUS.md"
    echo
    log "──────── $proj ────────"
    if [ -f "$f" ]; then
        if [ -s "$f" ]; then
            log "STATUS: EXISTS ($(wc -c < "$f" | tr -d ' ') bytes)"
            # Show first 6 lines of the status for the report
            head -6 "$f" | sed 's/^/  /' | while IFS= read -r l; do log "$l"; done
        else
            log "STATUS: EMPTY file ($f)"
        fi
    else
        log "STATUS: MISSING $f"
    fi
done

# ── 2. Kanban board snapshot ────────────────────────────
echo
log "═══ kanban board snapshot ═══"
for b in upero sycode-ai sycode-trading jarvis-os; do
    # Count by status semantics, not list glyphs. `list` includes done rows and glyphs
    # vary (`●` running, `◻` todo, `⊘` blocked); narrow glyph regexes hide backlog.
    stats="$(hermes kanban --board "$b" stats 2>/dev/null || true)"
    todo=$(awk '$1=="todo" {print $2}' <<<"$stats")
    ready=$(awk '$1=="ready" {print $2}' <<<"$stats")
    running=$(awk '$1=="running" {print $2}' <<<"$stats")
    blocked=$(awk '$1=="blocked" {print $2}' <<<"$stats")
    todo=${todo:-0}; ready=${ready:-0}; running=${running:-0}; blocked=${blocked:-0}
    open_total=$((todo + ready + running + blocked))
    log "  [$b]: open=$open_total (running=$running ready=$ready todo=$todo blocked=$blocked)"
done

# ── 3. Cron health check ────────────────────────────────
echo
log "═══ cron health ═══"
CRON_OK=true
if [ -f "/home/frank/.hermes/cron/jobs.json" ]; then
    total_jobs=$(grep -c '"id"' "/home/frank/.hermes/cron/jobs.json" || echo "0")
    log "  Cron DB: $total_jobs jobs loaded"
    # Check gateway is running
    if [ -f "/home/frank/.hermes/gateway.pid" ]; then
        gwpid=$(grep -oP '"pid":\s*\K[0-9]+' "/home/frank/.hermes/gateway.pid" || true)
        if [ -n "$gwpid" ] && kill -0 "$gwpid" 2>/dev/null; then
            log "  Gateway: RUNNING (pid $gwpid)"
        else
            log "  Gateway: STALE (pid $gwpid not alive)"
            CRON_OK=false
        fi
    else
        log "  Gateway: NO PID file"
        CRON_OK=false
    fi
    # Count jobs by last_status
    ok_jobs=$(grep -c '"last_status":\s*"ok"' "/home/frank/.hermes/cron/jobs.json" || true)
    log "  Jobs with last_status=ok: $ok_jobs"
else
    log "  Cron DB: MISSING"
    CRON_OK=false
fi

# ── 4. Gateway / channel state ────────────────────────
echo
log "═══ gateway / channels ═══"
if [ -f "/home/frank/.hermes/channel_directory.json" ]; then
    ch_ct=$(grep -oP '"[a-z_]+":\s*\[' "/home/frank/.hermes/channel_directory.json" | wc -l || true)
    active_ch=$(grep -oP '"id":\s*"[^"]+"' "/home/frank/.hermes/channel_directory.json" | wc -l || true)
    if [ -z "$active_ch" ]; then active_ch=0; fi
    log "  Channel types tracked: $ch_ct"
    log "  Active channels: $active_ch"
else
    log "  channel_directory.json: MISSING"
fi

# ── 5. Overall health ─────────────────────────────────
echo
log "═══ overall ═══"
if $CRON_OK; then
    log "HEALTH: OK"
    exit 0
else
    log "HEALTH: DEGRADED (cron/gateway issues detected)"
    exit 1
fi
