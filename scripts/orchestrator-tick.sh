#!/usr/bin/env bash
# orchestrator-tick.sh — REPURPOSED 2026-08-01 on Frank's instruction:
# headless `claude -p` is not a Max-plan-included method, so the PRIMARY
# loop is now the Claude Code session harness cron (kernel §0). This hermes
# no-agent cron job is the DURABLE FALLBACK: silent while the primary
# heartbeat is fresh; beyond FALLBACK_AGE it runs ONE bounded grok
# stabilizer pass (A0-A1 only — the stabilizer prompt forbids merges,
# deploys, file edits, and all A3 surfaces) and alerts.
# Empty stdout = healthy/silent (no-agent watchdog pattern).
set -u
export PATH="/home/frank/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
HB=/home/frank/dgx-fable-orchestrator/state/heartbeat
FALLBACK_AGE=14400   # matches the watchdog MAX: primary presumed dead beyond this
PROMPT=/home/frank/obsidian-fleet-vault/Orchestration/JARVIS-ORCHESTRATOR-FALLBACK-STABILIZER.md
STATE=/home/frank/dgx-fable-orchestrator/state

if [ ! -f "$HB" ]; then
  echo "ORCH FALLBACK: no heartbeat file at $HB — primary loop never ran; NOT running the grok stabilizer blind (one primary cycle must establish the baseline first)."
  exit 0
fi
age=$(( $(date +%s) - $(stat -c %Y "$HB") ))
[ "$age" -le "$FALLBACK_AGE" ] && exit 0

if [ ! -r "$PROMPT" ]; then
  echo "ORCH FALLBACK BROKEN: stabilizer prompt missing at $PROMPT (primary heartbeat ${age}s stale and no fallback possible)."
  exit 0
fi
exec 9>"$STATE/fallback.lock"
flock -n 9 || exit 0   # a fallback pass is already live

echo "ORCH FALLBACK: primary heartbeat ${age}s stale — running ONE bounded grok stabilizer pass (A0-A1 only). Primary needs revival: reopen the Claude session on the DGX."
timeout --signal=TERM --kill-after=60 1800 \
  grok -p "$(cat "$PROMPT")" > "$STATE/last-fallback.txt" 2>&1
echo "ORCH FALLBACK: grok stabilizer exit=$? — transcript in state/last-fallback.txt."
