#!/usr/bin/env bash
# R8 remediation apply helper (t_c826fb72) — NOT executed by the drafting seat.
# Run this ONLY as an interactive/live operator (Frank or an authorized seat)
# who can answer the native protected-instruction-file approval prompt for
# each SOUL.md write. Never run headless; never retry a rejected prompt.
#
# What it does: copies the 5 mechanically-fixed SOUL.md files in ./fixed/
# over the live profile SOULs, after diffing live-vs-original to confirm the
# live file has not drifted since this draft was made.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILES=/home/frank/.hermes/profiles

for name in buzzgw jarvis-voice research-trading trading-devops yorkstone-supplies-pm; do
  live="$PROFILES/$name/SOUL.md"
  orig="$HERE/original/$name.SOUL.md"
  fixed="$HERE/fixed/$name.SOUL.md"
  if ! diff -q "$orig" "$live" >/dev/null 2>&1; then
    echo "SKIP $name: live SOUL.md has drifted since this draft was captured — re-diff by hand." >&2
    continue
  fi
  cp "$live" "$live.bak.$(date +%Y%m%dT%H%M%S)"
  cp "$fixed" "$live"
  echo "applied: $name"
done

echo "Re-run the P8 SOUL oracle to confirm TOTAL_SOUL_ABSOLUTE_HOME_FRANK_HITS=0."
