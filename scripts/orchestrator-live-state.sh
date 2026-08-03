#!/usr/bin/env bash
# orchestrator-live-state.sh — fail-loud, read-only fleet snapshot.
# Consumer: the Jarvis Orchestrator kernel (§4 step 1) and the cockpit
# SessionStart hook. Every probe that fails must SAY SO (FAILED-PROBE
# line), never silently omit a section — an empty section is "unknown",
# not "healthy". No probe may fabricate a number from an error message:
# capture output and rc separately, THEN count.
set -u
REG=/home/frank/obsidian-fleet-vault/Orchestration/OBJECTIVE-REGISTRY.md
BOARDS_DIR="$HOME/.hermes/kanban/boards"
HB=/home/frank/dgx-fable-orchestrator/state/heartbeat

echo "== ORCHESTRATOR LIVE STATE $(date -u +%FT%TZ) =="

echo "-- boards (status=count per board) --"
if [ -d "$BOARDS_DIR" ]; then
  for db in "$BOARDS_DIR"/*/kanban.db; do
    [ -f "$db" ] || continue
    slug=$(basename "$(dirname "$db")")
    out=$(sqlite3 "file:${db}?mode=ro" \
      "SELECT status || '=' || COUNT(*) FROM tasks GROUP BY status;" 2>&1)
    rc=$?
    if [ $rc -eq 0 ]; then
      echo "$slug: $(echo "$out" | tr '\n' ' ')"
    else
      echo "FAILED-PROBE: board $slug rc=$rc ($out)"
    fi
  done
else
  echo "FAILED-PROBE: boards dir missing at $BOARDS_DIR"
fi

echo "-- blocked block_kind breakdown (NULLs coalesced) --"
for db in "$BOARDS_DIR"/*/kanban.db; do
  [ -f "$db" ] || continue
  slug=$(basename "$(dirname "$db")")
  out=$(sqlite3 "file:${db}?mode=ro" \
    "SELECT '$slug ' || COALESCE(block_kind,'NULL') || '=' || COUNT(*)
     FROM tasks WHERE status='blocked' GROUP BY COALESCE(block_kind,'NULL');" 2>&1)
  rc=$?
  if [ $rc -eq 0 ]; then
    [ -n "$out" ] && echo "$out" || echo "$slug: (no blocked rows)"
  else
    echo "FAILED-PROBE: block_kind $slug rc=$rc (column may differ; probe schema)"
  fi
done

echo "-- hermes cron errors (current profile only; kernel gap 3: scheduler.db can be stale) --"
out=$(timeout 60 hermes cron list 2>&1); rc=$?
if [ $rc -ne 0 ]; then
  echo "FAILED-PROBE: hermes cron list rc=$rc (124=timeout)"
else
  errs=$(printf '%s\n' "$out" | grep -ia 'error\|fail')
  [ -n "$errs" ] && printf '%s\n' "$errs" || echo "(no error/fail lines in cron list)"
fi

echo "-- provider posture --"
out=$(timeout 60 hermes status 2>&1); rc=$?
if [ $rc -ne 0 ]; then
  echo "FAILED-PROBE: hermes status rc=$rc (124=timeout)"
else
  printf '%s\n' "$out" | head -20
fi

echo "-- open PRs (sycode-trading, explicit limit) --"
if [ -d /home/frank/sycode-trading ]; then
  out=$(cd /home/frank/sycode-trading && timeout 60 gh pr list --state open --limit 200 2>&1); rc=$?
  if [ $rc -eq 0 ]; then
    echo "open PRs (capped at 200): $(printf '%s' "$out" | grep -c .)"
  else
    echo "FAILED-PROBE: gh pr list rc=$rc ($(printf '%s' "$out" | head -1))"
  fi
else
  echo "FAILED-PROBE: /home/frank/sycode-trading missing"
fi

echo "-- STATUS freshness vs registry SLA --"
if [ -f "$REG" ]; then
  grep '^status-file: ' "$REG" | while read -r _ path _ sla; do
    if [ -f "$path" ]; then
      age_d=$(( ( $(date +%s) - $(stat -c %Y "$path") ) / 86400 ))
      if [ "$age_d" -gt "${sla:-7}" ]; then
        echo "STALE: $path age=${age_d}d sla=${sla}d"
      else
        echo "ok: $path age=${age_d}d"
      fi
    else
      echo "FAILED-PROBE: status file missing $path"
    fi
  done
else
  echo "FAILED-PROBE: registry missing at $REG"
fi

echo "-- orchestrator heartbeat (only source=wrapper proves the loop) --"
if [ -f "$HB" ]; then
  echo "heartbeat age: $(( $(date +%s) - $(stat -c %Y "$HB") ))s content: $(head -c 200 "$HB")"
else
  echo "FAILED-PROBE: heartbeat file missing at $HB (first run, or loop dead)"
fi

echo "== END LIVE STATE (every number above is a point-in-time probe; re-probe at point of use) =="
