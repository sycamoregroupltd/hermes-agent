#!/usr/bin/env bash
# kanban-audit-chain-monitor — silent-green watchdog for the kanban event-log hash chain.
# Contract (adversarial review 2026-08-01, F2/F3/F5/F6 — do NOT regress these):
#   * exit 0 on EVERY handled path (green, tamper, lag, throttled repeat, continuity alarm).
#     hermes --no-agent crons deliver a failure summary on ANY nonzero exit EVERY tick,
#     which defeats the throttle and spams #critical-alerts. Signal via stdout + spool only.
#   * TAMPER class (breaks>0, forged_below_tip>0, unparseable verify line, continuity anomaly)
#     BYPASSES the throttle — alerts every run. Only pure lag-breach is throttled.
#   * (genesis_at, max_seq) persisted and compared each run; genesis change or seq drop
#     alarms (throttle-bypassed), single-shot (re-persisted after compare).
#   * KANBAN_CHAIN_* env overrides are IGNORED unless KANBAN_CHAIN_DRILL=1; drill spool/state
#     default to scratch so a forgotten override can never touch production.
# GC prunes: legitimate `hermes kanban gc` deletions surface as tamper-class breaks until a
# HUMAN runs `kanban-audit-chain.py reconcile-gc [--backup <pre-gc snapshot>] --apply`
# (t_78c65b78 design — the human gate is deliberate; do not wire reconcile into this monitor).
set -u

PROD_DB=/home/frank/.hermes/kanban/boards/jarvis-os/kanban.db
PROD_CHAIN=/home/frank/.hermes/audit/kanban-chain.db
PROD_SPOOL=/home/frank/.hermes/profiles/jarvis/state/alertmanager-spool/incoming
PROD_STATE=/home/frank/.hermes/audit/.kanban-chain-monitor.last-alert
PROD_GENSEQ=/home/frank/.hermes/audit/.kanban-chain-monitor.genesis-seq
CHAIN_SCRIPT=/home/frank/.hermes/scripts/kanban-audit-chain.py
MAX_LAG_MIN=30
REALERT_SECONDS=7200

if [ "${KANBAN_CHAIN_DRILL:-0}" = "1" ]; then
  DB=${KANBAN_CHAIN_DB:-$PROD_DB}
  CHAIN=${KANBAN_CHAIN_SIDECAR:-$PROD_CHAIN}
  SPOOL=${KANBAN_CHAIN_SPOOL_DIR:-/tmp/kanban-chain-drill/spool}
  STATE=${KANBAN_CHAIN_STATE:-/tmp/kanban-chain-drill/state}
  GENSEQ=${KANBAN_CHAIN_GEN_STATE:-/tmp/kanban-chain-drill/genseq}
  MAX_LAG_MIN=${KANBAN_CHAIN_MAX_LAG_MIN:-$MAX_LAG_MIN}
  REALERT_SECONDS=${KANBAN_CHAIN_REALERT_SECONDS:-$REALERT_SECONDS}
  mkdir -p "$SPOOL" "$(dirname "$STATE")" "$(dirname "$GENSEQ")"
else
  for v in KANBAN_CHAIN_DB KANBAN_CHAIN_SIDECAR KANBAN_CHAIN_SPOOL_DIR KANBAN_CHAIN_STATE \
           KANBAN_CHAIN_GEN_STATE KANBAN_CHAIN_MAX_LAG_MIN KANBAN_CHAIN_REALERT_SECONDS; do
    [ -n "${!v:-}" ] && echo "NOTE: $v ignored (set KANBAN_CHAIN_DRILL=1 for drills)" >&2
  done
  DB=$PROD_DB; CHAIN=$PROD_CHAIN; SPOOL=$PROD_SPOOL; STATE=$PROD_STATE; GENSEQ=$PROD_GENSEQ
fi

write_alert() { # severity, summary -> 0 on success; chmod 644 on EVERY write; never arms throttle itself
  local sev="$1" summary="$2" f
  f="$SPOOL/kanban-audit-chain-$(date +%s%3N).json"
  printf '{"status":"firing","alerts":[{"labels":{"alertname":"KanbanAuditChain","severity":"%s"},"annotations":{"summary":%s},"startsAt":"%s"}]}\n' \
    "$sev" "$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$summary")" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$f" 2>/dev/null || return 1
  chmod 644 "$f" 2>/dev/null || return 1
  return 0
}

throttled() { # 0 = throttled (skip alert), 1 = allowed; clamps future stamps (clock skew)
  local last now
  last=$(cat "$STATE" 2>/dev/null || echo 0); now=$(date +%s)
  case "$last" in (''|*[!0-9]*) last=0;; esac
  if [ "$last" -gt "$now" ]; then echo "NOTE: clamped future throttle stamp $last -> $now"; last=$now; echo "$now" > "$STATE"; fi
  [ $((now - last)) -lt "$REALERT_SECONDS" ]
}

alert() { # severity, summary, bypass(1=ignore throttle); stamp ONLY after successful spool write
  local sev="$1" summary="$2" bypass="$3"
  if [ "$bypass" != "1" ] && throttled; then return 0; fi
  echo "$summary"
  if write_alert "$sev" "$summary"; then date +%s > "$STATE"
  else echo "ERROR: alert spool write failed — throttle NOT armed, will retry next run"; fi
}

if [ "${1:-}" = "--test" ]; then # manual-only path; nonzero exit acceptable here (never runs under cron)
  write_alert "info" "TEST alert from kanban-audit-chain-monitor ($(date -u +%FT%TZ))" \
    && { echo "TEST alert written OK"; exit 0; } || { echo "TEST alert write FAILED"; exit 1; }
fi

append_out=$(python3 "$CHAIN_SCRIPT" append --db "$DB" --chain "$CHAIN" 2>&1)
verify_out=$(python3 "$CHAIN_SCRIPT" verify --db "$DB" --chain "$CHAIN" --max-lag-minutes "$MAX_LAG_MIN" 2>&1)
verify_rc=$?
line=$(printf '%s\n' "$verify_out" | grep -a '^CHAIN-VERIFY:' | tail -1)

if [ -z "$line" ]; then
  alert "critical" "KANBAN-AUDIT-CHAIN: monitor could not parse verify output (treat as tamper). append=[$append_out] verify=[$verify_out]" 1
  exit 0
fi
breaks=$(printf '%s' "$line" | sed -n 's/.*status=[A-Z]* breaks=\([0-9]*\).*/\1/p')
forged=$(printf '%s' "$line" | sed -n 's/.*forged_below_tip=\([0-9]*\).*/\1/p')
genesis=$(printf '%s' "$line" | sed -n 's/.*genesis_at=\([0-9]*\).*/\1/p')
maxseq=$(printf '%s' "$line" | sed -n 's/.*max_seq=\([0-9]*\).*/\1/p')

prev=$(cat "$GENSEQ" 2>/dev/null || echo "")
if [ -n "$prev" ] && [ -n "$genesis" ] && [ -n "$maxseq" ]; then
  pg=${prev% *}; ps=${prev#* }
  if [ "$pg" != "$genesis" ]; then
    alert "critical" "KANBAN-AUDIT-CHAIN: CRITICAL — chain continuity anomaly: genesis_at changed $pg -> $genesis (possible rebuild). $line" 1
  elif [ "${ps:-0}" -gt "${maxseq:-0}" ] 2>/dev/null; then
    alert "critical" "KANBAN-AUDIT-CHAIN: CRITICAL — chain max_seq dropped $ps -> $maxseq (possible truncation). $line" 1
  fi
fi
[ -n "$genesis" ] && [ -n "$maxseq" ] && echo "$genesis $maxseq" > "$GENSEQ"

if [ "${breaks:-0}" -gt 0 ] || [ "${forged:-0}" -gt 0 ]; then
  alert "critical" "KANBAN-AUDIT-CHAIN: FAIL — tamper-class break(s); if a hermes kanban gc ran, a human must reconcile-gc (see header). $line (db=$DB chain=$CHAIN)" 1
elif [ "$verify_rc" -ne 0 ]; then
  alert "warning" "KANBAN-AUDIT-CHAIN: lag/health breach. $line (db=$DB chain=$CHAIN)" 0
fi
exit 0
