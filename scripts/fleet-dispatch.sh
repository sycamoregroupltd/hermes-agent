#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# fleet-dispatch.sh — Hardened dispatcher for all PM boards.
# Canonical implementation for the Jarvis profile-local fleet-dispatch.sh exec shim.
# t_8c18ef11 fix: always call `hermes kanban dispatch`; do not infer readiness
# from `hermes kanban list | grep '^●'` because glyph/status rendering drift hid ready tasks.
# Dispatches up to N kanban tasks per board each run.
# Called by fleet-dispatch-loop cron or manually.

set -euo pipefail

MAX_PER_BOARD="${1:-2}"
LOGDIR="${FLEET_DISPATCH_LOGDIR:-/home/frank/.hermes/scripts/logs}"
ALLOWLIST_FILE="${CODEX_SELECTIVE_ALLOWLIST:-/home/frank/.hermes/state/codex-selective-dispatch-allowlist.json}"
SELECTIVE_STATE_FILE="${CODEX_SELECTIVE_STATE:-/home/frank/.hermes/state/codex-selective-dispatch.json}"
HERMES_BIN="${FLEET_DISPATCH_HERMES:-hermes}"
SELECTIVE_ENABLED="${FLEET_SELECTIVE_DISPATCH_ENABLED:-0}"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/fleet-dispatch_$(date -u +%Y%m%d_%H%M%S).log"

log(){ echo "[$(date -u +%Y%m%dT%H:%M:%SZ)] $*" | tee -a "$LOGFILE"; }

selective_status_json(){
    python3 - "$SELECTIVE_STATE_FILE" "$ALLOWLIST_FILE" <<'PY'
import datetime
import json
import pathlib
import re
import sys
import time

state_path = pathlib.Path(sys.argv[1])
allowlist_path = pathlib.Path(sys.argv[2])
profile_re = re.compile(r"^[A-Za-z0-9_.-]+$")

def emit(**kwargs):
    print(json.dumps(kwargs, separators=(",", ":")))


def parse_ts(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            pass
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.datetime.fromisoformat(s).timestamp()
        except ValueError:
            return None
    return None

try:
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
except Exception:
    emit(active=True, valid=False, reason="selective_state_malformed", profiles=[])
    raise SystemExit(0)
active = bool(state.get("tripped") or state.get("selective_active") or state.get("mode") == "selective")
if not active:
    emit(active=False, valid=True, reason="normal", profiles=[])
    raise SystemExit(0)
try:
    data = json.loads(allowlist_path.read_text())
except FileNotFoundError:
    emit(active=True, valid=False, reason="allowlist_missing", profiles=[])
    raise SystemExit(0)
except Exception:
    emit(active=True, valid=False, reason="allowlist_malformed", profiles=[])
    raise SystemExit(0)
if not isinstance(data, dict) or data.get("mode") != "selective":
    emit(active=True, valid=False, reason="allowlist_not_selective", profiles=[])
    raise SystemExit(0)
expires_at = parse_ts(data.get("expires_at"))
if expires_at is None or expires_at <= time.time():
    emit(active=True, valid=False, reason="allowlist_expired", profiles=[])
    raise SystemExit(0)
profiles = data.get("profiles")
if not isinstance(profiles, list) or any(not isinstance(p, str) or not profile_re.fullmatch(p) for p in profiles):
    emit(active=True, valid=False, reason="allowlist_bad_profiles", profiles=[])
    raise SystemExit(0)
emit(active=True, valid=True, reason="selective", profiles=sorted(set(profiles)), expires_at=data.get("expires_at"))
PY
}

frontier_json(){
    local dry_run_payload="$1"
    local status_payload="$2"
    python3 - "$dry_run_payload" "$status_payload" <<'PY'
import json
import sys

try:
    dry = json.loads(sys.argv[1])
    status = json.loads(sys.argv[2])
except Exception as exc:
    print(json.dumps({"ok": False, "reason": "json_parse_error", "error": type(exc).__name__}))
    raise SystemExit(0)
allowed = set(status.get("profiles") or [])
spawned = dry.get("spawned") if isinstance(dry, dict) else None
if not isinstance(spawned, list):
    print(json.dumps({"ok": False, "reason": "unexpected_dry_run_schema"}))
    raise SystemExit(0)
frontier = []
for item in spawned:
    if not isinstance(item, dict):
        print(json.dumps({"ok": False, "reason": "unexpected_spawned_item"}))
        raise SystemExit(0)
    task_id = item.get("task_id")
    assignee = item.get("assignee")
    if not isinstance(task_id, str) or not isinstance(assignee, str) or not assignee:
        print(json.dumps({"ok": False, "reason": "unexpected_spawned_fields"}))
        raise SystemExit(0)
    frontier.append({"task_id": task_id, "assignee": assignee})
assignees = sorted({item["assignee"] for item in frontier})
disallowed = sorted(a for a in assignees if a not in allowed)
print(json.dumps({
    "ok": True,
    "count": len(frontier),
    "assignees": assignees,
    "disallowed": disallowed,
}, separators=(",", ":")))
PY
}

json_get_bool(){ python3 -c 'import json,sys; print("true" if json.loads(sys.argv[1]).get(sys.argv[2]) else "false")' "$1" "$2"; }
json_get_text(){ python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2], ""))' "$1" "$2"; }
json_get_count(){ python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2], 0))' "$1" "$2"; }
json_get_csv(){ python3 -c 'import json,sys; print(",".join(json.loads(sys.argv[1]).get(sys.argv[2], [])))' "$1" "$2"; }

normal_dispatch_board(){
    local board="$1"
    local output dispatched
    output=$("$HERMES_BIN" kanban --board "$board" dispatch --max "$MAX_PER_BOARD" --json 2>/dev/null || true)
    dispatched=$(echo "$output" | grep -o '"task_id"' | wc -l || echo 0)
    log "  Dispatched: $dispatched"
}

selective_dispatch_board(){
    local board="$1"
    local status="$2"
    local dry_output frontier ok count disallowed assignees real_output dispatched
    dry_output=$("$HERMES_BIN" kanban --board "$board" dispatch --dry-run --max "$MAX_PER_BOARD" --json 2>/dev/null || true)
    frontier=$(frontier_json "$dry_output" "$status")
    ok=$(json_get_bool "$frontier" ok)
    if [ "$ok" != "true" ]; then
        log "  selective_fail_closed board=$board reason=$(json_get_text "$frontier" reason)"
        return 0
    fi
    count=$(json_get_count "$frontier" count)
    assignees=$(json_get_csv "$frontier" assignees)
    disallowed=$(json_get_csv "$frontier" disallowed)
    if [ "$count" -eq 0 ]; then
        log "  selective_no_frontier board=$board"
        return 0
    fi
    if [ -n "$disallowed" ]; then
        log "  selective_skip_frontier board=$board count=$count assignees=$assignees disallowed=$disallowed"
        return 0
    fi
    # Keep the same live-concurrency cap used by the dry-run. Hermes currently
    # treats --max as running+spawned concurrency, not a per-tick spawn count;
    # lowering it to the dry-run frontier count can stall boards that already
    # have running tasks. The dry-run frontier remains the allowlist gate.
    real_output=$("$HERMES_BIN" kanban --board "$board" dispatch --max "$MAX_PER_BOARD" --json 2>/dev/null || true)
    dispatched=$(echo "$real_output" | grep -o '"task_id"' | wc -l || echo 0)
    log "  selective_dispatch board=$board frontier=$count assignees=$assignees dispatched=$dispatched cap=$MAX_PER_BOARD"
}

log "═══ FLEET DISPATCH (boards: upero, sycode-ai, sycode-trading, jarvis-os, yorkstone-supplies) ═══"
log "Max per board: $MAX_PER_BOARD"

if [[ "$SELECTIVE_ENABLED" =~ ^(1|true|True|yes|YES)$ ]]; then
    status_payload=$(selective_status_json)
else
    status_payload='{"active":false,"valid":true,"reason":"selective-disabled","profiles":[]}'
fi
selective_active=$(json_get_bool "$status_payload" active)
selective_valid=$(json_get_bool "$status_payload" valid)
selective_reason=$(json_get_text "$status_payload" reason)
if [ "$selective_active" = "true" ]; then
    if [ "$selective_valid" = "true" ]; then
        log "Selective dispatch active: allowlist valid expires=$(json_get_text "$status_payload" expires_at)"
    else
        log "Selective dispatch active: fail-closed reason=$selective_reason"
    fi
fi

for board in upero sycode-ai sycode-trading jarvis-os yorkstone-supplies; do
    log "--- Dispatching board: $board ---"
    if [ "$selective_active" = "true" ]; then
        if [ "$selective_valid" != "true" ]; then
            log "  selective_fail_closed board=$board reason=$selective_reason"
        else
            selective_dispatch_board "$board" "$status_payload"
        fi
    else
        normal_dispatch_board "$board"
    fi
done

log "=== Dispatch cycle complete ==="
