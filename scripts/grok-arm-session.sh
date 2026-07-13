#!/usr/bin/env bash
# grok-arm-session.sh — invoke one Grok-ARM operating-loop iteration (paper research seat).
# Cron-driven every 4h. Grok-ARM's standing goal + constraints live in the vault goal prompt;
# this wrapper only wakes it. Observe its behavior via grok-arm/STATUS.md (append-only journal).
set -u
export PATH="/home/frank/.local/bin:/home/frank/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
cd /home/frank/obsidian/sycode-trading/grok-arm || exit 1

timeout 1500 grok -p "You are GROK-ARM, resuming your standing autonomous goal. First read /home/frank/obsidian/sycode-trading/grok-arm/GROK-GOAL-PROMPT.md (your mission, hard constraints, formats — they are absolute), then STATUS.md and your open theses in trade-log/. Execute exactly ONE operating-loop iteration: (1) resolve any open theses whose stop/target/horizon has been hit, using live market prices, appending resolutions in the required format; (2) do a fresh X/news/sentiment scan and record substantive findings in research/; (3) log new theses ONLY if they clear your strategy's R:R + cost + event rules — logging zero is always acceptable, forcing trades is not; (4) update STATUS.md (stats, in-progress, questions for Frank). Respect every hard constraint: paper only, append-only journal, thesis-before-outcome, count everything, write only inside grok-arm/. If it is Sunday UTC and no weekly review exists for this week, write it." \
  --always-approve --output-format plain >> /home/frank/logs/grok-arm-sessions.log 2>&1
echo "[$(date -u +%FT%TZ)] session exit=$?" >> /home/frank/logs/grok-arm-sessions.log

# POST-SESSION SCOPE AUDIT (added 2026-07-13 after Grok-ARM's first session wrote a
# 180-line uncommitted change into the prod-cron-executed shared trading checkout).
# Grok's in-prompt constraints are not enforcement; this is. Fail-closed: any tracked
# modification in the trading repo, or vault writes outside grok-arm/, alerts Frank.
viol=""
tracked=$(git -C /home/frank/sycode-trading status --porcelain 2>/dev/null | grep -a -c -v '^??')
[ "${tracked:-0}" -gt 0 ] && viol="trading-repo tracked mods: ${tracked}; "
outside=$(git -C /home/frank/obsidian/sycode-trading status --porcelain 2>/dev/null | grep -a -v '^?? grok-arm/\|^ M grok-arm/\|^A  grok-arm/\|grok-arm/' | grep -a -c . || true)
[ "${outside:-0}" -gt 0 ] && viol="${viol}vault writes outside grok-arm/: ${outside}"
if [ -n "$viol" ]; then
  echo "[$(date -u +%FT%TZ)] SCOPE-VIOLATION: $viol" >> /home/frank/logs/grok-arm-sessions.log
  export HERMES_HOME=/home/frank/.hermes
  hermes send -q -t whatsapp:Frank -s "🚨 Grok-ARM scope violation" \
    "Post-session audit found writes outside Grok-ARM's sandbox: ${viol}. Nothing auto-reverted (other agents may own some changes) — fable seat will triage. Log: ~/logs/grok-arm-sessions.log" || true
fi
