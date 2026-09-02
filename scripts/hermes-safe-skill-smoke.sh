#!/usr/bin/env bash
# Run a read-only Hermes skill preload smoke outside worker context.
set -euo pipefail

usage() {
    printf 'usage: %s SKILL\n' "$0" >&2
    exit 64
}

if (( $# != 1 )); then
    usage
fi

skill=$1
if [[ ! $skill =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
    printf 'safe skill smoke: invalid skill name: %s\n' "$skill" >&2
    exit 64
fi

# A dispatcher-owned worker must never be able to make this smoke claim its
# task, board, workspace, or run. Fail closed rather than guessing how to
# detach a partially inherited worker context.
worker_env_keys=(
    HERMES_KANBAN_TASK
    HERMES_KANBAN_BOARD
    HERMES_KANBAN_DB
    HERMES_KANBAN_WORKSPACE
    HERMES_KANBAN_RUN_ID
    HERMES_KANBAN_CLAIM_LOCK
    HERMES_KANBAN_WORKSPACES_ROOT
    HERMES_KANBAN_LOGS_ROOT
    HERMES_KANBAN_HOME
    HERMES_KANBAN_DISPATCH_IN_GATEWAY
    HERMES_SESSION_SOURCE
    HERMES_TENANT
    HERMES_DELEGATED_CHILD_CONTEXT
)

inherited=()
for key in "${worker_env_keys[@]}"; do
    if [[ ${!key+x} ]]; then
        inherited+=("$key")
    fi
done
# Keep this future-proof for newly added HERMES_KANBAN_* worker variables.
while IFS= read -r key; do
    case $key in
        HERMES_KANBAN_*)
            if [[ ! " ${inherited[*]} " == *" $key "* ]]; then
                inherited+=("$key")
            fi
            ;;
    esac
done < <(compgen -v)

if (( ${#inherited[@]} )); then
    printf 'safe skill smoke: refusing inherited worker environment: %s\n' \
        "${inherited[*]}" >&2
    printf 'run from a clean shell after unsetting worker variables\n' >&2
    exit 78
fi

hermes_bin=${HERMES_SAFE_SMOKE_BIN:-hermes}
if [[ $hermes_bin == */* && ! -x $hermes_bin ]]; then
    printf 'safe skill smoke: Hermes executable is not executable: %s\n' "$hermes_bin" >&2
    exit 69
fi

# Explicitly remove selectors/context even when a clean caller supplied them;
# the only enabled toolset is the empty list, so Kanban mutation tools cannot
# be exposed to the smoke process.
unset_args=()
for key in "${worker_env_keys[@]}" HERMES_PROFILE; do
    unset_args+=("-u" "$key")
done

exec env "${unset_args[@]}" "$hermes_bin" \
    --accept-hooks \
    --skills "$skill" \
    --toolsets "" \
    chat -q 'Return exactly HERMES_SAFE_SKILL_SMOKE_PASS. Do not use tools.'
