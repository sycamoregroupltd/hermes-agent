#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# Refresh FLEET-STATUS.md — the snapshot phone-Jarvis reads for instant fleet answers.
# Auto-discovers ALL boards (incl. new projects). no_agent cron.
set -uo pipefail
OUT="${FLEET_STATUS_OUT:-/home/frank/uaa-rules/FLEET-STATUS.md}"
# Kanban workers export HERMES_KANBAN_DB/HERMES_KANBAN_BOARD for their own
# task. The status refresh is a fleet-wide snapshot, so never let a caller's
# board-scoped environment leak into helper commands used during this run.
unset HERMES_KANBAN_DB HERMES_KANBAN_BOARD
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
PREV=""
if [ -f "$OUT" ]; then
  PREV=$(mktemp "${TMPDIR:-/tmp}/fleet-status-prev.XXXXXX")
  cp "$OUT" "$PREV"
fi
{
  echo "# FLEET STATUS (auto-refreshed $ts)"
  echo "_Read this for an instant fleet answer. Per-board task counts + what's active._"
  echo
  for db in /home/frank/.hermes/kanban/boards/*/kanban.db; do
    b=$(basename "$(dirname "$db")")
    counts=$(sqlite3 -separator ' ' "$db" "SELECT status||'='||c FROM (SELECT status,COUNT(*) c FROM tasks GROUP BY status)" 2>/dev/null | tr '\n' ' ')
    [ -z "$counts" ] && continue
    done7=$(sqlite3 "$db" "SELECT COUNT(*) FROM tasks WHERE status='done' AND completed_at > CAST(strftime('%s','now','-7 days') AS INTEGER)" 2>/dev/null || echo 0)
    running=$(sqlite3 "$db" "SELECT COUNT(*) FROM tasks WHERE status='running'" 2>/dev/null || echo 0)
    echo "## $b"
    echo "- counts: $counts"
    echo "- last 7d done: $done7 | running now: $running"
    # what's running
    act=$(sqlite3 -separator ' | ' "$db" "SELECT id, COALESCE(assignee,'-'), substr(title,1,55) FROM tasks WHERE status='running' ORDER BY COALESCE(started_at, created_at), id LIMIT 3" 2>/dev/null)
    [ -n "$act" ] && printf '%s\n' "$act" | sed 's/^- /-/; s/^/- active: /'
    echo
  done
  echo "## Pending Frank (review-required / needs-approval, oldest first)"
  for db in /home/frank/.hermes/kanban/boards/*/kanban.db; do
    b=$(basename "$(dirname "$db")")
    sqlite3 -separator ' | ' "$db" "SELECT '$b', id, substr(title,1,50) FROM tasks WHERE status='blocked' ORDER BY created_at ASC LIMIT 5" 2>/dev/null | sed 's/^/- /'
  done
} > "$OUT.tmp" 2>/dev/null && mv "$OUT.tmp" "$OUT"

# Compare the newly refreshed live snapshot with the prior cached snapshot.
# This gives Jarvis a deterministic stale-cache/churn marker instead of letting
# fast board churn silently mask material blocker growth between refreshes.
if [ -n "$PREV" ] && [ -f "$PREV" ] && [ -f "$OUT" ]; then
  FLEET_STATUS_PREV="$PREV" FLEET_STATUS_CUR="$OUT" FLEET_STATUS_TS="$ts" python3 - <<'PY' >> "$OUT" 2>/dev/null || true
import os
import re
from datetime import datetime
from pathlib import Path

prev_path = Path(os.environ["FLEET_STATUS_PREV"])
cur_path = Path(os.environ["FLEET_STATUS_CUR"])
live_ts = os.environ.get("FLEET_STATUS_TS", "")

HEADER_RE = re.compile(r"^# FLEET STATUS \(auto-refreshed ([^)]+)\)")
BOARD_RE = re.compile(r"^## (.+)$")
COUNTS_RE = re.compile(r"^- counts:\s*(.*)$")


def parse(path: Path):
    ts = "unknown"
    board = None
    counts = {}
    for line in path.read_text(errors="replace").splitlines():
        m = HEADER_RE.match(line)
        if m:
            ts = m.group(1)
            continue
        m = BOARD_RE.match(line)
        if m:
            board = m.group(1).strip()
            continue
        m = COUNTS_RE.match(line)
        if m and board:
            row = {}
            for token in m.group(1).split():
                if "=" not in token:
                    continue
                key, value = token.split("=", 1)
                try:
                    row[key] = int(value.rstrip(","))
                except ValueError:
                    pass
            counts[board] = row
    return ts, counts


def parse_ts(value: str):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


prev_ts, prev_counts = parse(prev_path)
cur_ts, cur_counts = parse(cur_path)
prev_dt = parse_ts(prev_ts)
cur_dt = parse_ts(cur_ts if cur_ts != "unknown" else live_ts)
age_seconds = None
if prev_dt and cur_dt:
    age_seconds = max(0, int((cur_dt - prev_dt).total_seconds()))

rows = []
max_abs_delta = 0
material = []
for board in sorted(set(prev_counts) | set(cur_counts)):
    before = prev_counts.get(board, {})
    after = cur_counts.get(board, {})
    keys = sorted(set(before) | set(after))
    deltas = {k: after.get(k, 0) - before.get(k, 0) for k in keys}
    changed = {k: v for k, v in deltas.items() if v}
    if not changed:
        continue
    board_max = max(abs(v) for v in changed.values())
    max_abs_delta = max(max_abs_delta, board_max)
    blocked_delta = changed.get("blocked", 0)
    total_abs = sum(abs(v) for v in changed.values())
    # Alert only on material blocker growth/shrinkage. Done/running churn can be
    # high during active swarms and should be visible as deltas without paging.
    if abs(blocked_delta) > 3:
        material.append(board)
    delta_text = " ".join(f"{k}{v:+d}" for k, v in sorted(changed.items()))
    rows.append((board, delta_text, total_abs, blocked_delta))

age_text = "unknown" if age_seconds is None else f"{age_seconds}s"
state = "material-change" if material else "normal-churn"
print()
print("## Live-vs-cache delta")
print(f"- previous-cache: {prev_ts}")
print(f"- live-refresh: {cur_ts if cur_ts != 'unknown' else live_ts}")
print(f"- cache-age: {age_text}")
print(f"- state: {state}")
if material:
    print(f"- alert: material board-count delta detected ({', '.join(material)})")
else:
    print(f"- no-alert: normal churn only (max_abs_delta={max_abs_delta}, threshold=3)")
for board, delta_text, total_abs, blocked_delta in rows[:12]:
    print(f"- delta: {board} | {delta_text} | total_abs_delta={total_abs} | blocked_delta={blocked_delta:+d}")
if len(rows) > 12:
    print(f"- delta: ... {len(rows) - 12} additional board(s) changed")
PY
  rm -f "$PREV"
fi

# Deterministic false-Pending-Frank classifier/report path.
# Fail-open: FLEET-STATUS refresh remains successful even if triage cannot run.
TRIAGE_SCRIPT="/home/frank/.hermes/scripts/pending-frank-triage.py"
TRIAGE_OUT="${FLEET_STATUS_TRIAGE_OUT:-/home/frank/uaa-rules/PENDING-FRANK-TRIAGE.md}"
if [ "${FLEET_STATUS_TRIAGE:-1}" != "0" ] && [ -x "$TRIAGE_SCRIPT" ]; then
  if "$TRIAGE_SCRIPT" --status-file "$OUT" --a3-queue-from-scan > "$TRIAGE_OUT.tmp" 2>/dev/null; then
    mv "$TRIAGE_OUT.tmp" "$TRIAGE_OUT"
    {
      echo
      echo "## Pending Frank delegated triage"
      echo "- full report: $TRIAGE_OUT"
      grep -E '^(Pending Frank before|Classified critical-list|Classified delegated-review|Classified ambiguous|False Pending Frank after.*|Orphan ready/todo assignee=null count):' "$TRIAGE_OUT" | sed 's/^/- /'
    } >> "$OUT"
  else
    rm -f "$TRIAGE_OUT.tmp"
  fi
fi

# Read-only worker-visibility preflight for governor/phone anomaly scans.
# This runs after the main snapshot and triage report so it can only add
# evidence to the phone-readable status artifact. It must not create, unblock,
# dispatch, assign, or mutate kanban tasks.
WORKER_PREFLIGHT_SCRIPT="/home/frank/.hermes/scripts/worker_visibility_preflight.py"
WORKER_PREFLIGHT_OUT="${WORKER_VISIBILITY_PREFLIGHT_JSON:-/home/frank/uaa-rules/worker-visibility-preflight.json}"
if [ "${FLEET_STATUS_WORKER_PREFLIGHT:-1}" != "0" ] && [ -x "$WORKER_PREFLIGHT_SCRIPT" ]; then
  if "$WORKER_PREFLIGHT_SCRIPT" --json-out "$WORKER_PREFLIGHT_OUT" > "$OUT.worker-preflight.tmp" 2>/dev/null; then
    {
      echo
      echo "## Worker visibility preflight"
      sed -n '1,20p' "$OUT.worker-preflight.tmp"
    } >> "$OUT"
  fi
  rm -f "$OUT.worker-preflight.tmp"
fi

# Terminal-lane queue report (GAP t_2e808b44, no-black-holes rule).
# Terminal lanes (fable/codex/grok, external-*, orion-*) are non-spawnable by
# design — only a human (Frank) or a seat can drain them. Surface the parked
# cards so the lane tells the human it is filling instead of being a silent
# black hole. Read-only; never mutates any board. Fail-open.
TERMINAL_LANE_SCRIPT="/home/frank/.hermes/scripts/terminal-lane-queue-report.py"
if [ "${FLEET_STATUS_TERMINAL_LANE:-1}" != "0" ] && [ -x "$TERMINAL_LANE_SCRIPT" ]; then
  if "$TERMINAL_LANE_SCRIPT" --md >> "$OUT" 2>/dev/null; then
    :
  fi
  # Discord digest line: only non-empty when cards exceed the escalation age.
  DIGEST_LINE=$("$TERMINAL_LANE_SCRIPT" 2>/dev/null)
  if [ -n "$DIGEST_LINE" ]; then
    {
      echo
      echo "## Terminal-lane digest (Discord)"
      echo "$DIGEST_LINE"
    } >> "$OUT"
  fi
fi

echo "[SILENT] FLEET-STATUS.md refreshed $ts"
