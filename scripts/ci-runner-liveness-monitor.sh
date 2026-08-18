#!/usr/bin/env bash
# CI runner liveness monitor.
#
# WHY THIS EXISTS (2026-08-10): dgx-ci-8 sat OFFLINE on GitHub for a full week
# while its systemd unit was `active` and its Runner.Listener process was alive.
# Its journal dead-ended at 2026-08-03 22:08. Nothing noticed. Meanwhile
# dgx-ci-2 and dgx-ci-6 were simply stopped. The repo ran at 6/9 capacity while
# a 158-PR backlog was CI-throughput-bound.
#
# THE LESSON THIS ENCODES: a live process is NOT proof of a connected runner.
# The only authority is GitHub's runner API. This monitor compares the API's
# view against local systemd units and alerts on BOTH failure shapes:
#   * offline + unit inactive  -> plain dead runner (start it)
#   * offline + unit ACTIVE    -> ZOMBIE (the silent one; restart it)
#
# Alerts via `hermes send`, same convention as system-crontab-watchdog.sh.
# The re-alert throttle is armed ONLY on confirmed delivery — a failed send must
# never buy itself silence (see the 2026-07 alert-delivery incident).
set -u
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/home/frank/.local/bin:/home/frank/.hermes/hermes-agent/venv/bin:/usr/bin:/bin:$PATH"
export HERMES_HOME=/home/frank/.hermes

REPO="${CI_RUNNER_MON_REPO:-sycamoregroupltd/sycode-trading}"
MIN_ONLINE="${CI_RUNNER_MIN_ONLINE:-8}"     # alert below this many online runners
ALERT_TARGET="${CI_RUNNER_ALERT_TARGET:-discord:#critical-alerts}"
REALERT_SECS="${REALERT_SECS:-21600}"        # 6h per key
LOG_FILE="${CI_RUNNER_MON_LOG:-/home/frank/logs/ci-runner-liveness.log}"
MON_STATE="${CI_RUNNER_MON_STATE:-/home/frank/.hermes/state/ci-runner-liveness-state.txt}"

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$MON_STATE")" 2>/dev/null
touch "$MON_STATE" 2>/dev/null

now_epoch=$(date +%s)
now_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
log() { echo "[$now_iso] $*" >> "$LOG_FILE"; }

send_alert() {
    local key="$1" subject="$2" body="$3" last delivered=0 fb
    last=$(grep -a "^${key}=" "$MON_STATE" 2>/dev/null | tail -1 | cut -d= -f2)
    if [ -n "${last:-}" ] && [ $((now_epoch - last)) -lt "$REALERT_SECS" ]; then
        log "SUPPRESSED key=$key (re-alert window)"; return 0
    fi
    if hermes send -q -t "$ALERT_TARGET" -s "$subject" "$body"; then
        delivered=1; log "ALERT-SENT target=$ALERT_TARGET key=$key"
    else
        log "ALERT-FAILED target=$ALERT_TARGET key=$key"
        for fb in ${CI_RUNNER_MON_FALLBACKS:-discord:#critical-alerts telegram:506972405}; do
            if hermes send -q -t "$fb" -s "🔁 FAILOVER: $subject" "$body"; then
                delivered=1; log "ALERT-FAILOVER-OK target=$fb key=$key"; break
            fi
        done
    fi
    # Arm the throttle ONLY on confirmed delivery.
    [ "$delivered" -eq 1 ] && printf '%s=%s\n' "$key" "$now_epoch" >> "$MON_STATE"
}

# --- authority: GitHub's runner API -----------------------------------------
# Check the EXIT CODE, not just emptiness. Caught by red-test 2026-08-10: a
# non-existent repo still yielded parseable-looking output, and an emptiness-only
# guard reported `online=0/1` with exit 0 — i.e. a failed probe rendered as a
# capacity alert instead of "unknown". A probe that cannot read is never healthy.
api=$(gh api "repos/$REPO/actions/runners" --jq '.runners[] | "\(.name)\t\(.status)"' 2>/dev/null)
api_rc=$?
if [ "$api_rc" -ne 0 ] || [ -z "${api//[[:space:]]/}" ]; then
    log "PROBE-FAILED rc=$api_rc: gh api returned no usable runner list for $REPO"
    send_alert "api_unreachable" "⚠ CI runner monitor cannot read the runner API" \
        "\`gh api repos/$REPO/actions/runners\` failed (rc=$api_rc) or returned nothing on $(hostname) at $now_iso.
Runner state is UNKNOWN, not healthy. Check: gh auth status; gh api repos/$REPO/actions/runners"
    exit 1
fi
# Every line must look like "<name>\t<online|offline>"; anything else means the
# shape changed and we must not silently score it.
if printf '%s\n' "$api" | grep -avqE '^[^[:space:]]+[[:space:]]+(online|offline)$'; then
    log "PROBE-MALFORMED: unexpected runner API shape"
    send_alert "api_malformed" "⚠ CI runner API returned an unexpected shape" \
        "Could not parse the runner list on $(hostname) at $now_iso. Treating as UNKNOWN, not healthy."
    exit 1
fi

online=$(printf '%s\n' "$api" | grep -ac 'online$')
offline_names=$(printf '%s\n' "$api" | grep -a 'offline$' | cut -f1)
total=$(printf '%s\n' "$api" | grep -ac .)

zombies=""; dead=""
for name in $offline_names; do
    unit="actions.runner.$(echo "$REPO" | tr '/' '-').${name}.service"
    if systemctl --user is-active --quiet "$unit" 2>/dev/null; then
        zombies="$zombies $name"      # unit active but GitHub says offline
    else
        dead="$dead $name"
    fi
done

log "online=$online/$total zombies=[${zombies:- none}] dead=[${dead:- none}]"

if [ -n "${zombies// /}" ]; then
    send_alert "zombie_runners" "🧟 CI runner ZOMBIE: unit active but GitHub says offline" \
"Runners:${zombies}
Host: $(hostname)  Time: $now_iso
Online: $online/$total (floor $MIN_ONLINE)

The systemd unit is ACTIVE and the listener process is alive, but GitHub reports the
runner OFFLINE — it is not accepting jobs. This is the silent failure mode that cost a
week of CI capacity on 2026-08-03.

Fix:  systemctl --user restart actions.runner.$(echo "$REPO" | tr '/' '-').<name>.service
Verify: journalctl --user -u <unit> --since -2min | grep 'Connected to GitHub'"
fi

if [ -n "${dead// /}" ]; then
    send_alert "dead_runners" "⚠ CI runners offline (unit not running)" \
"Runners:${dead}
Host: $(hostname)  Online: $online/$total (floor $MIN_ONLINE)
Fix: systemctl --user start actions.runner.$(echo "$REPO" | tr '/' '-').<name>.service"
fi

if [ "$online" -lt "$MIN_ONLINE" ]; then
    send_alert "below_floor" "⚠ CI capacity below floor: $online/$total online" \
"Only $online runners online (floor $MIN_ONLINE) on $(hostname) at $now_iso.
The PR backlog is CI-throughput-bound; degraded capacity stalls the merge train."
fi

exit 0
