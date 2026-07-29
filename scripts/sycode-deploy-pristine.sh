#!/usr/bin/env bash
# sycode-deploy-pristine.sh — deploy the sycodetrading-server from a PRISTINE deploy-owned
# git worktree, immune to GAP1 (the shared checkout /home/frank/sycode-trading being flipped
# to feature branches / dirtied by concurrent dispatched workers, which repeatedly blocks
# deploy_sycodeserver.py's source-pin/worktree gates).
#
# HOW: maintains a dedicated worktree the workers never touch, resets it --hard to origin/main
# each run, stages the gitignored runtime env files compose needs, then runs the gated deploy
# pointed at it (via SYCODE_DEPLOY_REPO_DIR/SYCODE_DEPLOY_BRANCH) UNDER the DEPLOY.lock.
#
# SAFETY: the deploy's own gates still run (source-pin refuses anything ahead of origin/main;
# positions/spacing/migration/health/rollback all unchanged). Requires a deploy_sycodeserver.py
# that honors SYCODE_DEPLOY_REPO_DIR/SYCODE_DEPLOY_BRANCH (env-overridable, default = today's
# values); on an OLDER script the env vars are simply ignored (harmless no-op, falls back to the
# shared checkout).
#
# STATUS: NEW mechanism — its FIRST real deploy must be a supervised validation (confirm the
# staged env-file set is complete for `docker compose up`). --check-only is always safe.
#
# Usage: sycode-deploy-pristine.sh [--holder <id>] [-- <extra deploy_sycodeserver.py args>]
set -euo pipefail

# --- cron-environment fixes (fable 2026-07-28) --------------------------------
# Both faults below are CRON-ONLY: a manual deploy from an interactive shell
# inherits a full PATH and a populated env, which is exactly what masked them.
#
# 1) cron runs with a minimal PATH and the crontab sets none, so `bun` was not
#    found and scripts/migrate.sh died with exit 127 -> migration pre-flight gate
#    failed -> deploy blocked, silently, every cycle.
export PATH="/home/frank/.bun/bin:/home/frank/.local/bin:${PATH:-/usr/local/bin:/usr/bin:/bin}"

# 2) server/.env.prod (the only env file carrying a HOST-reachable DATABASE_URL)
#    is absent from the shared checkout, so migrate.sh fell back to server/.env,
#    whose DATABASE_URL uses the docker-only alias `supabase-db` -> getaddrinfo
#    ENOTFOUND from the host. migrate.sh honours SYCODE_MIGRATION_DATABASE_URL
#    ahead of every other source, so derive a host-reachable URL from the existing
#    env rather than hardcoding a password or inventing a secrets file. The DB is
#    published on 127.0.0.1:5432 (docker port sycodetrading-supabase-db).
if [[ -z "${SYCODE_MIGRATION_DATABASE_URL:-}" && -f /home/frank/sycode-trading/server/.env ]]; then
  _dburl="$(grep -m1 '^DATABASE_URL=' /home/frank/sycode-trading/server/.env | cut -d= -f2-)"
  if [[ "$_dburl" == *@supabase-db:5432/* ]]; then
    export SYCODE_MIGRATION_DATABASE_URL="${_dburl/@supabase-db:5432/@127.0.0.1:5432}"
  fi
  unset _dburl
fi
# ------------------------------------------------------------------------------

PATH="${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"
export PATH
SHARED=/home/frank/sycode-trading
TREE=/home/frank/.hermes/deploy-state/build-tree
BRANCH=sycode-deploy-build
LOCKWRAP=/home/frank/.hermes/scripts/sycode-deploy-locked.sh
HOLDER=claude-fable-dgx

# Deterministic preflight: cron PATH does not inherit interactive shell rc files,
# so bun at ~/.local/bin/bun is invisible unless we export PATH explicitly.
# This check is REQUIRED for the migrate.sh preflight path (drizzle-kit).
bun_path="${BUN_BIN:-$(command -v bun || true)}"
if [[ -z "${bun_path}" ]]; then
  echo "[pristine] FAIL_CLOSED: bun not found on PATH (PATH=${PATH})" >&2
  # surface as a deploy-state receipt, not a silent exit
  mkdir -p /home/frank/.hermes/deploy-state
  printf '{"timestamp":"%s","bun_path":"%s","ok":false,"error":"bun not found on PATH"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${bun_path}" \
    > /home/frank/.hermes/deploy-state/pristine-bun-preflight.json
  exit 5
fi

# Self-test/regression switch for the bun-path proof: verify we can resolve bun,
# then exit 0 without invoking the deploy lane.
if [[ "${1:-}" == "--self-test-bun-path" ]]; then
  echo "[pristine] bun_path=${bun_path}"
  printf '{"timestamp":"%s","bun_path":"%s","ok":true}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${bun_path}" \
    > /home/frank/.hermes/deploy-state/pristine-bun-preflight.json
  exit 0
fi

# gitignored runtime files `docker compose up` + the host-side migrate.sh preflight need but a
# fresh git worktree lacks. NOTE: server/.env.prod is REQUIRED — migrate.sh (running on the HOST)
# sources server/.env.prod IN PREFERENCE to server/.env, and only .env.prod carries a host-reachable
# DATABASE_URL (localhost:5432). server/.env's DATABASE_URL last-wins line is the docker-internal
# host `supabase-db`, which does NOT resolve from the host → the advisory-lock psql fails and
# migrate.sh falsely reports "another migration in progress". Staging .env.prod fixes the lock check.
# [opus48 seat 2026-07-08: root-caused during first supervised real deploy of #413]
ENV_FILES=(".env" "server/.env" "server/.env.prod")

PASS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --holder) HOLDER="${2:-}"; shift 2 ;;
    --) shift; PASS=("$@"); break ;;
    *) PASS+=("$1"); shift ;;
  esac
done

# 1) ensure the dedicated deploy worktree exists (shares the shared repo's .git objects).
# Robust existence check via `rev-parse --is-inside-work-tree` — NOT `grep -qx` on the
# porcelain list: on the DGX `grep`=ugrep mishandles `-qx` and returns a false negative,
# which retriggers `worktree add -B` and dies with "'<branch>' is already used by worktree".
# [opus48 seat 2026-07-08: this false-negative blocked the #415 deploy via the wrapper]
git -C "$SHARED" fetch origin main
if ! git -C "$TREE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$SHARED" worktree add -B "$BRANCH" "$TREE" origin/main
fi

# 2) make it PRISTINE at origin/main — workers never touch this tree.
# Only `checkout -B` when the branch is NOT already this worktree's HEAD: re-checking-out a branch
# that is already checked out here throws `fatal: '<branch>' is already used by worktree` on some git
# versions and aborts the whole deploy. Once we are on $BRANCH, `reset --hard` does the real work.
# [opus48 seat 2026-07-08: this checkout collision blocked the first real pristine deploy]
git -C "$TREE" fetch origin main
if [[ "$(git -C "$TREE" rev-parse --abbrev-ref HEAD 2>/dev/null)" != "$BRANCH" ]]; then
  git -C "$TREE" checkout -qB "$BRANCH" origin/main
fi
git -C "$TREE" reset --hard origin/main
git -C "$TREE" clean -fd            # remove stray untracked; KEEP ignored (node_modules etc.)
echo "[pristine] $TREE @ $(git -C "$TREE" rev-parse --short HEAD) branch=$(git -C "$TREE" rev-parse --abbrev-ref HEAD)"

# 2b) provision server/node_modules so the host-side migrate.sh preflight can find drizzle-kit
# (`bun run drizzle-kit push` runs from $TREE/server). A fresh worktree has none and `bun install`
# is OSV-blocked here; symlink the shared checkout's server/node_modules (gitignored →
# survives `clean -fd`). [opus48 seat 2026-07-08]
if [[ ! -e "$TREE/server/node_modules" && -d "$SHARED/server/node_modules" ]]; then
  ln -s "$SHARED/server/node_modules" "$TREE/server/node_modules"
  echo "[pristine] linked server/node_modules -> $SHARED/server/node_modules"
fi

# 2c) SYNC bind-mount config files to deploy-owned location (GAP1 structural fix, t_70eb2622).
# The deploy-owned config directory is branch-stable and persists across git resets.
# After every pristine checkout, refresh the 4 docker bind-mount config files so the
# deploy-owned copy is always current with origin/main.
CONFIG_DIR=/home/frank/.hermes/deploy-state/sycode-config
# Prefer installed path (/usr/local/sbin), fall back to workspace seed script
SEED_SCRIPT="/usr/local/sbin/seed-sycode-config.sh"
[[ -x "$SEED_SCRIPT" ]] || SEED_SCRIPT="/home/frank/.hermes/kanban/boards/sycode-trading/workspaces/t_ef7ed63e/deploy/seed-sycode-config.sh"
[[ -x "$SEED_SCRIPT" ]] || { echo "[pristine] WARN: seed-sycode-config.sh not found anywhere"; }
SYCODE_REPO="$TREE" SYCODE_CONFIG_DIR="$CONFIG_DIR" bash "$SEED_SCRIPT" && \
  echo "[pristine] synced bind-mount configs to $CONFIG_DIR" || \
  echo "[pristine] WARN: bind-mount config sync had errors"

# 3) stage the gitignored runtime env files compose needs (from the shared checkout)
for f in "${ENV_FILES[@]}"; do
  if [[ -f "$SHARED/$f" ]]; then
    install -D -m 600 "$SHARED/$f" "$TREE/$f"
    echo "[pristine] staged $f"
  else
    echo "[pristine] WARN: $SHARED/$f absent — compose may fail if required" >&2
  fi
done

# 4) run the gated deploy from the pristine tree, UNDER the DEPLOY.lock.
# CRITICAL: pin COMPOSE_PROJECT_NAME=sycode-trading. `docker compose up` (in deploy_container())
# derives the project name from the cwd basename — which here is "build-tree", NOT the live
# stack's project "sycode-trading". Without this pin, compose would try to create a SECOND
# server under project "build-tree", collide with the running container_name/host-port (409),
# and FAIL the swap AFTER build_image + migrate.sh already ran against the prod DB (no rollback
# fires on a deploy_container failure). Pinning the project makes the recreate target the real
# stack + its existing named volumes; the relative binds (./server/models) resolve under $TREE,
# which is git-clean origin/main (all model files are tracked). [caught in PR #408 adversarial review]
exec "$LOCKWRAP" --holder "$HOLDER" --intent "pristine-worktree deploy of origin/main" -- \
  env SYCODE_DEPLOY_REPO_DIR="$TREE" SYCODE_DEPLOY_BRANCH="$BRANCH" COMPOSE_PROJECT_NAME=sycode-trading \
  python3 "$TREE/execution/deploy_sycodeserver.py" "${PASS[@]}"
