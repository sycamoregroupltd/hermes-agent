#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# system-crontab-watchdog.sh — verifies every script the SYSTEM crontab invokes still exists.
#
# WHY (2026-07-29, opus5 seat): stack-health-audit.sh was deleted around 07-28 21:30.
# Its */10 cron kept firing and erroring "not found" 129 times over ~21h while the
# full-stack liveness audit silently produced nothing — the last health snapshot went
# stale and nobody was told. Only a manual inspection caught it.
#
# COVERAGE GAP THIS CLOSES: the two existing cron watchdogs both look at hermes cron
# stores, not at the host crontab —
#   cron-watchdog.sh          -> ~/.hermes/cron/jobs.json + profiles/*/cron/jobs.json
#   dgx_cron_health_canary.py -> profiles/*/cron/jobs.json (scheduled as cron-health-canary)
# Nothing validated that a `crontab -l` entry still points at a file that exists.
# A vanished script is invisible to cron: it exits 127 and cron considers that a run.
#
# DESIGN: observe-only. Never edits the crontab, never recreates scripts. Silent when
# healthy. Alerts via `hermes send`, throttled one alert per key per REALERT_SECS.
#
# CHECKS, per non-comment crontab line:
#   1. every absolute /path/to/*.sh|*.py token must exist            -> ALERT if missing
#   2. it must be executable (.sh) or readable (.py, run via python3) -> ALERT if not
# Lines containing `docker exec` are skipped for path checks: their paths are
# container-internal, not host paths. They are counted and reported, never alerted on.

set -u
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/home/frank/.local/bin:/home/frank/.hermes/hermes-agent/venv/bin:/usr/bin:/bin:$PATH"
export HERMES_HOME=/home/frank/.hermes

MON_STATE="${MON_STATE:-/home/frank/.hermes/state/system-crontab-watchdog-state.txt}"
LOG_FILE="${LOG_FILE:-/home/frank/.hermes/logs/system-crontab-watchdog.log}"
REALERT_SECS="${REALERT_SECS:-21600}"   # re-alert every 6h while a target stays missing
CRONTAB_CMD="${CRONTAB_CMD:-crontab -l}"
# Same target convention as deploy_liveness_monitor.sh: WhatsApp is Frank's consumed
# surface, discord:#critical-alerts is failover + record.
ALERT_TARGET="${CRONTAB_MON_ALERT_TARGET:-whatsapp:Frank}"

mkdir -p "$(dirname "$MON_STATE")" "$(dirname "$LOG_FILE")"
touch "$MON_STATE"
now_epoch=$(date +%s)
now_iso=$(date -Is)

log() { echo "[$now_iso] $*" >> "$LOG_FILE"; }

send_alert() {
    local key="$1" subject="$2" body="$3" last
    last=$(grep -a "^${key}=" "$MON_STATE" 2>/dev/null | tail -1 | cut -d= -f2)
    if [ -n "${last:-}" ] && [ $((now_epoch - last)) -lt "$REALERT_SECS" ]; then
        log "SUPPRESSED key=$key (re-alert window)"
        return 0
    fi
    local delivered=0 fb
    if hermes send -q -t "$ALERT_TARGET" -s "$subject" "$body"; then
        delivered=1
        log "ALERT-SENT target=$ALERT_TARGET key=$key subject=$subject"
    else
        log "ALERT-FAILED target=$ALERT_TARGET rc=$? key=$key"
        for fb in ${CRONTAB_MON_FALLBACKS:-discord:#critical-alerts telegram:506972405}; do
            if hermes send -q -t "$fb" -s "🔁 FAILOVER: $subject" "$body"; then
                delivered=1
                log "ALERT-FAILOVER-OK target=$fb key=$key"
                break
            fi
            log "ALERT-FAILOVER-FAILED target=$fb rc=$? key=$key"
        done
    fi
    # Arm the re-alert throttle ONLY on confirmed delivery — an alert that reached
    # nobody must not buy 6h of silence. See the same fix in deploy_liveness_monitor.sh.
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

crontab_text=$($CRONTAB_CMD 2>/dev/null) || crontab_text=""
if [ -z "$crontab_text" ]; then
    send_alert crontab_empty "🚨 system crontab EMPTY or unreadable" \
        "\`$CRONTAB_CMD\` returned nothing on $(hostname). Every host-scheduled monitor, deploy and refresh job is gone. Check: crontab -l; ls -la /var/spool/cron/crontabs/"
    log "CRONTAB-EMPTY"
    exit 1
fi
clear_key crontab_empty

checked=0; missing=0; notexec=0; skipped_docker=0
missing_report=""

while IFS= read -r line; do
    case "$line" in ""|\#*) continue ;; esac
    # env-assignment-only lines (FOO=bar) carry no command
    [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*$ ]] && continue
    if echo "$line" | grep -q "docker exec"; then
        skipped_docker=$((skipped_docker + 1))
        continue
    fi
    # A `cd <dir> && ...` prefix makes later relative script paths resolvable.
    workdir=""
    [[ "$line" =~ cd[[:space:]]+(/[A-Za-z0-9._/~-]+) ]] && workdir="${BASH_REMATCH[1]}"

    # Script tokens ending .sh/.py, anchored at a word boundary. The anchor matters:
    # an unanchored /…\.py matches the TAIL of a relative token, so
    # `cd /repo && python3 execution/foo.py` used to yield the bogus path
    # "/foo.py" — a false MISSING on a file that exists. Caught 2026-07-29 by
    # running the watchdog against the live crontab before installing it.
    # Strip any trailing ` # comment` first: those comments routinely name OTHER
    # scripts ("replaces the deleted foo.sh") and scanning them invents missing files.
    cmd="${line%% #*}"
    # NB: strip the regex's captured leading blank per-line with sed, never `tr -d`
    # — tr eats the newlines between matches too and welds two paths into one token.
    for tok in $(echo "$cmd" | grep -aoE '(^|[[:space:]])[A-Za-z0-9._/~-]+\.(sh|py)' | sed 's/^[[:space:]]*//' | sort -u); do
        case "$tok" in
            /*) : ;;                                    # absolute, use as-is
            *)  if [ -n "$workdir" ]; then tok="${workdir}/${tok}"    # relative to `cd`
                else continue; fi ;;                    # unresolvable relative — skip, don't guess
        esac
        checked=$((checked + 1))
        if [ ! -e "$tok" ]; then
            missing=$((missing + 1))
            missing_report="${missing_report}  MISSING: ${tok}"$'\n'
            continue
        fi
        case "$tok" in
            *.sh) [ -x "$tok" ] || { notexec=$((notexec + 1)); missing_report="${missing_report}  NOT-EXECUTABLE: ${tok}"$'\n'; } ;;
            *.py) [ -r "$tok" ] || { notexec=$((notexec + 1)); missing_report="${missing_report}  NOT-READABLE: ${tok}"$'\n'; } ;;
        esac
    done
done <<< "$crontab_text"

if [ "$missing" -gt 0 ] || [ "$notexec" -gt 0 ]; then
    # key on the sorted target list so a NEW breakage re-alerts immediately
    key="targets_$(echo "$missing_report" | sort | md5sum | cut -c1-12)"
    send_alert "$key" "🚨 system crontab: ${missing} missing / ${notexec} unrunnable script(s)" \
"The host crontab invokes script(s) that are gone or unrunnable — those jobs exit 127 every tick and produce nothing, silently:

${missing_report}
Checked ${checked} script target(s) across the crontab. A vanished script is invisible to cron: it counts the failed exec as a run.
Restore from a .bak copy in the same directory if one exists, then confirm the job's output file starts moving again.
Check: crontab -l; ls -la ~/.hermes/scripts/"
    log "UNHEALTHY checked=$checked missing=$missing notexec=$notexec skipped_docker=$skipped_docker"
    echo "[system-crontab-watchdog] UNHEALTHY: ${missing} missing, ${notexec} unrunnable (of ${checked} checked)"
    echo "$missing_report"
    exit 1
fi

log "OK checked=$checked missing=0 notexec=0 skipped_docker=$skipped_docker"
echo "[SILENT] system crontab healthy: ${checked} script target(s) present and runnable (${skipped_docker} docker-exec line(s) not path-checked)"
exit 0
