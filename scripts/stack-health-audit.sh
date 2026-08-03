#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# stack-health-audit.sh — full-stack recovery/liveness audit for the DGX docker stack.
#
# WHY (2026-07-13, fable seat, Frank directive): the 07-07 host reboot silently
# killed ml-trainer (auto-retrain executor) + 2 hermes containers; nobody noticed
# for 6 days. Same reboot left PrometheusTargetDown firing into an Alertmanager
# with ALL receivers commented out. Frank: "we should be monitoring everything
# comes back to full health after a restart" and "if something gets turned off
# for a reason it needs to be documented and time stamped".
#
# DESIGN — declared-state manifest (~/.hermes/state/expected-containers.manifest):
#   <container-name>                    expected RUNNING (and healthy if it has a healthcheck)
#   OFF <container-name> <ISO-ts> <who> <reason...>   deliberately off — documented + timestamped
#   # comment lines and blanks ignored
# Rules:
#   - manifest container not running, no OFF line  -> ALERT (undeclared outage)
#   - manifest container running but unhealthy     -> ALERT
#   - OFF-declared container                       -> reported (visible), never alerted
#   - running container not in manifest            -> reported as unmanifested (no alert)
#   - Alertmanager has active alerts               -> relayed in the alert body
#     (bridges the receivers-all-commented-out delivery hole until fixed in-repo)
#
# Runs from the SYSTEM crontab (*/10 + @reboot). Observe-only: never starts,
# stops, or restarts anything. Alerts via `hermes send` (throttled per key).

set -u
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/home/frank/.local/bin:/home/frank/.hermes/hermes-agent/venv/bin:/usr/bin:/bin:$PATH"
export HERMES_HOME=/home/frank/.hermes

MANIFEST="${MANIFEST:-/home/frank/.hermes/state/expected-containers.manifest}"
MON_STATE="${MON_STATE:-/home/frank/.hermes/state/stack-health-alerts.txt}"
STATUS_FILE="${STATUS_FILE:-/home/frank/.hermes/state/stack-health.status}"
LOG_FILE="${LOG_FILE:-/home/frank/.hermes/logs/stack-health-audit.log}"
REALERT_SECS="${REALERT_SECS:-21600}"   # re-alert every 6h while a condition persists
# whatsapp:Frank is the consumed surface (see deploy_liveness_monitor.sh, t_ecfeb48c 07-13)
ALERT_TARGET="${STACK_MON_ALERT_TARGET:-whatsapp:Frank}"

mkdir -p "$(dirname "$MON_STATE")" "$(dirname "$LOG_FILE")"
now_epoch=$(date +%s)
now_iso=$(date -Is)

log() { echo "[$now_iso] $*" >> "$LOG_FILE"; }

send_alert() {
    local key="$1" subject="$2" body="$3"
    local last
    last=$(grep -a "^${key}=" "$MON_STATE" 2>/dev/null | tail -1 | cut -d= -f2)
    if [ -n "${last:-}" ] && [ $((now_epoch - last)) -lt "$REALERT_SECS" ]; then
        log "SUPPRESSED key=$key (re-alert window)"
        return 0
    fi
    # 2026-07-29 (opus5): this had a SINGLE target (whatsapp:Frank) and no failover,
    # and it armed the throttle regardless of the send result. WhatsApp has been 100%
    # dead since 07-26 (session logged out), so every stack-health alert since then —
    # including the standing ml-trainer outage — was written to nobody and then
    # suppressed. Chain the fallbacks and only throttle on confirmed delivery.
    local delivered=0 fb rc
    if hermes send -q -t "$ALERT_TARGET" -s "$subject" "$body"; then
        delivered=1
        log "ALERT-SENT target=$ALERT_TARGET key=$key subject=$subject"
    else
        rc=$?
        log "ALERT-FAILED rc=$rc target=$ALERT_TARGET key=$key — check hermes gateway"
        for fb in ${STACK_MON_FALLBACKS:-discord:#critical-alerts telegram:506972405}; do
            if hermes send -q -t "$fb" -s "🔁 FAILOVER: $subject" "$body"; then
                delivered=1
                log "ALERT-FAILOVER-OK target=$fb key=$key"
                break
            else
                rc=$?
                log "ALERT-FAILOVER-FAILED target=$fb rc=$rc key=$key"
            fi
        done
    fi
    if [ "$delivered" -eq 1 ]; then
        grep -a -v "^${key}=" "$MON_STATE" 2>/dev/null > "${MON_STATE}.tmp" || true
        echo "${key}=${now_epoch}" >> "${MON_STATE}.tmp"
        mv "${MON_STATE}.tmp" "$MON_STATE"
    else
        log "ALERT-UNDELIVERED key=$key — all channels failed, throttle NOT armed, retrying next run"
    fi
}

if [ ! -f "$MANIFEST" ]; then
    log "FATAL: manifest missing at $MANIFEST"
    echo "STACK: UNKNOWN (manifest missing) @ $now_iso" > "$STATUS_FILE"
    send_alert "manifest-missing" "[stack-health] manifest missing" \
        "stack-health-audit.sh cannot find $MANIFEST — the stack is UNMONITORED. Restore it (git-ignored host state) or regenerate from docker ps."
    exit 1
fi

# ---- snapshot docker state ---------------------------------------------------
declare -A RUNNING HEALTH
while IFS=$'\t' read -r name state health; do
    RUNNING["$name"]="$state"
    HEALTH["$name"]="$health"
done < <(docker ps -a --format $'{{.Names}}\t{{.State}}\t{{.Status}}' 2>/dev/null)

if [ "${#RUNNING[@]}" -eq 0 ]; then
    log "FATAL: docker ps returned nothing — daemon down or permission lost"
    echo "STACK: UNKNOWN (docker unreachable) @ $now_iso" > "$STATUS_FILE"
    send_alert "docker-unreachable" "[stack-health] docker daemon unreachable" \
        "docker ps returned no containers on the DGX at $now_iso. Daemon may be down — the whole stack is unmonitored and possibly dead."
    exit 1
fi

# ---- evaluate manifest -------------------------------------------------------
missing=()      # expected up, not running, no OFF declaration
unhealthy=()    # running but healthcheck failing / restarting
declared_off=() # documented OFF entries (visible, not alerted)
expected=0

while IFS= read -r raw; do
    line="${raw%%#*}"; line="$(echo "$line" | xargs 2>/dev/null || true)"
    [ -z "$line" ] && continue
    if [[ "$line" == OFF\ * ]]; then
        # OFF <name> <ISO-ts> <who> <reason...>
        declared_off+=("${line#OFF }")
        continue
    fi
    name="$line"; expected=$((expected+1))
    state="${RUNNING[$name]:-absent}"
    if [ "$state" != "running" ]; then
        missing+=("$name (state=$state)")
        continue
    fi
    case "${HEALTH[$name]:-}" in
        *unhealthy*|*Restarting*) unhealthy+=("$name (${HEALTH[$name]})") ;;
    esac
done < "$MANIFEST"

# ---- bridge: active Alertmanager alerts (receivers are commented out) --------
am_alerts=$(python3 - <<'EOF' 2>/dev/null || echo ""
import urllib.request, json
try:
    alerts = json.load(urllib.request.urlopen('http://localhost:9093/api/v2/alerts?active=true&silenced=false', timeout=5))
    lines = [f"{a['labels'].get('alertname','?')} ({a['labels'].get('severity','?')}) since {a['startsAt'][:16]}Z" for a in alerts]
    print('; '.join(lines[:10]))
except Exception:
    print("ALERTMANAGER-UNREACHABLE")
EOF
)

# ---- status file (consumed by seat-live-state.sh at every session boot) ------
ok=$((expected - ${#missing[@]}))
{
    echo "STACK: ${ok}/${expected} expected up, ${#unhealthy[@]} unhealthy, ${#declared_off[@]} declared-off @ $now_iso"
    for m in "${missing[@]}";      do echo "  MISSING: $m"; done
    for u in "${unhealthy[@]}";    do echo "  UNHEALTHY: $u"; done
    for d in "${declared_off[@]}"; do echo "  DECLARED-OFF: $d"; done
    [ -n "$am_alerts" ] && echo "  ALERTMANAGER: $am_alerts"
} > "$STATUS_FILE"

# ---- spool freshness (t_86527603, 2026-08-01): undrained spool = black hole ---
# ~/.hermes/alert-spool/ sat delivered-to-nobody Jul 30 -> Aug 1 (one writer,
# zero readers). Any dir matching **/alert-spool*/ or **/alertmanager-spool/incoming/
# holding a file older than SPOOL_STALE_MIN minutes means its drain/reader is dead.
# Alerts go via the PROVEN spool writer (Alertmanager-webhook JSON + chmod 644 on
# every write, pattern from kanban-audit-chain-monitor.sh; throttle stamped ONLY
# after a successful write). Irony guard: if the stale spool IS the proven one
# (or the write fails), fall through to the send_alert failover chain instead —
# never alert into the very spool that is stuck.
# Env overrides (SPOOL_SCAN_ROOT / PROVEN_SPOOL_DIR) exist for RED-PATH DRILLS
# ONLY — point both at scratch dirs so a drill never posts to Discord.
SPOOL_STALE_MIN="${SPOOL_STALE_MIN:-15}"
SPOOL_SCAN_ROOT="${SPOOL_SCAN_ROOT:-/home/frank/.hermes}"
PROVEN_SPOOL_DIR="${PROVEN_SPOOL_DIR:-/home/frank/.hermes/profiles/jarvis/state/alertmanager-spool/incoming}"

spool_write_alert() {  # $1 = summary
    local summary="$1" ts file
    ts=$(date -u +%FT%TZ)
    file="$PROVEN_SPOOL_DIR/stack-health-spool-freshness-$(date +%s%3N).json"
    mkdir -p "$PROVEN_SPOOL_DIR" || return 1
    python3 - "$file" "$summary" "$ts" <<'PYEOF' || return 1
import json, sys
file, summary, ts = sys.argv[1:4]
payload = {
    "status": "firing",
    "alerts": [{
        "labels": {
            "alertname": "SpoolNotDraining",
            "severity": "critical",
            "job": "stack-health-audit",
        },
        "annotations": {"summary": summary},
        "startsAt": ts,
    }],
}
with open(file, "w") as f:
    json.dump(payload, f)
PYEOF
    # Lesson: chmod on every write or the drain can't read it.
    chmod 644 "$file" 2>/dev/null
    return 0
}

spool_fallback_msg=""
stale_spools=()
proven_is_stale=0
while IFS= read -r d; do
    case "$d" in *archive*|*/.worktrees/*|*node_modules*) continue ;; esac
    stale_n=$(find "$d" -maxdepth 1 -type f -mmin +"$SPOOL_STALE_MIN" 2>/dev/null | wc -l)
    if [ "$stale_n" -gt 0 ]; then
        oldest=$(find "$d" -maxdepth 1 -type f -mmin +"$SPOOL_STALE_MIN" -printf '%T@ %p\n' 2>/dev/null | sort -n | head -1 | cut -d' ' -f2-)
        stale_spools+=("$d: ${stale_n} undrained file(s) >${SPOOL_STALE_MIN}min, oldest=$(basename "${oldest:-unknown}")")
        [ "$d" = "$PROVEN_SPOOL_DIR" ] && proven_is_stale=1
    fi
done < <(find "$SPOOL_SCAN_ROOT" \( -name node_modules -o -name .worktrees -o -name .git \) -prune -o \
           -type d \( -name 'alert-spool*' -o -path '*/alertmanager-spool/incoming' \) -print 2>/dev/null)

if [ ${#stale_spools[@]} -gt 0 ]; then
    spool_msg="SPOOL-BLACKHOLE: alert spool(s) not draining (files >${SPOOL_STALE_MIN}min old): ${stale_spools[*]} — spooled alerts are being delivered to NOBODY; find the dead reader/drain (t_86527603 class)."
    log "SPOOL-STALE: $spool_msg"
    echo "  SPOOL-STALE: ${stale_spools[*]}" >> "$STATUS_FILE"
    # throttle key from dir names only (file details churn between runs)
    skey="spool-stale-$(printf '%s' "${stale_spools[*]%%:*}" | md5sum | cut -c1-12)"
    slast=$(grep -a "^${skey}=" "$MON_STATE" 2>/dev/null | tail -1 | cut -d= -f2)
    if [ -z "${slast:-}" ] || [ $((now_epoch - slast)) -ge "$REALERT_SECS" ]; then
        if [ "$proven_is_stale" -eq 0 ] && spool_write_alert "$spool_msg"; then
            # throttle stamped ONLY after a successful spool write
            grep -a -v "^${skey}=" "$MON_STATE" 2>/dev/null > "${MON_STATE}.tmp" || true
            echo "${skey}=${now_epoch}" >> "${MON_STATE}.tmp"
            mv "${MON_STATE}.tmp" "$MON_STATE"
            log "SPOOL-STALE alert spooled to $PROVEN_SPOOL_DIR key=$skey"
        else
            # proven spool itself stuck, or write failed: route via the
            # send_alert failover chain below (throttles only on delivery).
            spool_fallback_msg="$spool_msg"
        fi
    else
        log "SUPPRESSED key=$skey (re-alert window)"
    fi
fi

# ---- goal-judge liveness (t_9df82b30, 2026-08-02) ----------------------------
# Every goal-mode kanban completion runs through the auxiliary goal judge;
# judge_goal maps ANY transport error to a "continue" verdict and the
# kanban_complete gate fail-closes on it, so a dead judge chain silently
# rejects every goal-mode close fleet-wide (2026-08-02 22:00Z: nous balance
# empty -> paid aux models 404 "requires available credits" -> NotFoundError
# rejections + completion backlog). One synthetic probe through the REAL
# production path (call_llm task="goal_judge", config-resolved provider/model).
# Env overrides exist for RED-PATH DRILLS ONLY (point GOAL_JUDGE_HOME at a
# scratch home with a paid-model pin to force a red).
GOAL_JUDGE_PROBE="${GOAL_JUDGE_PROBE:-/home/frank/.hermes/scripts/goal-judge-liveness.py}"
GOAL_JUDGE_HOME="${GOAL_JUDGE_HOME:-/home/frank/.hermes}"
GOAL_JUDGE_PY="${GOAL_JUDGE_PY:-/home/frank/.hermes/hermes-agent/.venv/bin/python}"
gj_problem=""
if [ -f "$GOAL_JUDGE_PROBE" ] && [ -x "$GOAL_JUDGE_PY" ]; then
    gj_out=$(timeout 90 "$GOAL_JUDGE_PY" "$GOAL_JUDGE_PROBE" "$GOAL_JUDGE_HOME" 2>&1)
    gj_rc=$?
    gj_last="${gj_out##*$'\n'}"
    if [ "$gj_rc" -ne 0 ]; then
        echo "  GOAL-JUDGE: DOWN — ${gj_last:-no output} (rc=$gj_rc)" >> "$STATUS_FILE"
        gj_problem="GOAL-JUDGE DOWN (goal-mode kanban completions fleet-wide are being REJECTED — t_9df82b30 class; check nous balance/model catalog): ${gj_last:-probe produced no output}. "
        log "GOAL-JUDGE-DOWN rc=$gj_rc ${gj_last:-no output}"
    else
        echo "  GOAL-JUDGE: ${gj_last}" >> "$STATUS_FILE"
        log "GOAL-JUDGE-OK ${gj_last}"
    fi
else
    echo "  GOAL-JUDGE: UNMONITORED (probe or venv missing)" >> "$STATUS_FILE"
    gj_problem="GOAL-JUDGE probe missing ($GOAL_JUDGE_PROBE or $GOAL_JUDGE_PY absent) — goal-judge liveness is UNMONITORED. "
    log "GOAL-JUDGE-UNMONITORED probe=$GOAL_JUDGE_PROBE py=$GOAL_JUDGE_PY"
fi

# ---- alerting ----------------------------------------------------------------
problems=""
[ ${#missing[@]}   -gt 0 ] && problems+="MISSING (no OFF declaration): ${missing[*]}. "
[ ${#unhealthy[@]} -gt 0 ] && problems+="UNHEALTHY: ${unhealthy[*]}. "
[ -n "${spool_fallback_msg:-}" ] && problems+="$spool_fallback_msg "
[ -n "${gj_problem:-}" ] && problems+="$gj_problem"
if [ -n "$am_alerts" ] && [ "$am_alerts" != "ALERTMANAGER-UNREACHABLE" ]; then
    # 2026-07-29 (opus5): this used to hardcode "undelivered — receivers unwired".
    # That was true when written (07-13) but the receivers were wired in-repo since,
    # and the claim kept being broadcast in every alert body — a stale assertion
    # dressed as a measurement. Measure it instead: ask Alertmanager whether its
    # webhook notifications are actually succeeding.
    am_sent=$(curl -s --max-time 5 http://localhost:9093/metrics 2>/dev/null \
        | awk '/^alertmanager_notifications_total\{integration="webhook"/ {print int($2)}' | tail -1)
    am_failed=$(curl -s --max-time 5 http://localhost:9093/metrics 2>/dev/null \
        | awk '/^alertmanager_notifications_failed_total\{integration="webhook"/ {s+=int($2)} END{print s+0}')
    if [ -z "${am_sent:-}" ]; then
        am_delivery="delivery UNKNOWN — could not read alertmanager metrics"
    elif [ "${am_sent:-0}" -eq 0 ]; then
        am_delivery="NOT delivered — 0 webhook notifications sent"
    elif [ "${am_failed:-0}" -gt 0 ]; then
        am_delivery="delivery DEGRADED — ${am_failed} failed of ${am_sent} webhook notifications"
    else
        am_delivery="delivered via webhook — ${am_sent} sent, 0 failed"
    fi
    problems+="Alertmanager active (${am_delivery}): $am_alerts. "
elif [ "$am_alerts" = "ALERTMANAGER-UNREACHABLE" ]; then
    problems+="Alertmanager itself unreachable on :9093. "
fi

if [ -n "$problems" ]; then
    key=$(echo -n "$problems" | md5sum | cut -c1-12)
    log "DEGRADED: $problems"
    send_alert "degraded-$key" "[stack-health] DGX stack degraded" \
        "$problems Manifest: $MANIFEST — if any of this is deliberate, add a timestamped OFF line (OFF <name> <ISO> <who> <reason>). Status: $STATUS_FILE"
else
    log "OK: ${ok}/${expected} up, ${#declared_off[@]} declared-off"
fi
