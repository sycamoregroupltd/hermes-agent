#!/usr/bin/env bash
# sycode-deploy-locked.sh -- single-deployer mutex for the sycodetrading-server.
#
# WHY: recreating/restarting sycodetrading-server sweeps open PAPER positions and
# wipes in-memory safety state. Two fable seats (claude-fable-dgx, claude-fable-0707)
# plus Frank all hold delegated deploy authority on the SAME box. Without a mutex,
# two concurrent deploys/restarts collide. This wrapper enforces ONE deployer at a
# time via an flock on ~/.hermes/deploy-state/DEPLOY.lock.
#
# FAIL-CLOSED: if the lock is held by another live holder, exit 99 and run NOTHING.
# flock auto-releases when this process exits or dies -> no stale-lock wedge.
#
# USAGE:
#   sycode-deploy-locked.sh --holder <session-id> --intent "<why>" -- <command> [args...]
#
# EXAMPLES:
#   sycode-deploy-locked.sh --holder claude-fable-dgx --intent "gated deploy of origin/main" -- \
#       python3 /home/frank/sycode-trading/execution/deploy_sycodeserver.py
#
#   sycode-deploy-locked.sh --holder claude-fable-0707 --intent "enable #401 injector restart" -- \
#       bash -c 'cd /home/frank/sycode-trading && COMPOSE_PROFILES=prod docker compose up -d --force-recreate server'
#
# OBSERVE the current holder any time:  cat ~/.hermes/deploy-state/DEPLOY.lock.meta
set -euo pipefail

LOCK=/home/frank/.hermes/deploy-state/DEPLOY.lock
META=/home/frank/.hermes/deploy-state/DEPLOY.lock.meta

HOLDER=""
INTENT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --holder) HOLDER="${2:-}"; shift 2 ;;
    --intent) INTENT="${2:-}"; shift 2 ;;
    --) shift; break ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "error: unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$HOLDER" ]] || { echo "error: --holder <session-id> is required" >&2; exit 2; }
[[ $# -gt 0 ]]     || { echo "error: no command given after --" >&2; exit 2; }

mkdir -p "$(dirname "$LOCK")"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[DEPLOY.lock] HELD by another deployer -- REFUSING (fail-closed). Current holder:" >&2
  cat "$META" 2>/dev/null >&2 || echo "(no metadata file)" >&2
  exit 99
fi

# We hold the lock. Record who/why for observability (best-effort).
_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"holder":"%s","pid":%d,"intent":"%s","acquired":"%s"}\n' \
  "$HOLDER" "$$" "${INTENT//\"/\'}" "$_iso" > "$META" 2>/dev/null || true
echo "[DEPLOY.lock] ACQUIRED holder=$HOLDER pid=$$ intent='$INTENT' at ${_iso}" >&2

cleanup() {
  : > "$META" 2>/dev/null || true
  echo "[DEPLOY.lock] released holder=$HOLDER pid=$$" >&2
}
trap cleanup EXIT

set +e
"$@"
rc=$?
set -e
exit "$rc"
