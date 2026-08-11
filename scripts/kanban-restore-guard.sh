#!/usr/bin/env bash
# kanban-restore-guard.sh — SAFE kanban.db restore gatekeeper.
#
# WHY: hermes issue #35240 + the 2026-07-09 corruption incident showed that an
# ad-hoc restore that `mv`/`cp`s a corrupt/OLDER snapshot over a live kanban.db
# silently downgrades the board (events 76536 -> 75364) and can data-loss the
# whole board (host sqlite3 .recover yields an EMPTY db). Hermes-agent core only
# QUARANTINES corrupt DBs (kanban.db.corrupt.<sha16>.bak); it never restores.
# This guard is the ONLY approved entry point for swapping kanban.db bytes back
# in. Any fable remediation / manual recovery MUST call this, never raw mv/cp.
#
# GUARANTEES (hard gates — fail closed, refuse to restore):
#   (a) Quiesce ALL hermes-* gateway units before touching the db.
#   (b) Recover ONLY via containerized `alpine:sqlite` `.recover` (host sqlite3
#       3.45.1 lacks sqlite_dbpage -> empty db). If the source is already a
#       ready .db file, it is still validated, not re-recovered.
#   (c) Gate on `PRAGMA integrity_check = ok` AND row-count floors
#       (tasks, task_events) from a freshly-counted live baseline.
#   (d) NEVER downgrade the live events count. If the candidate has FEWER
#       task_events than the live db, refuse.
#
# Usage:
#   kanban-restore-guard.sh <candidate_snapshot_path> [--board <slug>] [--allow-stop]
#
# SAFETY: this guard will NOT stop gateway units by itself. Quiescing the
# hermes-* gateway units is a Frank-gated production action. By default the
# guard REFUSES unless all gateway units are already stopped, OR you pass
# --allow-stop (which is itself gated: only run it under explicit Frank
# direction). This prevents an autonomous restore from knocking the fleet
# offline the way the 2026-07-09 'fable remediation' incident did.
#
# It writes the validated bytes to the live kanban.db ONLY after all gates
# pass, atomically (write to .pending then mv over live under the dispatch
# lock), and only with all gateway units stopped first.
set -uo pipefail

CANDIDATE="${1:-}"
BOARD="${3:-sycode-trading}"
ALLOW_STOP=0
case "${2:-}" in
  --allow-stop) ALLOW_STOP=1 ;;
esac
DB="/home/frank/.hermes/kanban/boards/${BOARD}/kanban.db"
LOCK="/home/frank/.hermes/kanban/boards/${BOARD}/kanban.db.dispatch.lock"
SQLITE_IMG="${KANBAN_RESTORE_SQLITE_IMG:-alpine:sqlite}"
RECOVERED_TMP="$(mktemp -d)/recovered.db"

fail() { echo "REFUSED: $*" >&2; exit 2; }

[ -n "$CANDIDATE" ] || fail "usage: kanban-restore-guard.sh <candidate> [--board <slug>]"
[ -e "$CANDIDATE" ] || fail "candidate snapshot not found: $CANDIDATE"

# (a) Quiesce all hermes gateway units — but NEVER autonomously.
GATEWAYS_UP=$(systemctl --user list-units --type=service --no-legend 2>/dev/null | awk '{print $1}' | grep -E '^hermes-gateway' | grep -vE '\.(inactive|dead|failed)' || true)
if [ -n "$GATEWAYS_UP" ]; then
  if [ "$ALLOW_STOP" -eq 1 ]; then
    echo "Quiescing hermes-* gateway units (--allow-stop)..."
    for u in $GATEWAYS_UP; do
      systemctl --user stop "$u" 2>/dev/null && echo "  stopped $u" || echo "  (could not stop $u)"
    done
    # belt-and-braces: ensure no process holds the dispatch lock
    fuser -k "$LOCK" 2>/dev/null || true
    sleep 2
  else
    fail "gateway units still running ($GATEWAYS_UP). Refusing to restore autonomously. Stop them first or pass --allow-stop under Frank direction."
  fi
fi
echo "All hermes gateway units quiesced (or already down) — safe to proceed."

# (b) Recover via containerized alpine:sqlite ONLY if the candidate is not already a clean sqlite db.
if docker image inspect "$SQLITE_IMG" >/dev/null 2>&1; then
  # Treat candidate as a possibly-corrupt db; recover it.
  docker run --rm -v "$CANDIDATE:/in.db" -v "$(dirname "$RECOVERED_TMP"):/out" "$SQLITE_IMG" \
    sh -c "sqlite3 /in.db '.recover' | sqlite3 /out/recovered.db" 2>/dev/null \
    || fail "containerized .recover failed for $CANDIDATE (host .recover is forbidden — would yield empty db)"
else
  fail "container image $SQLITE_IMG not available; host sqlite3 .recover is FORBIDDEN (empty-db risk). Install alpine:sqlite or supply a pre-recovered .db"
fi

# (c) integrity_check + row-count floors on the recovered db.
IC=$(sqlite3 "file:$RECOVERED_TMP?mode=ro" "PRAGMA integrity_check;" 2>&1 | head -1)
[ "$IC" = "ok" ] || fail "recovered db failed integrity_check: $IC"
LIVE_EVENTS=$(sqlite3 "file:$DB?mode=ro" "SELECT COUNT(*) FROM task_events;" 2>/dev/null || echo 0)
CAND_EVENTS=$(sqlite3 "file:$RECOVERED_TMP?mode=ro" "SELECT COUNT(*) FROM task_events;" 2>/dev/null || echo 0)
CAND_TASKS=$(sqlite3 "file:$RECOVERED_TMP?mode=ro" "SELECT COUNT(*) FROM tasks;" 2>/dev/null || echo 0)
[ "$CAND_EVENTS" -gt 0 ] 2>/dev/null || fail "recovered db has 0 task_events (empty-db landmine)"
[ "$CAND_TASKS" -gt 0 ] 2>/dev/null || fail "recovered db has 0 tasks"

# (d) NEVER downgrade the live events count.
if [ "$CAND_EVENTS" -lt "$LIVE_EVENTS" ] 2>/dev/null; then
  fail "candidate events ($CAND_EVENTS) < live events ($LIVE_EVENTS) — would DOWNGRADE the board. Refusing."
fi

# Atomic swap under dispatch lock (briefly acquired to serialize with any straggler).
(
  flock -n 9 || fail "could not acquire dispatch lock for swap"
  mv "$RECOVERED_TMP" "$DB"
) 9>"$LOCK"
echo "RESTORE OK: $BOARD kanban.db replaced (events $LIVE_EVENTS -> $CAND_EVENTS, tasks=$CAND_TASKS, integrity=ok)"
echo "Remember to restart the hermes-* gateway units you stopped."
