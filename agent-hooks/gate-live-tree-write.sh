#!/usr/bin/env bash
# gate-live-tree-write.sh — pre_tool_call hook.
# BLOCK a kanban worker from mutating the LIVE Hermes source tree
# (/home/frank/.hermes/hermes-agent) via git write commands or direct file
# writes. That tree is what every running gateway imports code from; a commit
# or file write there ships unreviewed code to the whole fleet without a
# restart, and diverges the live checkout from origin/main silently.
#
# INCIDENT (t_8b5495cd): kanban worker t_9722795a (profile builder) ran
# `git commit` three times directly in the live checkout on 2026-09-09 at
# 22:52-22:55 BST; no existing hook classified it (gate-terminal-docker-safety
# only looks at docker/podman; gate-config-writes only looks at config.yaml).
#
# Contract: read JSON payload on stdin; emit a block JSON to veto, or {} to
# allow. FAIL-OPEN on any error/ambiguity — never wedge the fleet. Gate only
# applies to kanban-worker runs (HERMES_KANBAN_TASK set) — never an
# interactive operator/Frank session, which may legitimately inspect or
# (rarely, explicitly) touch the live tree.
# Bypass: ALLOW_LIVE_TREE_WRITE=1 (operator-set only; a worker cannot set env
# mid-reasoning) lets the write through — logged.
set -uo pipefail || true
LOG=/home/frank/.hermes/cron/state/live-tree-write-gate.log
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PY="$HOOK_DIR/gate-live-tree-write.py"
payload=$(cat 2>/dev/null)

# Never gate outside a kanban-worker run.
if [ -z "${HERMES_KANBAN_TASK:-}" ]; then
  echo '{}'; exit 0
fi

if [ "${ALLOW_LIVE_TREE_WRITE:-}" = "1" ]; then
  printf '%s ALLOW(bypass) ALLOW_LIVE_TREE_WRITE=1 task=%s\n' \
    "$(date -u +%FT%TZ)" "${HERMES_KANBAN_TASK:-}" >> "$LOG" 2>/dev/null || true
  echo '{}'; exit 0
fi

# Cheap path: allow instantly unless the payload mentions git or the
# protected tree path at all.
if ! printf '%s' "$payload" | grep -qiE 'git|hermes-agent'; then
  echo '{}'; exit 0
fi

reason=$(printf '%s' "$payload" | python3 "$PY" 2>/dev/null)
if [ -n "${reason:-}" ]; then
  python3 -c 'import json,sys; print(json.dumps({"decision":"block","action":"block","reason":sys.argv[1],"message":sys.argv[1]}))' "$reason"
  exit 0
fi
echo '{}'
exit 0
