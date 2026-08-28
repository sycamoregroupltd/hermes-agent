#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies.
# vault-autocommit-liveness.sh — proves the obsidian vault autocommit chain is ALIVE.
#
# WHY (2026-08-05, fable seat): the hermes cron job obsidian-vault-git-autocommit
# (jarvis profile, id b2536429e954) died silently twice in one week:
#   1. 2026-08-04 dead-pin: profile script copy deleted -> auto-paused, nobody told.
#   2. 2026-08-05 next_run_at clobber: git checkouts inside the live ~/.hermes repo
#      restored the SANITIZED committed jobs.json (next_run_at stripped for every
#      job), so the scheduler perpetually re-deferred every job to now+interval and
#      NOTHING fired. The vaults are the canonical copies (no remotes); an unnoticed
#      stall means vault history exists only as uncommitted dirty files.
# Fix + monitor land in the same pass (silent-failure doctrine).
#
# CHECKS, per vault:
#   1. newest 'auto-commit vault snapshot' commit on HEAD is younger than
#      VAULT_LIVENESS_MAX_AGE_MIN (default 90; job cadence is 30m).
#   2. checkout is on the vault's expected branch (list entry PATH:BRANCH, else
#      EXPECTED_BRANCH, default main) — the 07-17..08-05 fault put
#      811 commits on a pm/* branch while the nightly Mac bundle backed up a
#      19-day-stale main. Commit-age alone cannot catch this (autocommits land on
#      whatever branch is checked out, so HEAD looks fresh while main rots).
#
# DESIGN: observe-only, silent when healthy. Alerts via `hermes send` with
# discord/telegram failover, throttled per key per REALERT_SECS (armed only on
# confirmed delivery). Same pattern as system-crontab-watchdog.sh.

set -u
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/home/frank/.local/bin:/home/frank/.hermes/hermes-agent/venv/bin:/usr/bin:/bin:$PATH"
export HERMES_HOME=/home/frank/.hermes

# Each entry is PATH or PATH:BRANCH. obsidian/investments was NOT watched here
# until 2026-08-29 (t_de78cf24) — the one vault that actually suffered the 7-day
# silent uncommitted window was the one vault this watchdog could not see, and it
# is on `master`, so it needs a per-vault branch or it would alert forever.
# /home/frank/obsidian/sycode-trading is a SYMLINK to quant-team; listing it would
# double-count one repo, so it is deliberately absent.
VAULTS="${VAULT_LIVENESS_VAULTS:-/home/frank/obsidian-fleet-vault /home/frank/obsidian/quant-team /home/frank/obsidian/investments:master}"
MAX_AGE_MIN="${VAULT_LIVENESS_MAX_AGE_MIN:-90}"
EXPECTED_BRANCH="${VAULT_LIVENESS_EXPECTED_BRANCH:-main}"
MON_STATE="${MON_STATE:-/home/frank/.hermes/state/vault-autocommit-liveness-state.txt}"
LOG_FILE="${LOG_FILE:-/home/frank/.hermes/logs/vault-autocommit-liveness.log}"
REALERT_SECS="${REALERT_SECS:-21600}"   # re-alert every 6h while a vault stays stale
ALERT_TARGET="${VAULT_LIVENESS_ALERT_TARGET:-whatsapp:Frank}"

mkdir -p "$(dirname "$MON_STATE")" "$(dirname "$LOG_FILE")"
touch "$MON_STATE"
now_epoch=$(date +%s)
now_iso=$(date -Is)

log() { echo "[$now_iso] $*" >> "$LOG_FILE"; }

send_alert() {
    local key="$1" subject="$2" body="$3" last
    # 2026-08-27: also write the alert to the BOARD — the only channel Frank reads.
    # Additive and non-fatal: never let a card write break a monitor.
    "$HOME/.hermes/scripts/fleet-alert-card.sh" "$key" "$subject" "$body" >/dev/null 2>&1 || true
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
        for fb in ${VAULT_LIVENESS_FALLBACKS:-discord:#critical-alerts telegram:506972405}; do
            if hermes send -q -t "$fb" -s "🔁 FAILOVER: $subject" "$body"; then
                delivered=1
                log "ALERT-FAILOVER-OK target=$fb key=$key"
                break
            fi
            log "ALERT-FAILOVER-FAILED target=$fb rc=$? key=$key"
        done
    fi
    # Arm the re-alert throttle ONLY on confirmed delivery — an alert that reached
    # nobody must not buy 6h of silence.
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

unhealthy=0
checked=0

for spec in $VAULTS; do
    case "$spec" in
        *:*) vault="${spec%%:*}"; want_branch="${spec#*:}" ;;
        *)   vault="$spec";       want_branch="$EXPECTED_BRANCH" ;;
    esac
    vname=$(basename "$vault")
    if [ ! -d "$vault/.git" ] && [ ! -f "$vault/.git" ]; then
        unhealthy=$((unhealthy + 1))
        send_alert "norepo_${vname}" "🚨 vault autocommit: ${vname} is not a git repo" \
"Vault ${vault} has no .git — the autocommit chain cannot protect it. This vault is a CANONICAL copy (no remote); every unrecorded edit is one disk failure from gone.
Check: ls -la ${vault}; hermes cron list (jarvis profile, job obsidian-vault-git-autocommit b2536429e954)"
        continue
    fi
    clear_key "norepo_${vname}"
    checked=$((checked + 1))

    # 1) autocommit freshness on HEAD (where the script commits)
    last_ct=$(git -C "$vault" log -1 --format=%ct --grep='auto-commit vault snapshot' 2>/dev/null)
    if [ -z "${last_ct:-}" ]; then
        age_min=99999
        last_desc="none found in HEAD history"
    else
        age_min=$(( (now_epoch - last_ct) / 60 ))
        last_desc="$(date -d "@$last_ct" -Is) (${age_min}m ago)"
    fi
    if [ "$age_min" -gt "$MAX_AGE_MIN" ]; then
        unhealthy=$((unhealthy + 1))
        dirty=$(git -C "$vault" status --porcelain 2>/dev/null | wc -l)
        send_alert "stale_${vname}" "🚨 vault autocommit STALE: ${vname} last snapshot ${age_min}m ago (limit ${MAX_AGE_MIN}m)" \
"Newest 'chore(obsidian): auto-commit vault snapshot' commit in ${vault}: ${last_desc}. Job cadence is 30m — the chain is dead or deferred. ${dirty} dirty file(s) are currently UNPROTECTED (vault is canonical, no remote; nightly Mac bundle only captures committed main).
Known kill modes: (a) profile script copy deleted -> dead-pin pause; (b) git checkout inside ~/.hermes clobbers jobs.json next_run_at -> scheduler perpetually re-defers (2026-08-05 incident).
Check: HERMES_PROFILE=jarvis hermes cron status; HERMES_PROFILE=jarvis hermes cron runs b2536429e954; grep -a 'had no next_run_at' ~/.hermes/profiles/jarvis/logs/agent.log | tail
Manual fire: HERMES_PROFILE=jarvis hermes cron run b2536429e954"
    else
        clear_key "stale_${vname}"
    fi

    # 2) branch check — autocommits must land on the branch the bundle backs up
    cur_branch=$(git -C "$vault" rev-parse --abbrev-ref HEAD 2>/dev/null)
    if [ "${cur_branch:-}" != "$want_branch" ]; then
        unhealthy=$((unhealthy + 1))
        send_alert "branch_${vname}" "🚨 vault ${vname} checked out on '${cur_branch:-?}' not '${want_branch}'" \
"Vault ${vault} is on branch '${cur_branch:-?}'. Autocommits land on the CHECKED-OUT branch, but the off-box backup follows ${want_branch} — history accrues off-bundle (the 07-17..08-05 fault stranded 811 commits / 19 days this way).
Fix (only if the branch is a strict descendant): cd ${vault} && git merge-base --is-ancestor ${want_branch} ${cur_branch:-BRANCH} && git checkout ${want_branch} && git merge --ff-only ${cur_branch:-BRANCH}"
    else
        clear_key "branch_${vname}"
    fi
done

if [ "$unhealthy" -gt 0 ]; then
    log "UNHEALTHY vaults_checked=$checked findings=$unhealthy max_age_min=$MAX_AGE_MIN"
    echo "[vault-autocommit-liveness] UNHEALTHY: $unhealthy finding(s) across $checked vault repo(s)"
    exit 1
fi

log "OK vaults_checked=$checked max_age_min=$MAX_AGE_MIN"
echo "[SILENT] vault autocommit healthy: $checked vault(s), newest snapshot within ${MAX_AGE_MIN}m, each on its expected branch"
exit 0
