#!/usr/bin/env bash
# Repo-built image drift monitor.
#
# WHY (2026-08-10): "production == main" was true for the `server` service ONLY.
# deploy_sycodeserver.py rebuilds nothing else, and only server/Dockerfile
# stamped com.sycodetrading.git.sha — so `web` sat 152 commits / 4 months behind
# with nothing reporting it. Container uptime is the RESTART date and hides this.
#
# Distinguishes three states, because they need different fixes:
#   UNLABELLED -> image carries no sha (cannot be attributed at all)  [PR #1060]
#   DRIFTED    -> labelled, but sha != origin/main
#   CURRENT    -> labelled and equal to origin/main
#
# Same alert convention as ci-runner-liveness-monitor.sh: throttle armed ONLY on
# confirmed delivery, and a probe that cannot read fails LOUD rather than green.
set -u
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/home/frank/.local/bin:/home/frank/.hermes/hermes-agent/venv/bin:/usr/bin:/bin:$PATH"
export HERMES_HOME=/home/frank/.hermes

REPO_DIR="${IMAGE_DRIFT_REPO_DIR:-/home/frank/sycode-trading}"
CONTAINERS="${IMAGE_DRIFT_CONTAINERS:-sycodetrading-server sycodetrading-web sycodetrading-market-data-gateway sycodetrading-mlflow-server}"
ALERT_TARGET="${IMAGE_DRIFT_ALERT_TARGET:-discord:#critical-alerts}"
REALERT_SECS="${REALERT_SECS:-86400}"   # 24h — drift is chronic, not spiky
LOG_FILE="${IMAGE_DRIFT_LOG:-/home/frank/logs/image-drift.log}"
MON_STATE="${IMAGE_DRIFT_STATE:-/home/frank/.hermes/state/image-drift-state.txt}"

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$MON_STATE")" 2>/dev/null
touch "$MON_STATE" 2>/dev/null
now_epoch=$(date +%s); now_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
log() { echo "[$now_iso] $*" >> "$LOG_FILE"; }

send_alert() {
    local key="$1" subject="$2" body="$3" last delivered=0 fb
    # 2026-08-27: also write the alert to the BOARD — the only channel Frank reads.
    # Additive and non-fatal: never let a card write break a monitor.
    "$HOME/.hermes/scripts/fleet-alert-card.sh" "$key" "$subject" "$body" >/dev/null 2>&1 || true
    last=$(grep -a "^${key}=" "$MON_STATE" 2>/dev/null | tail -1 | cut -d= -f2)
    if [ -n "${last:-}" ] && [ $((now_epoch - last)) -lt "$REALERT_SECS" ]; then
        log "SUPPRESSED key=$key"; return 0
    fi
    if hermes send -q -t "$ALERT_TARGET" -s "$subject" "$body"; then
        delivered=1; log "ALERT-SENT key=$key"
    else
        log "ALERT-FAILED key=$key"
        for fb in ${IMAGE_DRIFT_FALLBACKS:-discord:#critical-alerts telegram:506972405}; do
            hermes send -q -t "$fb" -s "🔁 FAILOVER: $subject" "$body" && { delivered=1; log "ALERT-FAILOVER-OK target=$fb"; break; }
        done
    fi
    [ "$delivered" -eq 1 ] && printf '%s=%s\n' "$key" "$now_epoch" >> "$MON_STATE"
}

# origin/main is the reference. A failed fetch/read must not render as "no drift".
git -C "$REPO_DIR" fetch origin main --quiet 2>/dev/null
MAIN=$(git -C "$REPO_DIR" rev-parse refs/remotes/origin/main 2>/dev/null)
if [ -z "${MAIN:-}" ] || [ "${#MAIN}" -ne 40 ]; then
    log "PROBE-FAILED: could not resolve refs/remotes/origin/main"
    send_alert "ref_unreadable" "⚠ Image drift monitor cannot resolve origin/main" \
        "On $(hostname) at $now_iso. Drift state is UNKNOWN, not clean. NOTE: a stray directory named 'origin/' inside the repo makes bare 'origin/main' ambiguous — this script uses refs/remotes/origin/main deliberately."
    exit 1
fi

drifted=""; unlabelled=""; current=0; missing=""
for c in $CONTAINERS; do
    if ! docker inspect "$c" >/dev/null 2>&1; then missing="$missing $c"; continue; fi
    sha=$(docker inspect "$c" --format '{{index .Config.Labels "com.sycodetrading.git.sha"}}' 2>/dev/null)
    if [ -z "${sha:-}" ] || [ "$sha" = "<no value>" ]; then unlabelled="$unlabelled $c"
    elif [ "$sha" != "$MAIN" ]; then
        behind=$(git -C "$REPO_DIR" rev-list --count "$sha..$MAIN" 2>/dev/null || echo '?')
        drifted="$drifted ${c}(${behind}_behind)"
    else current=$((current+1)); fi
done

log "main=${MAIN:0:9} current=$current drifted=[${drifted:- none}] unlabelled=[${unlabelled:- none}] missing=[${missing:- none}]"

[ -n "${drifted// /}" ] && send_alert "image_drift" "⚠ Repo-built images behind main" \
"Drifted:${drifted}
origin/main: ${MAIN:0:9}   Host: $(hostname)   $now_iso
Container uptime is the RESTART date and hides this — trust the sha label, not 'Up N days'."

[ -n "${unlabelled// /}" ] && send_alert "image_unlabelled" "⚠ Repo-built images carry no git sha" \
"Unlabelled:${unlabelled}
These cannot be attributed to any commit. Fix ships in PR #1060 (ARG BUILD_SHA + LABEL in each
final stage); until it merges AND those images are rebuilt, this alert is expected."

exit 0
