#!/bin/bash
# clean-outcome-labeler-24h-v2 — recurring producer (t_378462c4 / P0 t_9fbad511)
# Stamps label_version=v2_2026-07-06_leakfree on mature new-epoch journeys.
# Contract: vault governance/2026-07-06-clean-outcome-labeler-v2-spec-t_378462c4.md
# Silent on success (no-agent cron: empty stdout = no delivery).
# MUST alert on rc=2 (PR #379 merge condition 3) and any other nonzero rc.
set -u
cd /home/frank/sycode-trading/server || { echo "ALERT clean-outcome-labeler-24h-v2: cd failed"; exit 1; }
# Host-side run: server/.env pins the docker-network host `supabase-db` (unresolvable
# from the host). Override with the host-reachable URL, same DB (see server/.env.prod).
# Host-side run: server/.env pins docker-network hostnames (unresolvable
# from the host). Override with host-reachable URLs (root CAUTION: do not
# sed the canonical server/.env — these are per-run env overrides).
#   - DATABASE_URL -> localhost (TCP by-passes Kong; the wrapper already did this).
#   - Redis -> 127.0.0.1:6379 (the docker-published host port).
export DATABASE_URL="${DATABASE_URL:-postgresql://postgres:***@localhost:5432/postgres}"
export BULLMQ_REDIS_HOST="${BULLMQ_REDIS_HOST:-127.0.0.1}"
export BULLMQ_REDIS_PORT="${BULLMQ_REDIS_PORT:-6379}"
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379}"
export REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
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
