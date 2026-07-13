#!/usr/bin/env bash
# Per-loop token/cost report over the last 24h, grouped by loop name (title before '·').
# Honest metric: tokens. Dollars are $0 for gpt-5.5 (subscription-included) and unknown for grok/ollama.
# Delivered to Telegram by the cron that calls this.
DB="$HOME/.hermes/state.db"
since=$(( $(date +%s) - 86400 ))
echo "📊 *Fleet cost — last 24h* (by loop)"
echo ""
sqlite3 "$DB" "
SELECT
  CASE WHEN instr(title,' · ')>0 THEN substr(title,1,instr(title,' · ')-1)
       WHEN title IS NULL OR title='' THEN '(' || model || ' untitled)'
       ELSE title END AS loop,
  COUNT(*) runs,
  SUM(input_tokens+output_tokens+reasoning_tokens) tok,
  GROUP_CONCAT(DISTINCT model) models
FROM sessions
WHERE source='cron' AND started_at > $since
GROUP BY loop
ORDER BY tok DESC
LIMIT 15;" 2>/dev/null | awk -F'|' '{ printf "• %-32s %5s runs  %8.1fk tok  [%s]\n", substr($1,1,32), $2, $3/1000, $4 }'
echo ""
total=$(sqlite3 "$DB" "SELECT SUM(input_tokens+output_tokens+reasoning_tokens) FROM sessions WHERE source='cron' AND started_at > $since;" 2>/dev/null)
echo "Σ cron total: $(( ${total:-0} / 1000 ))k tokens / 24h"
echo "Note: \$0 = gpt-5.5 subscription-included; grok/ollama cost unpriced (track tokens, not \$)."
