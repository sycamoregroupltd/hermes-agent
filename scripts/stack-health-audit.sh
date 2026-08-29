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
    # 2026-08-27: also write the alert to the BOARD — the only channel Frank reads.
    # Additive and non-fatal: never let a card write break a monitor.
    "$HOME/.hermes/scripts/fleet-alert-card.sh" "$key" "$subject" "$body" >/dev/null 2>&1 || true
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
# t_cef408bd (2026-08-29): parallel NAME-ONLY arrays. The display arrays above embed
# docker's Status string, which contains an UPTIME DURATION ("Up 2 days (unhealthy)")
# — volatile text that must never reach the alert key. See the "alert key" block.
missing_names=()
unhealthy_names=()
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
        missing_names+=("$name")
        continue
    fi
    case "${HEALTH[$name]:-}" in
        *unhealthy*|*Restarting*) unhealthy+=("$name (${HEALTH[$name]})"); unhealthy_names+=("$name") ;;
    esac
done < "$MANIFEST"

# ---- bridge: active Alertmanager alerts (receivers are commented out) --------
# Two lines out: line 1 = human display (truncated, carries 'since' timestamps),
# line 2 = IDENTITY (t_cef408bd) — the full alertname set, sorted + deduped, with no
# severities, no 'since' timestamps and no [:10] truncation. The display slice is an
# UNORDERED first-10 of a changing list, so it is not a stable identity.
am_probe=$(python3 - <<'EOF' 2>/dev/null || printf '\n'
import urllib.request, json
try:
    alerts = json.load(urllib.request.urlopen('http://localhost:9093/api/v2/alerts?active=true&silenced=false', timeout=5))
    lines = [f"{a['labels'].get('alertname','?')} ({a['labels'].get('severity','?')}) since {a['startsAt'][:16]}Z" for a in alerts]
    names = sorted({a['labels'].get('alertname', '?') for a in alerts})
    print('; '.join(lines[:10]))
    print(','.join(names))
except Exception:
    print("ALERTMANAGER-UNREACHABLE")
    print("ALERTMANAGER-UNREACHABLE")
EOF
)
am_alerts="${am_probe%%$'\n'*}"
am_names="${am_probe#*$'\n'}"
[ "$am_names" = "$am_probe" ] && am_names=""   # probe produced <2 lines

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
gj_class=""   # t_cef408bd: stable class token for the alert key (detail stays in the body)
if [ -f "$GOAL_JUDGE_PROBE" ] && [ -x "$GOAL_JUDGE_PY" ]; then
    gj_out=$(timeout 90 "$GOAL_JUDGE_PY" "$GOAL_JUDGE_PROBE" "$GOAL_JUDGE_HOME" 2>&1)
    gj_rc=$?
    gj_last="${gj_out##*$'\n'}"
    if [ "$gj_rc" -ne 0 ]; then
        echo "  GOAL-JUDGE: DOWN — ${gj_last:-no output} (rc=$gj_rc)" >> "$STATUS_FILE"
        # t_f2360b4e (2026-08-29): this hardcoded "fleet-wide are being REJECTED".
        # Same stale-assertion class the Alertmanager branch was fixed for on 07-29:
        # true when written, then broadcast as fact without being measured. On
        # 2026-08-28 it was FALSE for 6h+ — only the dormant root/default home lost
        # its Nous session (revoked 17:10:19Z, refresh-token reuse) while all 74
        # profile lanes kept a working judge. goal-judge-liveness.py now MEASURES
        # the blast radius and appends it as a SCOPE clause to $gj_last; state the
        # failure and let that measurement speak for the reach.
        gj_problem="GOAL-JUDGE DOWN (goal-mode kanban completions in the probed home are being REJECTED — t_9df82b30 class; reach is measured in the SCOPE clause below): ${gj_last:-probe produced no output}. "
        gj_class="down"
        log "GOAL-JUDGE-DOWN rc=$gj_rc ${gj_last:-no output}"
    else
        echo "  GOAL-JUDGE: ${gj_last}" >> "$STATUS_FILE"
        log "GOAL-JUDGE-OK ${gj_last}"
    fi
else
    echo "  GOAL-JUDGE: UNMONITORED (probe or venv missing)" >> "$STATUS_FILE"
    gj_problem="GOAL-JUDGE probe missing ($GOAL_JUDGE_PROBE or $GOAL_JUDGE_PY absent) — goal-judge liveness is UNMONITORED. "
    gj_class="unmonitored"
    log "GOAL-JUDGE-UNMONITORED probe=$GOAL_JUDGE_PROBE py=$GOAL_JUDGE_PY"
fi

# ---- nous-balance liveness (t_141e28ed, 2026-08-29) --------------------------
# 2026-08-03: nous balance emptied and every PAID-pinned worker exited rc=0
# WITHOUT a kanban lifecycle (protocol_violation) or died pid-not-alive across
# all boards — silently shredding retries. The goal-judge lane was rescued by a
# free pin (tencent/hy3:free); the pool was later re-pinned to
# deepseek/deepseek-v4-flash-0731. An empty balance must ALERT, not silently
# eat retries. One synthetic read of the real Nous Portal usable-credit balance
# through the same production path the 15m guard-bundle watchdog uses
# (hermes_cli.nous_account). Threshold env-overridable; default $5 (watchdog
# parity). This surfaces in the 10-minute audit even though the watchdog also
# runs via guard-bundle-tick-15m.
NOUS_BALANCE_PROBE="${NOUS_BALANCE_PROBE:-/home/frank/.hermes/scripts/nous-balance-liveness.py}"
NOUS_BALANCE_PY="${NOUS_BALANCE_PY:-/home/frank/.hermes/hermes-agent/.venv/bin/python}"
NOUS_BALANCE_THRESHOLD="${NOUS_BALANCE_THRESHOLD_USD:-5.0}"
nb_problem=""
nb_class=""   # stable identity token for the alert key
if [ -f "$NOUS_BALANCE_PROBE" ] && [ -x "$NOUS_BALANCE_PY" ]; then
    nb_out=$(timeout 90 "$NOUS_BALANCE_PY" "$NOUS_BALANCE_PROBE" "$NOUS_BALANCE_THRESHOLD" 2>&1)
    nb_rc=$?
    nb_last="${nb_out##*$'\n'}"
    if [ "$nb_rc" -ne 0 ]; then
        echo "  NOUS-BALANCE: DOWN — ${nb_last:-no output} (rc=$nb_rc)" >> "$STATUS_FILE"
        # The BODY carries the measured usable figure / error; the KEY token is
        # stable so an empty balance creates ONE card, not one per 10-min tick.
        nb_problem="NOUS-BALANCE problem (worker/provider exhaustion risk — t_141e28ed): ${nb_last:-probe produced no output}. "
        nb_class="down"
        log "NOUS-BALANCE-DOWN rc=$nb_rc ${nb_last:-no output}"
    else
        echo "  NOUS-BALANCE: ${nb_last}" >> "$STATUS_FILE"
        log "NOUS-BALANCE-OK ${nb_last}"
    fi
else
    echo "  NOUS-BALANCE: UNMONITORED (probe or venv missing)" >> "$STATUS_FILE"
    nb_problem="NOUS-BALANCE probe missing ($NOUS_BALANCE_PROBE or $NOUS_BALANCE_PY absent) — nous balance liveness is UNMONITORED. "
    nb_class="unmonitored"
    log "NOUS-BALANCE-UNMONITORED probe=$NOUS_BALANCE_PROBE py=$NOUS_BALANCE_PY"
fi

# ---- model-pin drift (t_f21d5a0b, 2026-08-29) --------------------------------
# The fleet was repinned (68 config files) to the DATED build
# deepseek/deepseek-v4-flash-0731 on nous for a 7-day 90%-off promo. Two silent-rot
# classes: (a) served model DRIFTS off the pin (fallback_providers kick in, a profile
# .env overrides, a new profile ships another default) -> we quietly pay list price or
# lose capability; (b) the promo expires and we keep paying full list. model-pin-drift-
# check.py reads session_model_usage across the fleet state DBs (READ-ONLY) and reports
# any nous-billed (model,provider) in the window that is neither the pin nor a declared
# default nor a deliberate exception (free aux tiers, codex/grok/claude seats). Exit
# 1 on drift, 0 clean. The WRAPPER below captures that rc and feeds $problems/
# $problem_id only — it NEVER propagates the checker's nonzero exit, because a --no-agent
# cron would then spam a failure summary on EVERY tick (silent-green contract;
# kanban-audit-chain-monitor.sh pattern). Env overrides (MODEL_PIN_DB / WINDOW_HOURS /
# NO_CONFIG) exist for RED-PATH DRILLS ONLY — point MODEL_PIN_DB at a scratch copy so a
# drill never reads/writes anything but scratch.
MODEL_PIN_PROBE="${MODEL_PIN_PROBE:-/home/frank/.hermes/scripts/model-pin-drift-check.py}"
mp_problem=""
mp_class=""   # stable identity token for the alert key ("drift"|"unmonitored")
if [ -f "$MODEL_PIN_PROBE" ] && [ -x "$MODEL_PIN_PROBE" ]; then
    mp_out=$(timeout 120 "$MODEL_PIN_PROBE" 2>&1)
    mp_rc=$?
    mp_last="${mp_out##*$'\n'}"
    if [ "$mp_rc" -ne 0 ]; then
        # BODY carries the drift detail (model|provider|db|last); KEY token is stable so
        # a persistent drift creates ONE card, not one per 10-min tick.
        mp_problem="MODEL-PIN DRIFT (a served model is off the discounted pin — t_f21d5a0b): ${mp_last:-probe produced no output}. "
        mp_class="drift"
        log "MODEL-PIN-DRIFT rc=$mp_rc :: ${mp_last:-no output}"
    else
        echo "  MODEL-PIN: ${mp_last}" >> "$STATUS_FILE"
        log "MODEL-PIN-CLEAN ${mp_last}"
    fi
else
    echo "  MODEL-PIN: UNMONITORED (probe missing)" >> "$STATUS_FILE"
    mp_problem="MODEL-PIN probe missing ($MODEL_PIN_PROBE absent) — served-model pin drift is UNMONITORED. "
    mp_class="unmonitored"
    log "MODEL-PIN-UNMONITORED probe=$MODEL_PIN_PROBE"
fi

# ---- alerting ----------------------------------------------------------------
# $problems is the human BODY: it may (and should) carry counters, timestamps,
# durations and filenames. $problem_id is the alert KEY input: identity ONLY.
# Never merge the two. See the "alert key" block at the bottom of this file.
problems=""
problem_id=""
[ ${#missing[@]}   -gt 0 ] && problems+="MISSING (no OFF declaration): ${missing[*]}. "
[ ${#unhealthy[@]} -gt 0 ] && problems+="UNHEALTHY: ${unhealthy[*]}. "
[ -n "${spool_fallback_msg:-}" ] && problems+="$spool_fallback_msg "
[ -n "${gj_problem:-}" ] && problems+="$gj_problem"
[ -n "${nb_problem:-}" ] && problems+="$nb_problem"
[ -n "${mp_problem:-}" ] && problems+="$mp_problem"

[ ${#missing_names[@]}   -gt 0 ] && problem_id+="missing=$(printf '%s\n' "${missing_names[@]}"   | sort -u | paste -sd, -);"
[ ${#unhealthy_names[@]} -gt 0 ] && problem_id+="unhealthy=$(printf '%s\n' "${unhealthy_names[@]}" | sort -u | paste -sd, -);"
# spool identity = the stale DIRECTORIES only; the file count and oldest filename churn.
[ -n "${spool_fallback_msg:-}" ] && problem_id+="spool=$(printf '%s\n' "${stale_spools[@]%%:*}" | sort -u | paste -sd, -);"
[ -n "${gj_problem:-}" ] && problem_id+="goal-judge=${gj_class:-unknown};"
[ -n "${nb_problem:-}" ] && problem_id+="nous-balance=${nb_class:-unknown};"
[ -n "${mp_problem:-}" ] && problem_id+="model-pin=${mp_class:-unknown};"
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
    # IDENTITY: the SUB-CHECK "alertmanager has active alerts", not the alert list.
    # Measured 2026-08-29 over the last 150 DEGRADED runs in this log:
    #   md5($problems)              -> 146 distinct keys  (the bug: 157 duplicate cards)
    #   full sorted alertname set   ->  63 distinct keys, 95/149 consecutive flips
    #   critical-only alertname set ->  13 distinct keys, 47/149 consecutive flips
    #   failing sub-check identity  ->   4 distinct keys  <-- chosen
    # Alertmanager's active set flaps by design (that is what Alertmanager is for, and
    # it has its own webhook delivery path: 2800+ notifications sent). Re-exporting that
    # flapping into the card key just re-creates the flood at 1/2 the rate. The alert
    # NAMES are not lost: they are in $problems, i.e. in the card BODY, which is rebuilt
    # every time this key re-fires. $am_names is logged below for forensics.
    problem_id+="am-active;"
elif [ "$am_alerts" = "ALERTMANAGER-UNREACHABLE" ]; then
    problems+="Alertmanager itself unreachable on :9093. "
    problem_id+="am-unreachable;"
fi

# ---- alert key (t_cef408bd, 2026-08-29, fable-devops) ------------------------
# WAS: key=$(echo -n "$problems" | md5sum | cut -c1-12).
# $problems embeds a MONOTONIC alertmanager counter (am_sent), the per-alert
# "since <ISO>" timestamps, docker uptime durations and spool file counts, so the
# hash changed on nearly every 10-minute run. fleet-alert-card.sh supersedes only
# the previous card FOR THE SAME KEY, so the supersede never fired and 157
# duplicate "[host-alert] [stack-health] DGX stack degraded" cards piled up on
# jarvis-os (156 of 170 keys in host-cron-alert-cards.json were degraded-<hash>).
# RULE FOR ANYONE ADDING A SUB-CHECK HERE: append your volatile prose to $problems,
# and append a stable IDENTITY token (names/ids/classes only — no counters, no
# timestamps, no durations, no byte counts, no percentages) to $problem_id.
if [ -n "$problems" ]; then
    if [ -n "$problem_id" ]; then
        key=$(printf '%s' "$problem_id" | md5sum | cut -c1-12)
    else
        # A sub-check appended to $problems without an identity token. Fail to a
        # STABLE key and say so loudly — never fall back to md5("$problems").
        key="unclassified"
        log "KEY-UNCLASSIFIED: \$problems is set but \$problem_id is empty — a sub-check is missing its identity token (t_cef408bd)"
    fi
    log "DEGRADED key=degraded-$key id=[$problem_id] am_names=[${am_names:-}] :: $problems"
    send_alert "degraded-$key" "[stack-health] DGX stack degraded" \
        "$problems Manifest: $MANIFEST — if any of this is deliberate, add a timestamped OFF line (OFF <name> <ISO> <who> <reason>). Status: $STATUS_FILE"
else
    log "OK: ${ok}/${expected} up, ${#declared_off[@]} declared-off"
    # RESOLVE PATH (t_cef408bd, 2026-08-29). Before this, the only thing that ever
    # closed a stack-health card was the SAME key re-firing, so a condition that fixed
    # itself left its card open for ever. Family glob, because the key legitimately
    # changes with the problem class — a clean stack closes whichever one is open.
    # Additive and non-fatal, exactly like the raise path.
    "$HOME/.hermes/scripts/fleet-alert-card.sh" --resolve 'degraded-*' \
        "stack-health CLEAN at ${now_iso}: ${ok}/${expected} expected containers up, 0 unhealthy, ${#declared_off[@]} declared-off, no undrained alert spool, goal-judge probe OK, no active Alertmanager alerts. Status: $STATUS_FILE" \
        >/dev/null 2>&1 || true
fi
