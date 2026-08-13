#!/bin/bash
# clean-outcome-labeler-24h-v2 — recurring producer (t_378462c4 / P0 t_9fbad511)
# Stamps label_version=v2_2026-07-06_leakfree on mature new-epoch journeys.
# Contract: vault governance/2026-07-06-clean-outcome-labeler-v2-spec-t_378462c4.md
# Silent on success (no-agent cron: empty stdout = no delivery).
# MUST alert on rc=2 (labeler-defined failure) and any other nonzero rc.
set -u

cd /home/frank/sycode-trading/server || { echo "ALERT clean-outcome-labeler-24h-v2: cd failed"; exit 1; }

# ----------------------------------------------------------------------------
# Host-side DB tooling MUST use the HOST-REACHABLE env file (server/.env.prod),
# never the container-only server/.env (whose DATABASE_URL points at the docker
# host 'supabase-db', unreachable from the host where this cron runs).
# Mirrors server/scripts/r-multiple-labeler-recurring.sh (t_e1f68338 / t_1a5b7c48).
#
# PREVIOUS REGRESSION (t_1936c080): the wrapper used a literal *** placeholder
# fallback (`${DATABASE_URL:-postgresql://postgres:***@localhost:5432/postgres}`).
# When the cron host stopped injecting DATABASE_URL into the environment, the
# *** fallback produced silent `password authentication failed` every run,
# starving the 24h label pipeline for 24h+ (binary_backlog grew to ~19k). The
# *** was a redaction artifact, not a credential — fail closed instead of
# emitting a fake auth failure that no-ops silently on a no-agent cron.
# ----------------------------------------------------------------------------
ENV_FILE="/home/frank/sycode-trading/server/.env.prod"
if [ -f "$ENV_FILE" ]; then
  # Source only the host-relevant DB/Redis vars (defensively, with explicit guards).
  set -a
  # shellcheck disable=SC1090
  source <(grep -E '^(DATABASE_URL|REDIS_URL|REDIS_HOST|BULLMQ_REDIS_HOST|BULLMQ_REDIS_PORT|REDIS_PORT|SYCODE_LABELER_DB_POOL_MAX)=' "$ENV_FILE" 2>/dev/null)
  set +a
fi

# Fail closed: never run with a missing or container-only DATABASE_URL.
if [[ -z "${DATABASE_URL:-}" || "$DATABASE_URL" == *"supabase-db"* ]]; then
  echo "ALERT clean-outcome-labeler-24h-v2: host-reachable DATABASE_URL unavailable (need server/.env.prod with @localhost/@127.0.0.1). Refusing to run." >&2
  exit 1
fi

# Redis host-side overrides (cron runs on the host, not in docker).
export BULLMQ_REDIS_HOST="${BULLMQ_REDIS_HOST:-127.0.0.1}"
export BULLMQ_REDIS_PORT="${BULLMQ_REDIS_PORT:-6379}"
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379}"
export REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
export SYCODE_LABELER_DB_POOL_MAX="${SYCODE_LABELER_DB_POOL_MAX:-2}"

OUT=$(CLEAN_OUTCOME_LABELER_EXECUTE=true timeout 600 bun scripts/label-clean-outcomes-24h.ts --execute --json 2>&1)
RC=$?
if [ "$RC" -eq 2 ]; then
  echo "ALERT clean-outcome-labeler-24h-v2 rc=2 (labeler-defined failure — investigate producer): ${OUT:0:800}"
  exit 0   # alert delivered via stdout; don't crash the cron slot
elif [ "$RC" -ne 0 ]; then
  echo "ALERT clean-outcome-labeler-24h-v2 rc=$RC: ${OUT:0:800}"
  exit 0
fi
# success: stay silent unless rows were labeled (surface progress cheaply)
LABELED=$(echo "$OUT" | grep -oE '"(applied|labeled|updated)":[0-9]+' | grep -oE '[0-9]+' | head -1)
if [ -n "${LABELED:-}" ] && [ "$LABELED" -gt 0 ]; then
  echo "clean-outcome-labeler-24h-v2: labeled $LABELED rows this run"
fi
