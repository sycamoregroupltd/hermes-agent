#!/usr/bin/env bash
# on_session_start / on_session_end / subagent_stop hook: append every agent's
# lifecycle to ONE central fleet-activity feed — so "what is every agent doing right
# now" is answerable at a glance. Non-blocking, fail-quiet, always returns {}.
# Also emits structured JSONL to ~/.hermes/logs/structured/ with trace_id per spec.
set -uo pipefail
payload=$(cat 2>/dev/null)
ok() { echo '{}'; exit 0; }
FEED="/home/frank/.hermes/logs/fleet-activity.log"
STRUCTURED="/home/frank/.hermes/logs/structured/session.jsonl"
mkdir -p "$(dirname "$FEED")" "$(dirname "$STRUCTURED")" 2>/dev/null || ok

line=$(HERMES_HOOK_PAYLOAD="$payload" python3 - 2>/dev/null <<'PY'
import json, os, re, sys, time, datetime

def first(*vals, default='-'):
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return default

try:
    raw = os.environ.get('HERMES_HOOK_PAYLOAD', '')
    d = json.loads(raw) if raw.strip() else {}
except Exception:
    d = {}

extra = d.get('extra') if isinstance(d.get('extra'), dict) else {}
env = os.environ
sid = first(d.get('session_id'), d.get('parent_session_id'))
sid_short = sid[:32] if sid != '-' else '-'
ev = first(d.get('hook_event_name'), default='?')

# Profiles can arrive via the hook payload, the process env, or occasionally
# as a token in generated session ids. Prefer explicit values; keep unknown as '-'.
profile = first(
    d.get('profile'), d.get('profile_name'), d.get('hermes_profile'), d.get('config_profile'),
    extra.get('profile'), extra.get('profile_name'), extra.get('hermes_profile'),
    env.get('HERMES_PROFILE'), env.get('HERMES_CONFIG_PROFILE'), env.get('HERMES_PROFILE_NAME'),
)
if profile == '-':
    m = re.search(r'(?:profile|prof)[=_:-]([A-Za-z0-9_.-]+)', sid)
    if m:
        profile = m.group(1)

# Kanban task ids can arrive in several places depending on worker vs gateway
# path. Only accept native Kanban ids (t_<hex-ish>); other task_id payloads
# can be gateway/request UUIDs, not board tasks.
task = '-'
for candidate in (
    d.get('task_id'), d.get('kanban_task_id'), extra.get('task_id'), extra.get('kanban_task_id'),
    env.get('HERMES_KANBAN_TASK'), env.get('KANBAN_TASK_ID'),
):
    s = str(candidate or '').strip()
    if re.fullmatch(r't_[0-9a-fA-F]{6,}', s):
        task = s
        break
if task == '-':
    scan = ' '.join(str(x or '') for x in [sid, d.get('cwd'), d.get('source'), d.get('title')])
    m = re.search(r'\bt_[0-9a-fA-F]{6,}\b', scan)
    if m:
        task = m.group(0)

platform = first(d.get('platform'), d.get('source'), extra.get('platform'), default='?')
model = first(d.get('model'), extra.get('model'), default='?')
ts = time.strftime('%H:%M:%S')
ctx = f'profile={profile} task={task}'

# Structured logging per Hermes spec
trace_id = first(
    env.get('HERMES_TRACE_ID'),
    extra.get('trace_id'),
    d.get('trace_id'),
    default='-'
)
ts_iso = datetime.datetime.utcnow().isoformat(timespec='milliseconds') + 'Z'
level = 'INFO'
event_name = ev if ev not in ('?', '') else 'unknown'
data = {
    'session_id': sid if sid != '-' else None,
    'task_id': task if task != '-' else None,
    'platform': platform if platform != '?' else None,
    'model': model if model != '?' else None,
}
# event-specific
if ev == 'on_session_end':
    data['completed'] = d.get('completed')
    data['interrupted'] = d.get('interrupted')
elif ev == 'subagent_stop':
    data['child_profile'] = first(d.get('child_profile'), extra.get('child_profile'), default=None)
    data['child_task_id'] = first(d.get('child_task_id'), extra.get('child_task_id'), default=None)
    data['child_role'] = d.get('child_role')
    data['child_status'] = d.get('child_status')
    data['duration_ms'] = d.get('duration_ms')
    data['child_summary'] = (d.get('child_summary') or '')[:80] if d.get('child_summary') else None

# clean None from data
data = {k: v for k, v in data.items() if v is not None}

structured = {
    'trace_id': trace_id,
    'ts': ts_iso,
    'level': level,
    'profile': profile,
    'event': event_name,
    'data': data
}

# write structured JSONL (fail quiet)
try:
    with open('/home/frank/.hermes/logs/structured/session.jsonl', 'a') as sf:
        sf.write(json.dumps(structured, separators=(',', ':')) + '\n')
except Exception:
    pass

# human readable line (existing behaviour)
if ev == 'on_session_start':
    print(f'{ts} START   {ctx} sid={sid_short} model={model} platform={platform}')
elif ev == 'on_session_end':
    print(f'{ts} END     {ctx} sid={sid_short} completed={d.get("completed", "?")} interrupted={d.get("interrupted", "?")}')
elif ev == 'subagent_stop':
    child_profile = first(d.get('child_profile'), extra.get('child_profile'), default='-')
    child_task = first(d.get('child_task_id'), extra.get('child_task_id'), default='-')
    child_ctx = f'child_profile={child_profile} child_task={child_task}'
    print(f'{ts} CHILD   {ctx} {child_ctx} role={d.get("child_role") or "-"} status={d.get("child_status", "?")} {d.get("duration_ms", "?")}ms :: {(d.get("child_summary") or "")[:80]}')
else:
    print(f'{ts} {ev} {ctx} sid={sid_short}')
PY
) || { echo '{}'; exit 0; }
[ -n "$line" ] && echo "$line" >> "$FEED" 2>/dev/null
# keep the feed bounded (last 2000 lines)
if [ "$(wc -l < "$FEED" 2>/dev/null || echo 0)" -gt 2500 ]; then
  tail -2000 "$FEED" > "$FEED.tmp" 2>/dev/null && mv "$FEED.tmp" "$FEED" 2>/dev/null || true
fi
ok
