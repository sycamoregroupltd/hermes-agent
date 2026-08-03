#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# deploy_liveness_monitor.sh — OUT-OF-BAND liveness monitor for the sycode auto-deploy pipeline.
#
# WHY (2026-07-09, fable seat): the auto-deploy cron ENOENT-looped every 15min
# for ~5h (shared checkout parked on a branch without the deploy agent) while
# merged work sat undeployed — nobody noticed. Per the silent-failure doctrine
# every plugged black hole gets a tracked monitor in the same pass.
#
# Runs from the SYSTEM crontab (survives hermes gateway death). Observe-only:
# never deploys, never restarts. Alerts via `hermes send` (bot-token path).
#
# Checks:
#  1. CRON-ALIVE: deploy log mtime must be < STALE_SECS old (cron writes every 15m).
#  2. CRON-HEALTHY: the most recent completed run must end in a known status
#     line (SUCCESS/NOOP/BUILD_FAILED/ROLLED_BACK/PIPELINE_ERROR/SAFETY_GATE_BLOCKED/
#     DEPLOY.lock HELD). A run with NO status line = broken pipeline (the ENOENT class).
#  3. SHIP-GAP: if origin/main != deployed sha for > GAP_ALERT_SECS, alert with the
#     last status (catches gate deadlocks, e.g. injector positions never flat, and
#     silent classes not imagined yet).
# Re-alerts every REALERT_SECS while a condition persists.

set -u
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/home/frank/.local/bin:/home/frank/.hermes/hermes-agent/venv/bin:/usr/bin:/bin:$PATH"
export HERMES_HOME=/home/frank/.hermes

DEPLOY_LOG="${DEPLOY_LOG:-/home/frank/logs/deploy-sycodeserver.log}"
STATE_JSON="${STATE_JSON:-/home/frank/.hermes/deploy-state/sycodeserver-state.json}"
BUILD_TREE="${BUILD_TREE:-/home/frank/.hermes/deploy-state/build-tree}"
MON_STATE="${MON_STATE:-/home/frank/.hermes/state/deploy-liveness-state.txt}"
LOG_FILE="${LOG_FILE:-/home/frank/.hermes/logs/deploy-liveness.log}"
STALE_SECS="${STALE_SECS:-2700}"        # 45 min: cron runs every 15m, 3 misses = dead
GAP_ALERT_SECS="${GAP_ALERT_SECS:-14400}" # 4h of merged-undeployed before alerting
REALERT_SECS="${REALERT_SECS:-21600}"   # re-alert every 6h while condition persists
# 2026-07-13 (t_ecfeb48c): primary target flipped discord→whatsapp:Frank. 13 ship-gap
# alerts (up to 58h) went unread in discord:#critical-alerts — an alert without a
# consumer is a black hole. WhatsApp is Frank's consumed surface; discord stays as
# failover + record. Throttle (REALERT_SECS=6h/key) bounds the noise budget.
ALERT_TARGET="${DEPLOY_MON_ALERT_TARGET:-whatsapp:Frank}"

mkdir -p "$(dirname "$MON_STATE")" "$(dirname "$LOG_FILE")"
now_epoch=$(date +%s)
now_iso=$(date -Is)

log() { echo "[$now_iso] $*" >> "$LOG_FILE"; }

send_alert() {
    local key="$1" subject="$2" body="$3"
    # throttle: one alert per key per REALERT_SECS
    local last
    last=$(grep -a "^${key}=" "$MON_STATE" 2>/dev/null | tail -1 | cut -d= -f2)
    if [ -n "${last:-}" ] && [ $((now_epoch - last)) -lt "$REALERT_SECS" ]; then
        log "SUPPRESSED key=$key (re-alert window)"
        return 0
    fi
    local delivered=0
    if hermes send -q -t "$ALERT_TARGET" -s "$subject" "$body"; then
        delivered=1
        log "ALERT-SENT target=$ALERT_TARGET key=$key subject=$subject"
    else
        local rc=$?
        log "ALERT-FAILED target=$ALERT_TARGET rc=$rc key=$key"
        # 2026-07-29 (opus5): walk a CHAIN of fallbacks, not a single one. WhatsApp
        # has been 100% dead since 07-26 (session logged out, needs a QR re-scan) and
        # discord was itself down 07-26..07-28 — with one fallback those windows
        # overlapped and 14 alerts reached nobody.
        local fb
        for fb in ${DEPLOY_MON_FALLBACKS:-discord:#critical-alerts telegram:506972405}; do
            if hermes send -q -t "$fb" -s "🔁 FAILOVER: $subject" "$body"; then
                delivered=1
                log "ALERT-FAILOVER-OK target=$fb key=$key"
                break
            fi
            log "ALERT-FAILOVER-FAILED target=$fb rc=$? key=$key"
        done
    fi
    # 2026-07-29 (opus5): arm the re-alert throttle ONLY on confirmed delivery.
    # It used to be armed unconditionally, so an alert that reached NOBODY still
    # bought 6h of silence on that key — the failure mode turned a transient
    # two-channel outage into a permanently swallowed alert. Undelivered now means
    # untracked, so the very next run retries.
    if [ "$delivered" -eq 1 ]; then
        grep -av "^${key}=" "$MON_STATE" 2>/dev/null > "$MON_STATE.tmp" || true
        echo "${key}=${now_epoch}" >> "$MON_STATE.tmp"
        mv "$MON_STATE.tmp" "$MON_STATE"
    else
        log "ALERT-UNDELIVERED key=$key — all channels failed, throttle NOT armed, retrying next run"
    fi
}

clear_key() {
    grep -av "^${1}=" "$MON_STATE" 2>/dev/null > "$MON_STATE.tmp" || true
    mv "$MON_STATE.tmp" "$MON_STATE"
}

# ── 1. CRON-ALIVE ────────────────────────────────────────────────────────────
if [ ! -f "$DEPLOY_LOG" ]; then
    send_alert cron_dead "🚨 sycode auto-deploy: log missing" "Deploy log $DEPLOY_LOG does not exist — auto-deploy cron may be gone entirely. Check: crontab -l | grep deploy"
else
    log_age=$((now_epoch - $(stat -c %Y "$DEPLOY_LOG")))
    if [ "$log_age" -gt "$STALE_SECS" ]; then
        send_alert cron_dead "🚨 sycode auto-deploy: cron silent ${log_age}s" "Deploy log untouched for ${log_age}s (threshold ${STALE_SECS}s). The 15-min auto-deploy cron is not running. Check: crontab -l; tail /home/frank/logs/deploy-sycodeserver.log"
    else
        clear_key cron_dead
        # ── 2. CRON-HEALTHY: last completed run must end in a known status ──
        # Two output contracts are accepted:
        #   legacy (pre-2026-07-13): a bare "NOOP:/SUCCESS:/..." status line
        #   current: the pristine-worktree agent's JSON report, "action": "<verb>"
        # 2026-07-29 (opus5): the 07-13 pristine rewrite replaced status lines with
        # JSON and this check was never updated — it asserted cron_broken on EVERY
        # completed run for 16 days, so a real pipeline break was undetectable
        # (saturated detector). Window widened 80→400: a healthy JSON run block is
        # ~90 lines, so tail -80 could not even see its own ACQUIRED marker.
        last_block=$(tail -400 "$DEPLOY_LOG" | awk '/DEPLOY.lock. ACQUIRED/{buf=""} {buf=buf $0 "\n"} END{printf "%s", buf}')
        if echo "$last_block" | grep -aqE "SUCCESS:|NOOP:|BUILD_FAILED:|ROLLED_BACK:|PIPELINE_ERROR:|SAFETY_GATE_BLOCKED:|UNKNOWN:|DEPLOY.lock. HELD" \
           || echo "$last_block" | grep -aqE '"action"[[:space:]]*:[[:space:]]*"(noop|deploy|deployed|build_failed|rolled_back|pipeline_error|safety_gate_blocked|unknown)"'; then
            clear_key cron_broken
        elif echo "$last_block" | grep -aq "DEPLOY.lock. released"; then
            # run completed but printed no contract status → broken pipeline (ENOENT class)
            snippet=$(echo "$last_block" | tail -4 | head -c 400)
            send_alert cron_broken "🚨 sycode auto-deploy: pipeline broken" "Last completed cron run produced no status line — the deploy agent is not executing (ENOENT class). Tail: $snippet"
        fi
        # else: a run is mid-flight (ACQUIRED without release yet) — fine, skip
    fi
fi

# ── 3. SHIP-GAP ──────────────────────────────────────────────────────────────
deployed_sha=$(python3 -c "import json;print(json.load(open('$STATE_JSON'))['deployed_sha'])" 2>/dev/null || echo "")
remote_sha=$(git -C "$BUILD_TREE" ls-remote origin refs/heads/main 2>/dev/null | cut -f1)
if [ -z "$deployed_sha" ] || [ -z "$remote_sha" ]; then
    # probe failure: escalate only after 3 consecutive (state-counted) failures
    fails=$(grep -a "^probe_fails=" "$MON_STATE" 2>/dev/null | tail -1 | cut -d= -f2); fails=$((${fails:-0} + 1))
    grep -av "^probe_fails=" "$MON_STATE" 2>/dev/null > "$MON_STATE.tmp" || true
    echo "probe_fails=${fails}" >> "$MON_STATE.tmp"; mv "$MON_STATE.tmp" "$MON_STATE"
    log "PROBE-FAILED deployed='$deployed_sha' remote='$remote_sha' consecutive=$fails"
    [ "$fails" -ge 3 ] && send_alert probe_dead "⚠️ sycode deploy monitor: probes failing" "Cannot read deployed sha ($STATE_JSON) or origin/main ($BUILD_TREE) for $fails consecutive runs — the monitor itself is blind. Investigate."
elif [ "$deployed_sha" = "$remote_sha" ]; then
    clear_key probe_fails; clear_key ship_gap
    # 2026-07-29 (opus5): also drop the per-sha gap tracker. Previously it was only
    # cleared when a NEW gap opened, so a closed gap left a stale "gap_since_<sha>="
    # line in the state file that read as an open ship-gap to anyone inspecting it.
    sed -i '/^gap_since_/d' "$MON_STATE" 2>/dev/null || true
    log "OK deployed=current (${deployed_sha:0:9})"
else
    clear_key probe_fails
    # gap exists — how long? track first-seen epoch per remote sha
    seen=$(grep -a "^gap_since_${remote_sha:0:9}=" "$MON_STATE" 2>/dev/null | tail -1 | cut -d= -f2)
    if [ -z "${seen:-}" ]; then
        sed -i '/^gap_since_/d' "$MON_STATE" 2>/dev/null || true
        echo "gap_since_${remote_sha:0:9}=${now_epoch}" >> "$MON_STATE"
        log "GAP-NEW deployed=${deployed_sha:0:9} main=${remote_sha:0:9}"
    elif [ $((now_epoch - seen)) -gt "$GAP_ALERT_SECS" ]; then
        last_status=$(tail -400 "$DEPLOY_LOG" 2>/dev/null | grep -aoE "SUCCESS:.*|NOOP:.*|BUILD_FAILED:.*|ROLLED_BACK:.*|PIPELINE_ERROR:.*|SAFETY_GATE_BLOCKED:.*|\"action\"[[:space:]]*:[[:space:]]*\"[a-z_]+\"" | tail -1)
        send_alert ship_gap "⚠️ sycode: merged work undeployed $(( (now_epoch - seen) / 3600 ))h" "origin/main ${remote_sha:0:9} has not deployed (running ${deployed_sha:0:9}) for over $(( (now_epoch - seen) / 3600 ))h. Last agent status: ${last_status:-none}. Gate deadlock (injector positions?) or pipeline issue. State: $STATE_JSON"
    else
        log "GAP-TRACKING deployed=${deployed_sha:0:9} main=${remote_sha:0:9} age=$((now_epoch - seen))s"
    fi
fi

# ── 4. BUILD-TREE-DIRTY (added 2026-07-13) ──────────────────────────────────
# Uncommitted edits in the deploy-owned build-tree fail-close every deploy gate
# silently (caused the 3-day 07-10..07-13 ship gap: injector flag flip + WIP).
# The pristine deploy resets --hard, but dirt still signals a worker violating
# the "workers never touch build-tree" rule — and destroys their work on reset.
dirty_count=$(git -C "$BUILD_TREE" status --porcelain 2>/dev/null | wc -l)
if [ "${dirty_count:-0}" -gt 0 ]; then
    seen_dirty=$(grep -a "^tree_dirty_since=" "$MON_STATE" 2>/dev/null | tail -1 | cut -d= -f2)
    if [ -z "${seen_dirty:-}" ]; then
        echo "tree_dirty_since=${now_epoch}" >> "$MON_STATE"
        log "TREE-DIRTY-NEW files=$dirty_count"
    elif [ $((now_epoch - seen_dirty)) -gt 7200 ]; then  # 2h grace
        dirty_files=$(git -C "$BUILD_TREE" status --porcelain 2>/dev/null | head -5 | tr '\n' ' ')
        send_alert tree_dirty "🚨 sycode: deploy build-tree DIRTY ${dirty_count} files" "Uncommitted edits in $BUILD_TREE for >2h: $dirty_files — a worker is violating the deploy-tree rule; their work will be destroyed by the next pristine reset AND legacy gates fail-close. Salvage: commit to the task branch + push, then clean the tree."
    else
        log "TREE-DIRTY-TRACKING files=$dirty_count age=$((now_epoch - seen_dirty))s"
    fi
else
    clear_key tree_dirty
    sed -i '/^tree_dirty_since=/d' "$MON_STATE" 2>/dev/null || true
fi

exit 0
