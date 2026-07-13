#!/usr/bin/env bash
# stranded-work-monitor.sh — weekly OOB monitor for stranded/unpushed agent work.
#
# WHY (2026-07-13, fable seat): an audit found ~250 unpushed commits across ~50
# branches on DGX+Mac, including the ONLY copy of whole features, plus a dirty
# deploy build-tree that silently blocked deploys for 3 days. Per the
# silent-failure doctrine the plugged hole gets a tracked monitor in the same pass.
#
# Observe-only. Writes a digest to the vault (named consumer: Frank/seat via
# Learnings review + hermes send alert when findings exceed thresholds).
set -u
export PATH="/home/frank/.local/bin:/home/frank/.hermes/hermes-agent/venv/bin:/usr/bin:/bin:$PATH"
export HERMES_HOME=/home/frank/.hermes

AUDIT=/home/frank/.hermes/scripts/audit-stranded-worktrees.sh
OUT=/home/frank/obsidian/sycode-trading/operations/stranded-work-digest.md
ALERT_TARGET="${STRANDED_MON_ALERT_TARGET:-whatsapp:Frank}"
STALE_DAYS=2

mkdir -p "$(dirname "$OUT")"
now_iso=$(date -Is)
cutoff=$(date -d "-${STALE_DAYS} days" +%F)

findings=$(bash "$AUDIT" 2>/dev/null | awk -F'\t' -v c="$cutoff" '$3 < c' )
count=$(printf '%s' "$findings" | grep -a -c . || true)

# no-remote repo scan (single-copy risk) — top-level home repos only
noremote=""
for d in /home/frank /home/frank/*/; do
  [ -e "$d/.git" ] || continue
  git -C "$d" remote get-url origin >/dev/null 2>&1 && continue
  n=$(git -C "$d" rev-list --count HEAD 2>/dev/null || echo 0)
  [ "${n:-0}" -gt 0 ] && noremote="${noremote}${d} (${n} commits)\n"
done

{
  echo "# Stranded-work digest — ${now_iso}"
  echo
  echo "Worktrees with unpushed commits or dirty files older than ${STALE_DAYS}d: ${count}"
  echo
  echo '```'
  printf '%s\n' "$findings"
  echo '```'
  echo
  echo "## Repos with no remote (single-copy)"
  echo
  printf "$noremote" | sed 's/^/- /'
} > "$OUT"

if [ "${count:-0}" -gt 0 ]; then
  hermes send -q -t "$ALERT_TARGET" -s "🧹 stranded work: ${count} stale dirty/unpushed worktrees" \
    "Weekly stranded-work audit found ${count} worktrees with unpushed commits or dirty files older than ${STALE_DAYS} days. Digest: $OUT — salvage (commit+push) or sweep them; unpushed work is one janitor pass from gone." \
    || true
fi
exit 0
