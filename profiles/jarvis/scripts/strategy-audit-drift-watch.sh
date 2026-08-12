#!/usr/bin/env bash
# ============================================================================
# strategy-audit-drift-watch.sh — no_agent cron LOOP gate (fleet convention)
# ============================================================================
# Detects NEW strategy audit drift: a strategy whose `strategies.updated_at`
# advanced past its latest `strategy_audit_log.created_at` (by > tolerance)
# WITHOUT a matching audit row — the silent-write class behind the 2026-07-29
# 20:40Z signal_filter bypass (t_e94bbe14 / t_b6ae8624).
#
# Design (per independent reviewer round-2 findings):
#   F1 liveness  — real scheduled job (cron every 30m, no_agent) with a named
#                  alert path (Alertmanager type=trading -> #critical-alerts)
#                  and a named consumer (nervous-system-engineer wake loop).
#   F2 usability  — dynamic baseline: first run snapshots current updated_at
#                  per strategy (GREEN); subsequent runs ALERT only when a
#                  strategy's updated_at moves past its baseline without an
#                  audit row. Known 45 auditless + 22 stale rows do NOT mute it.
#   F3 separation — stale-audit and missing-audit are distinct buckets in the
#                  report; alert decision is per-row and reported separately.
#
# Conventions (mirrors persistence-health-watch.sh / elon-stall-watch.sh):
#   - final stdout line is JSON {"wakeAgent": true|false, ...}
#   - state-dedup via signature file (same drift does not re-wake every tick)
#   - fail-open: DB error / psql failure -> {"wakeAgent":false}, exit 0
#   - read-only SQL; NEVER writes to the audit database
#
# Modes:
#   (no args)            cron gate mode: reconcile + alert on NEW drift
#   --manual             print full current drift status report, exit 0
#   --threshold-seconds  override tolerance (default 60s)
#   --reset-baseline     drop the baseline snapshot (next run re-snapshots)
# ============================================================================
set -uo pipefail

STATE_DIR="/home/frank/.hermes/cron/state"
mkdir -p "$STATE_DIR"
BASELINE_FILE="${STATE_DIR}/strategy-audit-drift-baseline.json"
SIG_FILE="${STATE_DIR}/strategy-audit-drift.sig"

THRESHOLD_SECONDS="${STRATEGY_AUDIT_DRIFT_THRESHOLD_SECONDS:-60}"

ALERTMANAGER_URL="${ALERTMANAGER_URL:-http://localhost:9093}"
# Alertmanager route `type: trading` -> trading-receiver -> OOB relay -> #critical-alerts
ALERTMANAGER_ROUTE_LABEL="trading"

MANUAL=0
RESET_BASELINE=0

for arg in "$@"; do
  case "$arg" in
    --manual) MANUAL=1 ;;
    --reset-baseline) RESET_BASELINE=1 ;;
    --threshold-seconds=*) THRESHOLD_SECONDS="${arg#--threshold-seconds=}" ;;
    *) echo "unknown arg: $arg" >&2 ;;
  esac
done

PSQL=(docker exec -e PGPASSWORD=postgres sycodetrading-supabase-db psql -h localhost -U postgres -d postgres -v ON_ERROR_STOP=1 -t -A -P pager=off)

# ── Fail-open wrapper ──────────────────────────────────────────────────────
emit_false() {
  echo "$1" >&2
  echo '{"wakeAgent":false}'
  exit 0
}

# ── 1. Read-only drift query ───────────────────────────────────────────────
# One strategy per line: id|name|updated_at|latest_audit_created_at ('' = none)
ROWS="$("${PSQL[@]}" -c "
  SELECT s.id || '|' || COALESCE(s.name,'') || '|' || s.updated_at::text || '|' || COALESCE(MAX(a.created_at)::text, '')
  FROM strategies s
  LEFT JOIN strategy_audit_log a ON a.strategy_id = s.id
  GROUP BY s.id, s.name, s.updated_at
  ORDER BY s.updated_at DESC;
" 2>/dev/null)" || emit_false "strategy-audit-drift: DB query failed (fail-open)"

[ -z "$ROWS" ] && emit_false "strategy-audit-drift: no strategy rows returned (fail-open)"

# ── 2. Classify + baseline in python (robust date math) ────────────────────
PY_OUTPUT="$(THRESHOLD_SECONDS="$THRESHOLD_SECONDS" BASELINE_FILE="$BASELINE_FILE" RESET_BASELINE="$RESET_BASELINE" python3 - "$ROWS" <<'PYEOF'
import json, os, sys

threshold = int(os.environ.get("THRESHOLD_SECONDS", "60"))
baseline_path = os.environ.get("BASELINE_FILE", "")
reset = os.environ.get("RESET_BASELINE", "0") == "1"
baseline_path = baseline_path or "/tmp/strategy-audit-drift-baseline.json"

# rows come as argv[1] (avoids stdin redirection conflicts in cron)
rows_text = sys.argv[1] if len(sys.argv) > 1 else ""

baseline = {}
bootstrap = False
if not reset and os.path.exists(baseline_path):
    try:
        with open(baseline_path, "r") as f:
            baseline = json.load(f).get("observed", {})
    except Exception:
        baseline = {}
else:
    bootstrap = True  # first run / reset: snapshot current state, do not alert

def parse_ts(v):
    # accept postgres timestamptz text
    import datetime
    s = str(v).strip()
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None

rows = []
for line in rows_text.splitlines():
    parts = line.split("|")
    if len(parts) < 4:
        continue
    sid, name, updated, latest_audit = parts[0], parts[1], parts[2], parts[3]
    up_ts = parse_ts(updated)
    au_ts = parse_ts(latest_audit) if latest_audit else None
    if up_ts is None:
        bucket = "missing-audit"
        drift = None
    elif au_ts is None:
        bucket = "missing-audit"
        drift = None
    else:
        drift = int(round(up_ts - au_ts))
        bucket = "stale-audit" if drift > threshold else "ok"
    rows.append({
        "id": sid, "name": name, "updated_at": updated,
        "latest_audit": latest_audit, "drift_seconds": drift,
        "bucket": bucket,
    })

new_drift = []
for r in rows:
    prev = baseline.get(r["id"])
    # first-run bootstrap: snapshot everything, alert nothing
    if bootstrap:
        baseline[r["id"]] = r["updated_at"]
        continue
    # new strategy appeared with drift -> alert (it was not known/grandfathered)
    if prev is None:
        if r["bucket"] != "ok":
            new_drift.append(r)
        baseline[r["id"]] = r["updated_at"]
        continue
    # known strategy: only NEW mutation (updated_at advanced) without audit
    if r["updated_at"] != prev and r["bucket"] != "ok":
        new_drift.append(r)
    baseline[r["id"]] = r["updated_at"]

# persist baseline (atomic-ish)
tmp = baseline_path + ".tmp"
with open(tmp, "w") as f:
    json.dump({"observed": baseline, "updated_at": r["updated_at"] if rows else "", "threshold_seconds": threshold}, f, indent=2)
os.replace(tmp, baseline_path)

counts = {"total": len(rows), "ok": 0, "stale-audit": 0, "missing-audit": 0}
for r in rows:
    counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1

out = {
    "bootstrap": bootstrap,
    "new_drift": len(new_drift),
    "counts": counts,
    "rows": rows,
    "new_drift_rows": new_drift,
    "threshold_seconds": threshold,
}
sys.stdout.write(json.dumps(out))
PYEOF
)" || emit_false "strategy-audit-drift: python classification failed (fail-open)"

# ── 3. Manual mode: full current drift status report ───────────────────────
if [ "$MANUAL" = "1" ]; then
  python3 - "$PY_OUTPUT" <<'PYEOF'
import json, sys, os
d = json.loads(sys.argv[1])
print("=" * 78)
print("  STRATEGY AUDIT-DRIFT RECONCILE  (read-only, manual)")
print("=" * 78)
for r in d["rows"]:
    drift = "null" if r["drift_seconds"] is None else str(r["drift_seconds"])
    print(f'{r["id"]}\t{r["name"]}\t{r["updated_at"]}\t{r["latest_audit"]}\t{drift}\t{r["bucket"]}')
print("-" * 78)
c = d["counts"]
print(f"  total={c['total']} ok={c['ok']} stale-audit={c['stale-audit']} missing-audit={c['missing-audit']} threshold={d.get('threshold_seconds', 'n/a')}s")
print(f"  baseline: {'snapshotted this run (GREEN)' if d['bootstrap'] else 'loaded from ' + os.environ.get('BASELINE_FILE','')}")
print("=" * 78)
PYEOF
  echo '{"wakeAgent":false,"mode":"manual","new_drift":'"$(echo "$PY_OUTPUT" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["new_drift"])' 2>/dev/null || echo 0)"'}}'
  exit 0
fi

# ── 4. Cron gate: decide wake ──────────────────────────────────────────────
NEW_DRIFT="$(echo "$PY_OUTPUT" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["new_drift"])' 2>/dev/null || echo 0)"
NEW_DRIFT="${NEW_DRIFT:-0}"

if [ "$NEW_DRIFT" = "0" ]; then
  : > "$SIG_FILE"
  echo "strategy-audit-drift: clean (no new drift)"
  echo '{"wakeAgent":false}'
  exit 0
fi

# Build signature from new-drift rows: ids + updated_at (stable identity)
SIG_SOURCE="$(echo "$PY_OUTPUT" | python3 -c '
import json,sys
d = json.loads(sys.stdin.read())
print("|".join("%s@%s" % (r["id"], r["updated_at"]) for r in d["new_drift_rows"]))
' 2>/dev/null)"
SIG="$(printf '%s' "$SIG_SOURCE" | md5sum | cut -c1-12)"

# State-dedup: same drift signature already alerted -> do not re-wake
if [ -f "$SIG_FILE" ] && grep -qxF "$SIG" "$SIG_FILE"; then
  echo "strategy-audit-drift: persists (already woke): $SIG"
  echo '{"wakeAgent":false}'
  exit 0
fi
echo "$SIG" >> "$SIG_FILE"

# ── 5. Emit alert through fleet conventions ────────────────────────────────
echo "STRATEGY-AUDIT-DRIFT — NEW silent strategy mutation(s) detected:"
echo "$PY_OUTPUT" | python3 -c '
import json,sys
d = json.loads(sys.stdin.read())
for r in d["new_drift_rows"]:
    gap = "NO AUDIT ROW" if r["drift_seconds"] is None else str(r["drift_seconds"]) + "s"
    print("  [%s] %s  %s  %s" % (r["bucket"].upper(), r["name"], r["id"], gap))
'

# Alertmanager POST (type=trading route -> OOB relay -> #critical-alerts)
# Keep the description JSON-safe: single line, no raw control characters.
DESC="$(echo "$PY_OUTPUT" | python3 -c '
import json,sys
d = json.loads(sys.stdin.read())
rows = d["new_drift_rows"]
lines = []
for r in rows[:20]:
    lines.append("%s (%s) %s drift=%s" % (r["name"], r["id"], r["bucket"], r["drift_seconds"]))
print("; ".join(lines))
' 2>/dev/null | tr -d "\r\n")"
ALERT_PAYLOAD="$(printf '[{"labels":{"alertname":"StrategyAuditDrift","severity":"warning","type":"%s","instance":"strategy-audit-drift-monitor"},"annotations":{"summary":"%s strategy(s) mutated without audit row","description":"%s"}}]' \
  "$ALERTMANAGER_ROUTE_LABEL" "$NEW_DRIFT" "$DESC")"
if curl -s -m 10 -X POST -H 'Content-Type: application/json' -d "$ALERT_PAYLOAD" "$ALERTMANAGER_URL/api/v2/alerts" >/dev/null 2>&1; then
  echo "strategy-audit-drift: Alertmanager POST OK ($ALERTMANAGER_URL)"
else
  echo "strategy-audit-drift: Alertmanager POST FAILED (non-fatal)" >&2
fi

echo '{"wakeAgent":true,"reason":"strategy audit drift detected: '"$NEW_DRIFT"' new silent mutation(s)","signals":"'"$SIG"'"}'
exit 0
