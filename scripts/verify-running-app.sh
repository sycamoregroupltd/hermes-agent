#!/usr/bin/env bash
# verify-running-app.sh — deterministic running-app gate for frontend tasks.
# The guardian/PM runs this BEFORE approving any task that touches a web route.
# "type-check green" is NOT acceptance — the page must actually render.
# Usage: verify-running-app.sh <base_url> <path> [host_header]
#   e.g. verify-running-app.sh http://127.0.0.1:4300 /marketplace upero.localhost
# Exit 0 + "VERIFY_PASS" only if HTTP 200 AND real content AND no error markers.
set -uo pipefail
BASE="${1:?usage: verify-running-app.sh <base_url> <path> [host_header]}"
PATH_="${2:?usage: verify-running-app.sh <base_url> <path> [host_header]}"
HOST="${3:-}"

hdr=()
[ -n "$HOST" ] && hdr=(-H "Host: $HOST")

tmp_headers=$(mktemp)
trap 'rm -f "$tmp_headers"' EXIT

body=$(curl -s -m 20 -D "$tmp_headers" "${hdr[@]}" "${BASE}${PATH_}" 2>/dev/null)
code=$(awk 'NR==1 { print $2 }' "$tmp_headers")
content_type=$(awk 'BEGIN{IGNORECASE=1} /^content-type:/ { sub(/^[^:]+:[[:space:]]*/, ""); gsub(/\r/, ""); print; exit }' "$tmp_headers")
x_powered_by=$(awk 'BEGIN{IGNORECASE=1} /^x-powered-by:/ { sub(/^[^:]+:[[:space:]]*/, ""); gsub(/\r/, ""); print; exit }' "$tmp_headers")

is_payload_response=0
printf '%s' "$x_powered_by" | grep -qi 'Payload' && is_payload_response=1

is_payload_admin_success=0
if [ "$is_payload_response" -eq 1 ] && [[ "$PATH_" == /admin* ]]; then
  # Payload/Next RSC embeds not-found fallback translations and component names in
  # otherwise successful admin pages. Payload branding alone is not a success
  # marker; require a concrete admin state before allowing RSC-only fallback
  # strings to be ignored by the not-found check below.
  if printf '%s' "$body" | grep -qiE '<title>[^<]*(Dashboard|Login|Create First User|Upero CMS)|Payload admin (dashboard|login)|Payload is a headless CMS'; then
    is_payload_admin_success=1
  fi
fi

is_payload_json_collection=0
if [ "$is_payload_response" -eq 1 ] && printf '%s' "$content_type" | grep -qi 'application/json'; then
  if printf '%s' "$body" | python3 -c 'import json,sys
try:
    data=json.load(sys.stdin)
except Exception:
    sys.exit(1)
required={"docs","totalDocs","totalPages","page","limit","hasNextPage","hasPrevPage"}
sys.exit(0 if isinstance(data, dict) and required.issubset(data.keys()) and isinstance(data.get("docs"), list) else 1)
' 2>/dev/null; then
    is_payload_json_collection=1
  fi
fi

fail() { echo "VERIFY_FAIL ${PATH_} :: $1 (HTTP ${code})"; exit 1; }

[ "$code" = "200" ] || fail "non-200"
body_for_not_found="$body"
if [ "$is_payload_admin_success" -eq 1 ]; then
  # Successful Payload admin pages can include translated not-found fallback text
  # inside Next RSC bootstrap script chunks. Strip only those script chunks, then
  # preserve the generic guardrail for any visible/adversarial not-found content.
  body_for_not_found=$(printf '%s' "$body" | perl -0pe 's#<script[^>]*>[^<]*__next_f\.push\([^<]*Page not found[^<]*\)[^<]*</script>##gis')
fi
echo "$body_for_not_found" | grep -qi "Page not found" && fail "renders 404 page"
server_error_re="Application error|Internal Server|(^|[^[:alnum:]])(HTTP[[:space:]]*)?500([^[:alnum:]]|$).{0,80}(error|server)|(error|server).{0,80}(^|[^[:alnum:]])(HTTP[[:space:]]*)?500([^[:alnum:]]|$)"
echo "$body" | grep -qiE "$server_error_re" && fail "renders server-error"
# Next.js error pages embed __next_error__ / a single error boundary — catch the shell
echo "$body" | grep -q "__next_error__"             && fail "next.js error boundary rendered"
# Next.js App Router's RSC bootstrap commonly emits protocol sentinels like
# `$undefined` in the HTML payload. Those are not user-visible broken data, so
# strip that exact token before counting genuine undefined leaks.
body_without_rsc_undefined=$(printf '%s' "$body" | perl -pe 's/\$undefined//g')
ucount=$(printf '%s' "$body_without_rsc_undefined" | grep -o "undefined" | wc -l)
[ "${ucount:-0}" -gt 5 ]                             && fail "${ucount}x 'undefined' in output (likely broken data)"
bytes=$(echo -n "$body" | wc -c)
if [ "$is_payload_json_collection" -ne 1 ]; then
  [ "${bytes:-0}" -lt 500 ]                          && fail "suspiciously small body (${bytes}b)"
fi

echo "VERIFY_PASS ${PATH_} :: HTTP 200, ${bytes}b, real content"
exit 0
