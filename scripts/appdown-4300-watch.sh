#!/usr/bin/env bash
# appdown-4300-watch — no-agent wakeAgent gate for Upero storefront on :4300.
# Scans live HTTP health periodically; wakes NSE only on a new unhealthy signature.
# Fail-open-to-silent on internal uncertainty: final line is always wakeAgent JSON.
set -uo pipefail

PORT="${APPDOWN_PORT:-4300}"
URL="${APPDOWN_URL:-http://127.0.0.1:${PORT}/}"
NAME="${APPDOWN_NAME:-upero-web}"
STATE="${APPDOWN_STATE:-/home/frank/.hermes/cron/state/appdown-${PORT}-watch.seen}"
LOG_HINTS="${APPDOWN_LOG_HINTS:-/home/frank/upero/logs/upero-web-dev.log /home/frank/upero/logs/upero-web.service.log}"
OK_REGEX="${APPDOWN_OK_REGEX:-^[23][0-9][0-9]$}"
TIMEOUT="${APPDOWN_TIMEOUT:-5}"

mkdir -p "$(dirname "$STATE")" 2>/dev/null || { echo '{"wakeAgent": false}'; exit 0; }
touch "$STATE" 2>/dev/null || { echo '{"wakeAgent": false}'; exit 0; }

command -v curl >/dev/null 2>&1 || { echo "appdown-${PORT}-watch: curl unavailable; failing open"; echo '{"wakeAgent": false}'; exit 0; }

body_file=$(mktemp 2>/dev/null || printf '/tmp/appdown-%s.%s' "$PORT" "$$")
headers_file=$(mktemp 2>/dev/null || printf '/tmp/appdown-%s.headers.%s' "$PORT" "$$")
cleanup() { rm -f "$body_file" "$headers_file" 2>/dev/null || true; }
trap cleanup EXIT

code=$(curl -sS -L -m "$TIMEOUT" -D "$headers_file" -o "$body_file" -w '%{http_code}' "$URL" 2>&1)
curl_rc=$?
# If curl itself failed, its stderr is in $code and no reliable HTTP code exists.
if [ "$curl_rc" -eq 0 ] && printf '%s' "$code" | grep -Eq "$OK_REGEX"; then
  : > "$STATE" 2>/dev/null || true
  echo "appdown-${PORT}-watch: ${NAME} healthy at ${URL} (HTTP ${code})"
  echo '{"wakeAgent": false}'
  exit 0
fi

status="curl_rc=${curl_rc}"
if [ "$curl_rc" -eq 0 ]; then
  status="http=${code}"
fi

cause=""
if [ -s "$body_file" ]; then
  cause=$(python3 - "$body_file" <<'PY' 2>/dev/null || true
import html, json, re, sys
p=sys.argv[1]
s=open(p,'rb').read(200000).decode('utf-8','replace')
# Next error payload often embeds useful JSON in __NEXT_DATA__.
m=re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', s, re.S)
if m:
    try:
        data=json.loads(html.unescape(m.group(1)))
        err=data.get('err') or {}
        msg=err.get('message') or ''
        if msg:
            print(re.sub(r'\s+', ' ', msg)[:260])
            raise SystemExit
    except Exception:
        pass
# Generic module/build/DB failures from dev overlay or rendered text.
patterns=[r'Module not found:.*', r"Cannot find module .*", r"Database `[^`]+` does not exist", r"Could not find a production build.*", r"PrismaClientInitializationError.*"]
for pat in patterns:
    mm=re.search(pat, s, re.S)
    if mm:
        print(re.sub(r'\s+', ' ', html.unescape(mm.group(0)))[:260])
        raise SystemExit
text=re.sub(r'<[^>]+>', ' ', s)
text=html.unescape(re.sub(r'\s+', ' ', text)).strip()
print(text[:180])
PY
)
fi

if [ -z "$cause" ]; then
  for log in $LOG_HINTS; do
    [ -r "$log" ] || continue
    hint=$(tail -80 "$log" 2>/dev/null | grep -E "Module not found|Cannot find module|Database .* does not exist|Could not find a production build|PrismaClientInitializationError|GET / 500| ⨯ " | tail -5 | tr '\n' ';' | sed 's/["\\]/_/g' | cut -c1-260 || true)
    [ -n "$hint" ] && { cause="$hint"; break; }
  done
fi
[ -n "$cause" ] || cause="no-cause-extracted"
cause=$(printf '%s' "$cause" | tr '\n' ' ' | sed 's/["\\]/_/g' | cut -c1-260)

sig=$(printf '%s|%s|%s' "$URL" "$status" "$cause" | md5sum 2>/dev/null | cut -c1-12 || printf '%s' "$status")
if grep -qxF "$sig" "$STATE" 2>/dev/null; then
  echo "appdown-${PORT}-watch: condition persists (already woke): ${NAME} ${status} ${cause}"
  echo '{"wakeAgent": false}'
else
  echo "$sig" >> "$STATE" 2>/dev/null || true
  echo "WAKE NERVOUS-SYSTEM — appdown:${PORT} ${NAME} unhealthy at ${URL}: ${status}; cause=${cause}"
  echo '{"wakeAgent": true}'
fi
