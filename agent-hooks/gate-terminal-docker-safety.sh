#!/usr/bin/env bash
# gate-terminal-docker-safety.sh — pre_llm_call / pre_tool_call hook (matcher: terminal).
# Blocks high-risk docker invocations that dump credentials or mutate prod-ish
# data stores. Fail-open on parse errors (never wedge the fleet).
#
# BLOCK:
#   - docker exec into supabase/postgres containers (password-free admin)
#   - docker exec ... printenv / env / cat of .env / /proc/*/environ
#   - docker inspect --format '{{.Config.Env}}' (secret dump)
#   - docker compose/stack down|rm -f of sycodetrading-* / production names
#   - docker run --privileged
# ALLOW: docker ps/logs/stats, paper-server recreate scripts, read-only inspect
#        without Env format, docker exec of non-db containers with no env dump.
# Bypass: ALLOW_DOCKER_UNSAFE=1 (orchestrator-set only).
set -uo pipefail || true
LOG=/home/frank/.hermes/cron/state/docker-safety-gate.log
payload=$(cat 2>/dev/null || true)

ok() { echo '{}'; exit 0; }
block() {
  python3 -c 'import json,sys; print(json.dumps({"decision":"block","reason":sys.argv[1]}))' "$1"
  printf '%s BLOCK %s\n' "$(date -u +%FT%TZ)" "$1" >> "$LOG" 2>/dev/null || true
  exit 0
}

if [ "${ALLOW_DOCKER_UNSAFE:-}" = "1" ]; then
  printf '%s ALLOW(bypass) ALLOW_DOCKER_UNSAFE=1\n' "$(date -u +%FT%TZ)" >> "$LOG" 2>/dev/null || true
  ok
fi

# Cheap path: no docker/podman/nerdctl token → allow.
if ! printf '%s' "$payload" | grep -qiE '(^|[^A-Za-z0-9_])(docker|podman|nerdctl)([^A-Za-z0-9_]|$)'; then
  ok
fi

reason=$(printf '%s' "$payload" | python3 /home/frank/.hermes/agent-hooks/gate-terminal-docker-safety.py 2>/dev/null || true)

if [ -n "${reason:-}" ]; then
  block "$reason"
fi
ok
