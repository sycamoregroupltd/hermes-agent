#!/usr/bin/env bash
#
# arena-insert-liveness-monitor.sh — Hermes cron wrapper (scheduled every 15m)
# Task: t_a20174c0 (Wire Hermes 15m cron for arena-insert-liveness-monitor)
#
# Runs server/scripts/arena-insert-liveness-monitor.ts (branch
# feat/arena-insert-liveness-monitor, commit 8cb31d187) against the live
# sycode-trading Postgres at 127.0.0.1:5432 — the docker port map, NOT the
# container-only 'supabase-db' hostname (per task constraint).
#
# SECRET HYGIENE: the DB password is NEVER stored in this file, the kanban
# card, or the vault. It is read at runtime from the live
# sycodetrading-supabase-db container env (POSTGRES_PASSWORD). The URL is
# assembled in-memory and passed to bun via DATABASE_URL, not as a process
# argument, so it is not exposed in argv.
#
# EXIT-CODE HANDLING (documented for t_a20174c0 acceptance):
#   0 -> healthy (lag <= 30m): wrapper prints NOTHING -> no_agent stays silent.
#   1 -> stale/unreachable: wrapper forwards the monitor output so Hermes
#        delivers the ALERT to the configured channel(s).
#   2 -> config/runtime error: wrapper forwards the diagnostic output so Hermes
#        delivers the error alert.
# The monitor itself is SELECT-only under assertReadOnlySql: no DDL, no writes,
# no live-trading action, no deploy.
#
set -uo pipefail

BUN_BIN="/home/frank/.bun/bin/bun"
# Worktree may be re-created/removed across deploys; allow override for eval/test
# resilience (eval-runner simulates stale via threshold 0m without a live DB).
WORKTREE="${ARENA_LIVENESS_WORKTREE:-/home/frank/sycode-trading/.worktrees/arena-liveness-monitor}"
SCRIPT="${WORKTREE}/server/scripts/arena-insert-liveness-monitor.ts"
PG_CONTAINER="sycodetrading-supabase-db"
MAX_AGE_MINUTES="${ARENA_LIVENESS_MAX_AGE_MINUTES:-30}"

# Discord alert delivery (task t_25fa3fa1): Frank chose Discord. Hermes
# `deliver: "all"` does not reach Discord (platform not registered), so the
# wrapper delivers directly. ARENA_ALERT_DISCORD_WEBHOOK / DISCORD_WEBHOOK_URL
# / ARENA_ALERT_DISCORD_RELAY resolve the target; local JSON audit is always
# written. The helper is SELECT-only safe (outbound POST + local file only).
DELIVER_HELPER="/home/frank/.hermes/profiles/devops/scripts/arena-alert-deliver.sh"

# Select-only monitor; --no-write keeps the feature worktree clean (no report
# files accumulating every 15m). Alerting is driven by the exit code + delivered
# stdout, which is sufficient for the watchdog contract.
NO_WRITE="${ARENA_LIVENESS_NO_WRITE:-1}"
EXTRA_ARGS=()
if [ "${NO_WRITE}" = "1" ]; then
  EXTRA_ARGS+=(--no-write)
fi

# Resolve the password at runtime from the live DB container (no literal secret).
DB_PASS="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$PG_CONTAINER" 2>/dev/null | grep '^POSTGRES_PASSWORD=' | cut -d= -f2-)"
if [ -z "$DB_PASS" ]; then
  echo "[arena-liveness-cron] CONFIG ERROR: could not read POSTGRES_PASSWORD from container ${PG_CONTAINER}" >&2
  exit 2
fi

# Task-mandated host/port: docker maps 5432/tcp -> 127.0.0.1:5432.
DB_PASS_ENCODED="$(DB_PASS="$DB_PASS" python3 -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["DB_PASS"], safe=""))')"
DATABASE_URL="postgresql://postgres:${DB_PASS_ENCODED}@127.0.0.1:5432/postgres"

# Run from the worktree so bun resolves node_modules (pg) the same way the
# verified manual test did.
cd "$WORKTREE" || { echo "[arena-liveness-cron] CONFIG ERROR: worktree ${WORKTREE} missing" >&2; exit 2; }

OUT="$(DATABASE_URL="$DATABASE_URL" $BUN_BIN run "$SCRIPT" --max-age-minutes="$MAX_AGE_MINUTES" "${EXTRA_ARGS[@]}" 2>&1)"
RC=$?

if [ "$RC" -eq 0 ]; then
  # Healthy: stay silent so the 15m cadence does not spam delivery channels.
  exit 0
fi

# Non-zero (stale/unreachable=1, config/runtime error=2): surface the monitor's
# diagnostic output AND deliver a Discord alert (Frank's chosen channel, t_25fa3fa1).
# Hermes `deliver: "all"` does not reach Discord (platform not registered), so we
# deliver directly via the helper, which is SELECT-only safe.
echo "$OUT"
# Deliver to Discord through the helper. ARENA_ALERT_EXIT_CODE tags the severity;
# the helper resolves the webhook (DISCORD_WEBHOOK_URL / relay) and always writes a
# local JSON artifact. Delivery failure must NOT mask the monitor's own exit code.
if [ -x "$DELIVER_HELPER" ]; then
  ARENA_ALERT_EXIT_CODE="$RC" ARENA_ALERT_MONITOR="arena-insert-liveness-monitor" \
    echo "$OUT" | "$DELIVER_HELPER" --exit "$RC" --monitor "arena-insert-liveness-monitor" \
    || echo "[arena-liveness-cron] WARN: alert delivery helper returned non-zero (alert may not have reached Discord); local artifact still written" >&2
else
  echo "[arena-liveness-cron] WARN: $DELIVER_HELPER missing; no Discord delivery" >&2
fi
exit "$RC"
