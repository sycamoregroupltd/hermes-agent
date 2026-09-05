#!/usr/bin/env bash
# CI runner cap reconciler (Option A, DGX CI cap-drift architecture packet
# t_a1a791f1 / harvest t_46609036 / this-card t_44097b86).
#
# WHY THIS EXISTS (2026-09-05): ci-runner-liveness-monitor.sh is floor-
# asymmetric: it alerts below a MIN_ONLINE floor and tells a human to
# `systemctl --user start` dead runners, but has NO path to bring an
# over-cap pool back down. The only "cap" that existed before this script
# was a prose marker file (state/ci-runner-cap-2026-09-02.txt) that nothing
# ever read. Result: cap was set to 2 on 2026-09-02, and by 2026-09-05 all
# 9 dgx-ci-* runners were online+busy again with nothing enforcing the cap
# in either direction.
#
# THIS SCRIPT enforces a SINGLE machine-readable cap file in BOTH
# directions:
#   - under cap  -> start stopped (dead, non-zombie) units up to the cap
#   - over cap   -> stop IDLE (non-busy) units down to the cap; a busy
#                   Runner.Worker is NEVER stopped ("drain-not-kill")
#
# SAFETY / ISOLATION (card t_44097b86, HARD gate):
#   - Default mode is DRY-RUN. No systemctl call happens unless BOTH
#     `--apply` is passed AND CI_RECONCILER_APPLY_CONFIRM is set to the
#     exact token below. This is a second, independent safety gate on top
#     of the CLI flag so this script can be reviewed, tested, and even
#     committed live without ever being able to accidentally mutate a
#     runner from a stray `--apply`.
#   - The deployer runner (label "deployer", name sycodetrading-deployer)
#     is excluded from the pool unconditionally and is never planned on.
#   - Busy runners are never selected for STOP, no matter how far over cap
#     the pool is. If there are not enough idle runners to reach the cap,
#     the script reports "deferred" and performs zero STOP actions rather
#     than force-stopping in-flight work.
#   - This script has NOT been installed to any cron/systemd timer and is
#     NOT live. It exists only in this git worktree/branch pending
#     os-reviewer review.
#
# TESTABILITY: every external read (GitHub runner API, systemd unit list)
# can be overridden with a fixture file so tests never touch the live host.
# See tests/test_ci_runner_cap_reconciler.sh.
set -u

# --- configuration (env-overridable for prod use and tests) -----------------
REPO="${CI_RECONCILER_REPO:-sycamoregroupltd/sycode-trading}"
CAP_FILE="${CI_RECONCILER_CAP_FILE:-/home/frank/.hermes/state/ci-runner-cap.conf}"
POOL_LABEL="${CI_RECONCILER_POOL_LABEL:-ci}"           # GH runner label that marks the reconciler's pool
EXCLUDE_LABEL="${CI_RECONCILER_EXCLUDE_LABEL:-deployer}" # never touch runners carrying this label
LOG_FILE="${CI_RECONCILER_LOG:-/home/frank/logs/ci-runner-cap-reconciler.log}"

# Test/fixture overrides. When set, these REPLACE the live `gh api` /
# `systemctl --user list-units` calls entirely — no network or systemd
# access happens when a fixture is provided.
RUNNERS_JSON_FIXTURE="${CI_RECONCILER_RUNNERS_JSON:-}"     # path to a `gh api .../actions/runners` JSON body
UNITS_FIXTURE="${CI_RECONCILER_UNITS_FIXTURE:-}"           # path; lines "<unit> <active|inactive>"

# Second independent safety gate for real mutation (see header). Must match
# EXACTLY; there is no default value, so it can never be true by accident.
APPLY_CONFIRM_TOKEN="I-UNDERSTAND-THIS-STOPS-STARTS-LIVE-CI-RUNNERS"

APPLY=0
VERBOSE=0
for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=1 ;;
        --verbose) VERBOSE=1 ;;
        --repo=*) REPO="${arg#--repo=}" ;;
        --cap-file=*) CAP_FILE="${arg#--cap-file=}" ;;
        -h|--help)
            cat <<'EOF'
Usage: ci-runner-cap-reconciler.sh [--apply] [--verbose] [--repo=OWNER/REPO] [--cap-file=PATH]

Dry-run by default: prints the plan, mutates nothing.
--apply alone is NOT enough to mutate anything: CI_RECONCILER_APPLY_CONFIRM
must also be set to the exact confirmation token in the script header.
EOF
            exit 0
            ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(now_iso)] $*" | tee -a "$LOG_FILE" >&2; }

# --- 1. cap: single source of truth ------------------------------------------
# Format: a line `CAP=<int>`; blank lines and lines starting with # ignored.
# This REPLACES the old prose marker file — nothing reads that one; this
# script reads only CAP_FILE.
read_cap() {
    if [ ! -f "$CAP_FILE" ]; then
        log "FATAL: cap file not found: $CAP_FILE"
        return 1
    fi
    local cap
    cap=$(grep -E '^CAP=[0-9]+$' "$CAP_FILE" | tail -1 | cut -d= -f2)
    if [ -z "$cap" ]; then
        log "FATAL: cap file $CAP_FILE has no valid CAP=<int> line"
        return 1
    fi
    printf '%s' "$cap"
}

# --- 2. runner pool: GitHub's runner API is the authority --------------------
# Same lesson ci-runner-liveness-monitor.sh already encodes: a live systemd
# unit is not proof of a connected runner. Filter to POOL_LABEL, exclude
# EXCLUDE_LABEL (the deployer) by label, not by name-guessing.
fetch_runners_json() {
    if [ -n "$RUNNERS_JSON_FIXTURE" ]; then
        if [ ! -f "$RUNNERS_JSON_FIXTURE" ]; then
            log "FATAL: fixture not found: $RUNNERS_JSON_FIXTURE"
            return 1
        fi
        cat "$RUNNERS_JSON_FIXTURE"
        return 0
    fi
    gh api "repos/$REPO/actions/runners" 2>/dev/null
}

# Emits TSV: name<TAB>status<TAB>busy   for the in-scope pool only.
pool_rows() {
    local json rc
    json=$(fetch_runners_json)
    rc=$?
    if [ "$rc" -ne 0 ] || [ -z "$json" ]; then
        log "FATAL: could not read runner list (rc=$rc)"
        return 1
    fi
    printf '%s' "$json" | jq -r --arg pool "$POOL_LABEL" --arg excl "$EXCLUDE_LABEL" '
        .runners[]
        | select([.labels[].name] | index($excl) | not)
        | select([.labels[].name] | index($pool))
        | [.name, .status, (.busy | tostring)]
        | @tsv
    '
}

# --- 3. systemd unit resolution ----------------------------------------------
# Two live naming schemes coexist on this host (confirmed 2026-09-05 via
# `systemctl --user list-units '*runner*'`):
#   actions.runner.<repo-dashed>.<gh-name>.service   (dgx-ci-4..9)
#   gha-runner-<suffix>.service                      (dgx-ci-1..3, suffix = gh-name minus "dgx-")
# Do not assume either pattern; resolve against the live/fixture unit list.
all_units_lines() {
    if [ -n "$UNITS_FIXTURE" ]; then
        if [ ! -f "$UNITS_FIXTURE" ]; then
            log "FATAL: units fixture not found: $UNITS_FIXTURE"
            return 1
        fi
        cat "$UNITS_FIXTURE"
        return 0
    fi
    # "<unit> <active|inactive>" per line, load-state agnostic.
    systemctl --user list-units --all --no-legend --plain '*.service' 2>/dev/null \
        | awk '{print $1, $3}'
}

resolve_unit() {
    local gh_name="$1" repo_dashed all_lines suffix cand
    repo_dashed=$(echo "$REPO" | tr '/' '-')
    suffix="${gh_name#dgx-}"
    all_lines=$(all_units_lines) || return 1
    for cand in "actions.runner.${repo_dashed}.${gh_name}.service" "gha-runner-${suffix}.service"; do
        if printf '%s\n' "$all_lines" | awk -v u="$cand" '$1==u{found=1} END{exit !found}'; then
            echo "$cand"
            return 0
        fi
    done
    return 1
}

unit_active() {
    local unit="$1" all_lines
    all_lines=$(all_units_lines) || return 1
    printf '%s\n' "$all_lines" | awk -v u="$unit" '$1==u{print $2; found=1} END{exit !found}'
}

# --- 4. build the plan --------------------------------------------------------
# Populates two newline-separated globals: PLAN_STOP, PLAN_START
# (each "gh_name<TAB>unit").
build_plan() {
    local cap idle_online_names="" dead_units_ordered=""
    cap=$(read_cap) || return 1
    ONLINE_COUNT=0
    PLAN_STOP=""
    PLAN_START=""
    UNRESOLVED=""
    ZOMBIES=""

    local rows
    rows=$(pool_rows) || return 1
    if [ -z "$rows" ]; then
        log "FATAL: pool is empty after label filtering (label=$POOL_LABEL) — refusing to act on an empty/mis-scoped pool"
        return 1
    fi

    # Pass 1: classify every pool member.
    local name status busy unit active_state
    while IFS=$'\t' read -r name status busy; do
        [ -z "$name" ] && continue
        if [ "$status" = "online" ]; then
            ONLINE_COUNT=$((ONLINE_COUNT + 1))
            if [ "$busy" = "false" ]; then
                idle_online_names="${idle_online_names}${name}\n"
            fi
        else
            # offline: resolve unit, classify dead (inactive, startable) vs
            # zombie (active but GitHub says offline — out of scope here;
            # that's the liveness monitor's restart-path job, not ours).
            unit=$(resolve_unit "$name")
            if [ -z "$unit" ]; then
                UNRESOLVED="${UNRESOLVED}${name}\n"
                continue
            fi
            active_state=$(unit_active "$unit")
            if [ "$active_state" = "active" ]; then
                ZOMBIES="${ZOMBIES}${name} (${unit})\n"
            else
                dead_units_ordered="${dead_units_ordered}${name}\t${unit}\n"
            fi
        fi
    done <<< "$rows"

    CAP="$cap"

    if [ "$ONLINE_COUNT" -gt "$cap" ]; then
        local excess=$((ONLINE_COUNT - cap))
        local candidates
        candidates=$(printf '%b' "$idle_online_names" | grep -v '^$' | sort)
        local picked=0
        while IFS= read -r name; do
            [ -z "$name" ] && continue
            [ "$picked" -ge "$excess" ] && break
            unit=$(resolve_unit "$name") || { UNRESOLVED="${UNRESOLVED}${name}\n"; continue; }
            PLAN_STOP="${PLAN_STOP}${name}\t${unit}\n"
            picked=$((picked + 1))
        done <<< "$candidates"
        DEFERRED_OVER=$((excess - picked))
    else
        DEFERRED_OVER=0
    fi

    if [ "$ONLINE_COUNT" -lt "$cap" ]; then
        local need=$((cap - ONLINE_COUNT))
        local picked=0
        while IFS=$'\t' read -r name unit; do
            [ -z "$name" ] && continue
            [ "$picked" -ge "$need" ] && break
            PLAN_START="${PLAN_START}${name}\t${unit}\n"
            picked=$((picked + 1))
        done < <(printf '%b' "$dead_units_ordered" | grep -v '^$' | sort)
        UNMET_UNDER=$((need - picked))
    else
        UNMET_UNDER=0
    fi

    return 0
}

# --- 5. report / act ----------------------------------------------------------
print_plan() {
    echo "== ci-runner-cap-reconciler plan (repo=$REPO cap=$CAP online=$ONLINE_COUNT) =="
    if [ -n "$PLAN_STOP" ]; then
        echo "STOP (idle, over cap):"
        printf '%b' "$PLAN_STOP" | grep -v '^$' | while IFS=$'\t' read -r n u; do echo "  - $n -> $u"; done
    fi
    if [ -n "$PLAN_START" ]; then
        echo "START (dead, under cap):"
        printf '%b' "$PLAN_START" | grep -v '^$' | while IFS=$'\t' read -r n u; do echo "  - $n -> $u"; done
    fi
    if [ "${DEFERRED_OVER:-0}" -gt 0 ]; then
        echo "DEFERRED: ${DEFERRED_OVER} runner(s) still over cap but only busy candidates remain — drain-not-kill, no STOP issued for them."
    fi
    if [ "${UNMET_UNDER:-0}" -gt 0 ]; then
        echo "UNMET: ${UNMET_UNDER} runner(s) still needed to reach cap but no dead (startable) unit was found."
    fi
    if [ -n "$ZOMBIES" ]; then
        echo "ZOMBIES (out of scope — unit active, GitHub offline; use ci-runner-liveness-monitor.sh restart path):"
        printf '%b' "$ZOMBIES" | grep -v '^$' | while IFS= read -r z; do echo "  - $z"; done
    fi
    if [ -n "$UNRESOLVED" ]; then
        echo "UNRESOLVED (no matching systemd unit found — resolve manually before acting):"
        printf '%b' "$UNRESOLVED" | grep -v '^$' | while IFS= read -r r; do echo "  - $r"; done
    fi
    if [ -z "$PLAN_STOP" ] && [ -z "$PLAN_START" ]; then
        if [ "${DEFERRED_OVER:-0}" -gt 0 ]; then
            echo "no immediate action: all over-cap excess is busy (drain-not-kill deferred, see DEFERRED above)."
        elif [ "${UNMET_UNDER:-0}" -gt 0 ]; then
            echo "no immediate action: under cap but no startable dead unit found (see UNMET above)."
        else
            echo "cap enforced: online=$ONLINE_COUNT matches cap=$CAP, no action needed."
        fi
    fi
}

# Alert-body text ready to slot into the existing send_alert() convention
# (ci-runner-liveness-monitor.sh / fleet-alert-card.sh). NOT sent by this
# script — sending real alerts from an unreviewed candidate is out of
# scope for this card; wiring is left for the reviewed follow-up.
alert_text() {
    if [ -n "$PLAN_STOP" ] || [ -n "$PLAN_START" ] || [ "${DEFERRED_OVER:-0}" -gt 0 ]; then
        echo "cap enforced: drifted ${ONLINE_COUNT}->${CAP} (repo=$REPO). $( [ -n "$PLAN_STOP" ] && echo "stopping $(printf '%b' "$PLAN_STOP" | grep -vc '^$') idle runner(s). " )$( [ -n "$PLAN_START" ] && echo "starting $(printf '%b' "$PLAN_START" | grep -vc '^$') dead runner(s). " )$( [ "${DEFERRED_OVER:-0}" -gt 0 ] && echo "${DEFERRED_OVER} still over cap, all busy, deferred (drain-not-kill)." )"
    fi
}

apply_plan() {
    if [ "$APPLY" -ne 1 ]; then
        return 0
    fi
    if [ "${CI_RECONCILER_APPLY_CONFIRM:-}" != "$APPLY_CONFIRM_TOKEN" ]; then
        log "SAFETY: --apply given but CI_RECONCILER_APPLY_CONFIRM does not match the required token. Refusing to mutate anything (dry-run only)."
        return 0
    fi
    local name unit
    if [ -n "$PLAN_STOP" ]; then
        while IFS=$'\t' read -r name unit; do
            [ -z "$name" ] && continue
            log "APPLY: systemctl --user stop $unit (runner $name, idle, over cap)"
            systemctl --user stop "$unit"
        done < <(printf '%b' "$PLAN_STOP" | grep -v '^$')
    fi
    if [ -n "$PLAN_START" ]; then
        while IFS=$'\t' read -r name unit; do
            [ -z "$name" ] && continue
            log "APPLY: systemctl --user start $unit (runner $name, dead, under cap)"
            systemctl --user start "$unit"
        done < <(printf '%b' "$PLAN_START" | grep -v '^$')
    fi
}

main() {
    if ! build_plan; then
        echo "reconciler: FAILED to build plan (see log: $LOG_FILE)" >&2
        exit 1
    fi
    print_plan
    local at
    at=$(alert_text)
    [ -n "$at" ] && [ "$VERBOSE" -eq 1 ] && echo "ALERT-TEXT (not sent): $at"
    apply_plan
    exit 0
}

main
