#!/usr/bin/env bash
# cmux-kanban-bridge.sh
# Ring the Mac (via the cmux SSH relay) when a Hermes task needs a human.
#
# MUST be launched from INSIDE a cmux SSH workspace on the DGX, e.g. (run in a cmux tab):
#   cmux ssh dgx --name fleet-alerter -- bash ~/.hermes/scripts/cmux-kanban-bridge.sh
# Only inside that relay session is the `cmux` shim on PATH, so `cmux notify` reaches your Mac.
#
# Trigger: baseline-then-diff over configured kanban statuses. First run silently records
# everything already in those statuses (no ring storm); afterwards it rings only when a NEW
# task enters one of them. Env knobs:
#   BRIDGE_BOARD (sycode-trading)  BRIDGE_STATUSES ("review blocked")
#   BRIDGE_INTERVAL (45s)          BRIDGE_STATE_DIR (~/.hermes/state/cmux-bridge)
set -uo pipefail

BOARD="${BRIDGE_BOARD:-sycode-trading}"
INTERVAL="${BRIDGE_INTERVAL:-45}"
STATUSES="${BRIDGE_STATUSES:-review blocked}"
STATE_DIR="${BRIDGE_STATE_DIR:-$HOME/.hermes/state/cmux-bridge}"
STATE="$STATE_DIR/seen-${BOARD}.txt"
mkdir -p "$STATE_DIR"

ts(){ date '+%Y-%m-%d %H:%M:%S'; }
have_cmux(){ command -v cmux >/dev/null 2>&1; }

ring(){ # $1=subtitle  $2=body
  echo "[$(ts)] 🔔 ring: $1 :: $2"
  if have_cmux; then
    cmux notify --title "🛰 ${BOARD}" --subtitle "$1" --body "$2" >/dev/null 2>&1 \
      || echo "           (cmux notify failed — is the cmux ssh workspace still open?)"
  else
    echo "           (no 'cmux' on PATH — not inside a cmux relay session; ring skipped)"
  fi
}

snapshot(){ # -> lines of "status:t_id<TAB>oneline"
  local st line id
  for st in $STATUSES; do
    while IFS= read -r line; do
      id="$(printf '%s' "$line" | grep -oE 't_[0-9a-f]+' | head -n1)"
      [ -n "$id" ] || continue
      printf '%s:%s\t%s\n' "$st" "$id" "$(printf '%s' "$line" | tr -s ' \t' ' ' | sed 's/^ //;s/ $//')"
    done < <(hermes kanban --board "$BOARD" ls --status "$st" 2>/dev/null | grep -E 't_[0-9a-f]+')
  done
}

echo "[$(ts)] cmux <-> hermes bridge  |  board=$BOARD  statuses='$STATUSES'  interval=${INTERVAL}s"
if have_cmux; then
  echo "[$(ts)] relay OK: $(command -v cmux)"
else
  echo "[$(ts)] WARNING: no 'cmux' on PATH. Launch via 'cmux ssh ... -- bash $0' or rings won't reach the Mac."
fi
ring "bridge online" "watching ${BOARD} for: ${STATUSES}"

seed=0
[ -f "$STATE" ] || seed=1
while :; do
  snap="$(snapshot)"
  curr="$(printf '%s\n' "$snap" | cut -f1 | grep -E '.' | sort -u)"
  if [ "$seed" = 1 ]; then
    printf '%s\n' "$curr" > "$STATE"
    echo "[$(ts)] baselined $(printf '%s\n' "$curr" | grep -c .) existing item(s); watching for new ones."
    seed=0
  else
    new="$(comm -13 <(sort -u "$STATE" 2>/dev/null) <(printf '%s\n' "$curr"))"
    if [ -n "$new" ]; then
      while IFS= read -r k; do
        [ -n "$k" ] || continue
        st="${k%%:*}"
        body="$(printf '%s\n' "$snap" | awk -F'\t' -v kk="$k" 'index($0,kk"\t")==1{print substr($0, length(kk)+2); exit}')"
        [ -n "$body" ] || body="$k"
        ring "needs you · ${st}" "$body"
      done <<< "$new"
    fi
    printf '%s\n' "$curr" > "$STATE"
  fi
  sleep "$INTERVAL"
done

