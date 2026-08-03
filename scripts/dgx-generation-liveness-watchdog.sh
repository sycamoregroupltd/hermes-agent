#!/usr/bin/env bash
# dgx-generation-liveness-watchdog.sh
# Generation-liveness watchdog for SycodeTrading.
#
# Watches the age of the newest signal_journeys row. A "zombie feed"
# (binance-uk geoblock root) leaves the server UP + health GREEN while
# generation silently stops. This restarts sycodetrading-server ONLY on a
# CONFIRMED, sustained hang, and FAILS CLOSED (never restarts) when the DB
# is unreadable.
#
# DURABILITY: This file lives OUTSIDE the repo working tree so a
# `git checkout <other-branch>` cannot delete it. The original in-tree copy
# (server/scripts/) was untracked and was lost on 2026-07-12 ~20:40Z when the
# host repo was switched to fix/t_cfbbb102-sidecar-intentionally-disabled,
# leaving generation unmonitored for ~4.5h. Reconstructed 2026-07-13 by elon
# to match the proven self-log behavior (900s threshold, uptime + cooldown
# guards, fail-closed blackout handling).
#
# Cron:  */5 * * * * /home/frank/.hermes/scripts/dgx-generation-liveness-watchdog.sh >> <cron.log> 2>&1
# Env :  GEN_WATCHDOG_WEBHOOK_URL (optional) - alert relay for WARN/CRITICAL.

set -uo pipefail

DB_CONTAINER="sycodetrading-supabase-db"
SERVER_CONTAINER="sycodetrading-server"
THRESHOLD_S=900          # 15m with zero new journeys = genuine hang
UPTIME_GUARD_S=1200      # never restart a server still warming up (<20m)
COOLDOWN_S=1200          # never re-restart within 20m of the last restart
BLACKOUT_STALE_TICKS=3   # 3 consecutive unreadable ticks (~15m) => CRITICAL

MON_DIR="/home/frank/sycode-trading/server/.tmp/monitoring"
LOG="$MON_DIR/generation-liveness-watchdog.log"
LAST_RESTART_F="$MON_DIR/.generation-liveness-watchdog.last-restart"
BLACKOUT_F="$MON_DIR/.generation-liveness-watchdog.blackout-count"
WEBHOOK_URL="${GEN_WATCHDOG_WEBHOOK_URL:-}"

mkdir -p "$MON_DIR"
now_iso()   { date -u +%Y-%m-%dT%H:%M:%SZ; }
now_epoch() { date +%s; }
log()       { echo "$(now_iso) $*" >> "$LOG"; }

notify() {
  local level="$1"; shift
  local msg="$*"
  [ -z "$WEBHOOK_URL" ] && return 0
  curl -fsS -m 5 -X POST "$WEBHOOK_URL" \
    -H 'Content-Type: application/json' \
    -d "$(printf '{"level":"%s","source":"gen-liveness-watchdog","message":"%s"}' "$level" "$msg")" \
    >/dev/null 2>&1 || true
}

# --- read newest journey age in seconds; empty/non-numeric => unreadable ---
age="$(docker exec "$DB_CONTAINER" psql -U postgres -d postgres -tAc \
  "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - MAX(triggered_at)))::int, -1) FROM signal_journeys;" \
  2>/dev/null | tr -d '[:space:]')"

if ! [[ "$age" =~ ^-?[0-9]+$ ]] || [ "$age" -lt 0 ]; then
  # FAIL CLOSED: cannot read => NEVER restart. Track sustained blackout.
  bc=$(( $(cat "$BLACKOUT_F" 2>/dev/null || echo 0) + 1 ))
  echo "$bc" > "$BLACKOUT_F"
  if [ "$bc" -ge "$BLACKOUT_STALE_TICKS" ]; then
    log "CRITICAL DB-read blackout ${bc} ticks — monitoring BLIND (fail-closed, NO restart) — NEEDS HUMAN"
    notify critical "gen-liveness watchdog BLIND ${bc} ticks (DB unreadable) — no auto-restart, needs human"
  else
    log "could not read journey-age (got '<empty>') — no action (fail-closed) [blackout ${bc}/${BLACKOUT_STALE_TICKS}]"
  fi
  exit 0
fi

# good read => clear blackout counter
echo 0 > "$BLACKOUT_F"

if [ "$age" -le "$THRESHOLD_S" ]; then
  log "OK journey-age=${age}s (<= ${THRESHOLD_S}s) — generation healthy"
  exit 0
fi

# --- confirmed hang (age > threshold) ---
started="$(docker inspect -f '{{.State.StartedAt}}' "$SERVER_CONTAINER" 2>/dev/null)"
if [ -n "$started" ]; then
  st=$(date -d "$started" +%s 2>/dev/null || echo 0)
  up=$(( $(now_epoch) - st ))
else
  up=0
fi

if [ "$up" -lt "$UPTIME_GUARD_S" ]; then
  log "HANG journey-age=${age}s (>${THRESHOLD_S}s) BUT uptime=${up}s (<${UPTIME_GUARD_S}s) — warming up, NO restart"
  exit 0
fi

last_restart=$(cat "$LAST_RESTART_F" 2>/dev/null || echo 0)
since=$(( $(now_epoch) - last_restart ))
if [ "$since" -lt "$COOLDOWN_S" ]; then
  log "HANG journey-age=${age}s uptime=${up}s BUT last restart ${since}s ago (<${COOLDOWN_S}s) — hang persists after restart, NO re-restart — NEEDS HUMAN"
  notify critical "gen-liveness: hang persists ${age}s, restart ${since}s ago did not recover — needs human"
  exit 0
fi

# --- act ---
# Position-safe restart (t_4e6f516c gate): route through the merged wrapper
# execution/restart_sycodeserver.py (PR #490), which reuses the deploy firewall's
# fetch_open_positions_count as the single source of truth and adds a host-DNS
# preflight. FAIL CLOSED if the wrapper is missing: never bare-restart.
RESTART_WRAPPER="/home/frank/.hermes/deploy-state/build-tree/execution/restart_sycodeserver.py"
if [ ! -f "$RESTART_WRAPPER" ]; then
  log "CRITICAL position-safe wrapper missing ($RESTART_WRAPPER) — FAIL CLOSED, NO restart — NEEDS HUMAN"
  notify critical "gen-liveness: position-safe wrapper missing — no restart — needs human"
  exit 0
fi
log "HANG journey-age=${age}s (>${THRESHOLD_S}s), uptime=${up}s → invoking position-safe restart wrapper"
wrapper_out="$(python3 "$RESTART_WRAPPER" 2>&1)"; rc=$?
echo "$wrapper_out" >> "$LOG"
case "$rc" in
  0)
    now_epoch > "$LAST_RESTART_F"
    log "RESTARTED ${SERVER_CONTAINER} OK via position-safe wrapper (was journey-age=${age}s)"
    notify warn "gen-liveness: zombie-feed hang (${age}s) — restarted ${SERVER_CONTAINER} via position-safe wrapper"
    ;;
  5)
    # Safety gate blocked: open positions > 0 (or count unverifiable), or host DNS failing.
    # No restart; record cooldown so we don't spam retry/alert every 5m. Needs human.
    now_epoch > "$LAST_RESTART_F"
    log "HANG journey-age=${age}s BUT restart BLOCKED by position-safe gate (open_positions>0 or host DNS failing) — NO restart — NEEDS HUMAN"
    notify critical "gen-liveness: restart blocked by position-safe gate (open positions or host DNS) — needs human"
    ;;
  *)
    log "RESTART FAILED via position-safe wrapper (rc=${rc}) — NEEDS HUMAN"
    notify critical "gen-liveness: position-safe restart FAILED (rc=${rc}) — needs human"
    ;;
esac
exit 0

