#!/usr/bin/env bash
# sync-pattern-win-rate-registry.sh — canonical no-agent cron entrypoint
#
# Canonical-copy rule: this regular file is the maintained implementation.
# Profile-local cron stores must execute a tiny shim that execs this file.
#
# Purpose: refresh Sycode Trading pattern_win_rate_registry from the latest
# synthetic_labels_*_pattern_win_rates.csv for the PR #348 bridge staleness gate.

set -euo pipefail

SERVER_DIR="/home/frank/sycode-trading/server"
PROJECT_SCRIPT="${SERVER_DIR}/scripts/sync-pattern-win-rate-registry.ts"
BUN_BIN="${BUN_BIN:-/home/frank/.bun/bin/bun}"
ENV_FILE="${SERVER_DIR}/.env"
TS="$(date -Iseconds)"

if [[ ! -d "${SERVER_DIR}" ]]; then
  echo "ERROR: server dir missing: ${SERVER_DIR}" >&2
  exit 1
fi
if [[ ! -f "${PROJECT_SCRIPT}" ]]; then
  # SELF-HEAL (2026-07-13, t_a842317d): the deploy working tree can drift off
  # origin/main (e.g. checked out on a feature branch), in which case this script
  # disappears and the cron hard-fails every 360m -> pattern_win_rate_registry goes
  # stale past its 26h SLO (recurred 2026-07-12 + 2026-07-13). Restore the canonical
  # blob from origin/main WITHOUT switching the working-tree branch. Only acts when
  # the blob exists on origin/main; any other error still fails loudly.
  # PROJECT_SCRIPT is <repo>/server/scripts/...; git paths are relative to repo root.
  REPO_ROOT="$(dirname "${SERVER_DIR}")"
  BLOB_PATH="${PROJECT_SCRIPT#"${REPO_ROOT}/"}"   # -> server/scripts/sync-pattern-win-rate-registry.ts
  echo "WARN: project sync script missing at ${PROJECT_SCRIPT}; attempting self-heal from origin/main (${BLOB_PATH})" >&2
  if command -v git >/dev/null 2>&1 && git -C "${REPO_ROOT}" cat-file -e "origin/main:${BLOB_PATH}" 2>/dev/null; then
    mkdir -p "$(dirname "${PROJECT_SCRIPT}")"
    if git -C "${REPO_ROOT}" show "origin/main:${BLOB_PATH}" >"${PROJECT_SCRIPT}"; then
      echo "SELF-HEAL: restored ${PROJECT_SCRIPT} from origin/main" >&2
    else
      echo "ERROR: self-heal restore failed for ${PROJECT_SCRIPT}" >&2
      exit 1
    fi
  else
    echo "ERROR: project sync script missing and not recoverable from origin/main: ${PROJECT_SCRIPT}" >&2
    exit 1
  fi
fi
if [[ ! -x "${BUN_BIN}" ]]; then
  echo "ERROR: bun not executable at ${BUN_BIN}" >&2
  exit 1
fi

cd "${SERVER_DIR}"

echo "pattern-win-rate-registry sync start ${TS}"
echo "server_dir=${SERVER_DIR}"

# The TypeScript script loads server/.env itself. Parse the same env for the
# post-run psql verification query without shell-sourcing arbitrary .env lines
# and without printing DATABASE_URL.
if [[ -z "${DATABASE_URL:-}" ]]; then
  DATABASE_URL="$(python3 - "${ENV_FILE}" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.exists():
    sys.exit(0)
for raw in p.read_text(errors='ignore').splitlines():
    s = raw.strip()
    if not s or s.startswith('#') or '=' not in s:
        continue
    k, v = s.split('=', 1)
    if k.strip() == 'DATABASE_URL':
        print(v.strip().strip('"').strip("'"))
        break
PY
)"
fi
if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: DATABASE_URL is not available from environment or ${ENV_FILE}" >&2
  exit 1
fi

# HOST-RUNNABILITY FIX (2026-07-12, kanban t_ffc00875):
# This cron runs `bun` on the HOST, but server/.env's DATABASE_URL uses the
# compose-only alias `supabase-db` (resolves only inside the compose network;
# on the host it yields `getaddrinfo ENOTFOUND` -> every run dies, registry
# never refreshes -> staleness). The DB is published to 127.0.0.1:5432, so
# rewrite the alias to the host-local published address. No credentials are
# altered; only the host segment is swapped.
if [[ "${DATABASE_URL}" == *"@supabase-db"* ]]; then
  DATABASE_URL="${DATABASE_URL/@supabase-db/@127.0.0.1}"
fi

export DATABASE_URL

"${BUN_BIN}" run scripts/sync-pattern-win-rate-registry.ts

count="$(PGCONNECT_TIMEOUT=15 psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 -Atc 'SELECT count(*) FROM pattern_win_rate_registry;')"
updated="$(PGCONNECT_TIMEOUT=15 psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 -Atc "SELECT COALESCE(max(last_updated)::text, 'NULL') FROM pattern_win_rate_registry;")"

echo "pattern_win_rate_registry_count=${count}"
echo "pattern_win_rate_registry_max_last_updated=${updated}"
echo "pattern-win-rate-registry sync end $(date -Iseconds)"
