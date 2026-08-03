#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# oob_gateway_liveness_probe.sh — OUT-OF-BAND gateway/cron liveness canary.
# Runs from the SYSTEM crontab (not hermes cron) so it survives gateway death —
# the 2026-07-02 outage proved every in-gateway watchdog dies with its host.
# Observe-only: checks state, writes evidence, alerts via `hermes send`
# (bot-token path; works with no gateway running). Never restarts anything.
#
# Deployed 2026-07-02 by claude-126f49c9 (A2: deterministic no-agent watchdog,
# approvals-registry delegated class). Evidence + design:
# obsidian-fleet-vault/Orchestration/runbooks/goal-orchestrator-operating-runbook.md

set -u
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/home/frank/.local/bin:/home/frank/.hermes/hermes-agent/venv/bin:/usr/bin:/bin:$PATH"
# Force root HERMES_HOME so `hermes send` finds the bot-token platform config
# even when this runs inside a profile-scoped cron (e.g. jarvis-voice).
export HERMES_HOME=/home/frank/.hermes

STATE_FILE="${STATE_FILE:-/home/frank/.hermes/state/oob-canary-state.txt}"
LOG_FILE="${LOG_FILE:-/home/frank/.hermes/logs/oob-gateway-canary.log}"
JOBS_JSON="${JOBS_JSON:-/home/frank/.hermes/profiles/jarvis/cron/jobs.json}"
REALERT_SECS="${REALERT_SECS:-21600}"   # re-alert every 6h while still down
FRESHNESS_SECS="${FRESHNESS_SECS:-2700}"  # newest jarvis cron last_run_at must be < 45 min old
UNITS="${UNITS:-hermes-gateway-jarvis.service hermes-gateway-jarvis-voice.service hermes-gateway-jarvis-os-pm.service hermes-gateway-sycode-trading-pm.service}"
ALERT_TARGET="${OOB_CANARY_ALERT_TARGET:-discord:#critical-alerts}"

mkdir -p "$(dirname "$STATE_FILE")" "$(dirname "$LOG_FILE")"
now_epoch=$(date +%s)
now_iso=$(date -Is)

send_alert() {
    subject="$1"
    body="$2"
    if hermes send -q -t "$ALERT_TARGET" -s "$subject" "$body"; then
        echo "[$now_iso] ALERT-SENT target=$ALERT_TARGET subject=$subject" >> "$LOG_FILE"
        return 0
    fi
    rc=$?
    echo "[$now_iso] ALERT-FAILED target=$ALERT_TARGET rc=$rc subject=$subject" >> "$LOG_FILE"
    # Cross-channel failover for critical alerts. 2026-07-29 (opus5): this used to
    # fail over ONLY to whatsapp:Frank, which has been 100% dead since 07-26 (session
    # logged out) — so a discord outage meant the alert reached nobody. Telegram is
    # appended as a live last resort; whatsapp stays first so it self-heals on re-pair.
    for fb in ${OOB_CANARY_FALLBACKS:-whatsapp:Frank telegram:506972405}; do
        if hermes send -q -t "$fb" -s "🔁 FAILOVER: $subject" "$body"; then
            echo "[$now_iso] ALERT-FAILOVER-OK target=$fb subject=$subject" >> "$LOG_FILE"
            return 0
        fi
        echo "[$now_iso] ALERT-FAILOVER-FAILED target=$fb rc=$? subject=$subject" >> "$LOG_FILE"
    done
    echo "[$now_iso] ALERT-UNDELIVERED subject=$subject — every channel failed" >> "$LOG_FILE"
    return "$rc"
}

problems=""
for u in $UNITS; do
    st=$(systemctl --user is-active "$u" 2>/dev/null || echo "unknown")
    [ "$st" = "active" ] || problems="${problems}${u}=${st}; "
done

# jarvis cron freshness (the 68-job global ticker)
fresh_age=$(python3 - "$JOBS_JSON" <<'EOF' 2>/dev/null || echo 999999
import json, sys, datetime
data = json.load(open(sys.argv[1]))
jobs = data["jobs"] if isinstance(data, dict) and "jobs" in data else data
newest = None
for j in jobs:
    ts = j.get("last_run_at")
    if not ts:
        continue
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except ValueError:
        continue
    if newest is None or dt > newest:
        newest = dt
if newest is None:
    print(999999)
else:
    now = datetime.datetime.now(newest.tzinfo)
    print(int((now - newest).total_seconds()))
EOF
)
[ "$fresh_age" -gt "$FRESHNESS_SECS" ] && problems="${problems}jarvis-cron-stale=${fresh_age}s; "

prev_status="OK"; prev_alert_epoch=0
if [ -f "$STATE_FILE" ]; then
    prev_status=$(sed -n '1p' "$STATE_FILE")
    prev_alert_epoch=$(sed -n '2p' "$STATE_FILE")
    case "$prev_alert_epoch" in ''|*[!0-9]*) prev_alert_epoch=0;; esac
fi

if [ -z "$problems" ]; then
    echo "[$now_iso] OK units-active cron-age=${fresh_age}s" >> "$LOG_FILE"
    if [ "$prev_status" != "OK" ]; then
        send_alert "DGX gateway canary: RECOVERED" \
          "All gateway units active again; jarvis cron age ${fresh_age}s. ($now_iso)"
    fi
    printf 'OK\n0\n' > "$STATE_FILE"
else
    echo "[$now_iso] DOWN $problems" >> "$LOG_FILE"
    if [ "$prev_status" = "OK" ] || [ $((now_epoch - prev_alert_epoch)) -ge $REALERT_SECS ]; then
        if send_alert "DGX gateway canary: FAILURE" \
          "$problems -- in-gateway watchdogs are down with it; manual action needed: systemctl --user start <unit>. ($now_iso)"; then
            printf 'DOWN\n%s\n' "$now_epoch" > "$STATE_FILE"
        else
            printf 'DOWN\n%s\n' "$prev_alert_epoch" > "$STATE_FILE"
        fi
    else
        printf 'DOWN\n%s\n' "$prev_alert_epoch" > "$STATE_FILE"
    fi
fi
exit 0
