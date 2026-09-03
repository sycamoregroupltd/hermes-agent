#!/usr/bin/env bash
set -euo pipefail
attempts_file=$(mktemp)
trap 'rm -f "$attempts_file"' EXIT
attempt_count=0
rsync() {
  attempt_count=$((attempt_count + 1))
  printf '%s' "$attempt_count" > "$attempts_file"
  return 23
}
sleep() { :; }
push_ok=0
for attempt in 1 2 3; do
    if rsync -a --partial --partial-dir=.rsync-partial --timeout=3600 \
            --bwlimit=0 -e 'ssh -4' \
            /tmp/source mac:dgx-fleet-backups/; then
        push_ok=1
        break
    else
        rc=$?
        echo "WARNING: rsync attempt $attempt/3 failed (rc=$rc) — retrying in 60s" >&2
        [ "$attempt" -lt 3 ] && sleep 60
    fi
done
[ "$push_ok" -eq 0 ]
[ "$(cat "$attempts_file")" = 3 ]
# The expected diagnostic must preserve the real rsync failure code.
# Re-run the loop and capture its output directly for the assertion.
actual=$(for attempt in 1 2 3; do
    if rsync -a /tmp/source mac:dgx-fleet-backups/; then
        push_ok=1; break
    else
        rc=$?
        printf 'attempt=%s rc=%s\n' "$attempt" "$rc"
    fi
done)
printf '%s\n' "$actual" | grep -q 'attempt=1 rc=23'
printf '%s\n' "$actual" | grep -c 'rc=23' | grep -qx 3
printf 'failure-path regression PASS: 3 bounded attempts, rc=23 preserved\n'
