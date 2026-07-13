#!/usr/bin/env bash
set -euo pipefail
PSQL=(docker exec -e PGPASSWORD=postgres sycodetrading-supabase-db psql -h localhost -U postgres -d postgres -v ON_ERROR_STOP=1 -P pager=off)
echo "# Arena runtime 24h monitor — t_aadefb79"
date -u '+generated_at=%Y-%m-%dT%H:%M:%SZ'
echo
echo "## Proof/readiness"
(curl -fsS http://localhost:3001/ready 2>/dev/null || true) | python3 -c 'import json,sys; s=sys.stdin.read();
try:
 data=json.loads(s); print("status=%s proofModeEnabled=%s proofInvalidated=%s readySince=%s" % (data.get("status"), data.get("proof",{}).get("proofModeEnabled"), data.get("proof",{}).get("proofInvalidated"), data.get("readySince")))
except Exception: print(s[:1000])'
echo
echo "## AI agent ratings"
"${PSQL[@]}" -c "SELECT c.strategy_id, c.display_name, r.rating, r.rd, r.volatility, r.games_played, r.wins, r.losses, r.updated_at FROM strategy_catalog c JOIN strategy_ratings r USING(strategy_id) WHERE c.strategy_id IN ('llm_confluence_purist','llm_contrarian_beta','llm_momentum_alpha') ORDER BY c.strategy_id;"
echo
echo "## Arena/outcome activity (24h)"
"${PSQL[@]}" -c "SELECT COUNT(*) total, MAX(decided_at) latest_decision, COUNT(*) FILTER (WHERE decided_at > now() - interval '24 hours') decisions_24h FROM strategy_arena_decisions; SELECT COUNT(*) total, MAX(created_at) latest_outcome, COUNT(*) FILTER (WHERE created_at > now() - interval '24 hours') outcomes_24h, COUNT(*) FILTER (WHERE created_at > now() - interval '24 hours' AND is_simulated) simulated_24h FROM strategy_outcomes; SELECT strategy_id, COUNT(*) n, COUNT(*) FILTER (WHERE was_executed) executed, ROUND(AVG(COALESCE(actual_pnl_percent,counterfactual_pnl_percent))::numeric,4) avg_pnl, MAX(created_at) latest FROM strategy_outcomes WHERE created_at > now() - interval '24 hours' GROUP BY strategy_id ORDER BY n DESC LIMIT 20;"
echo
echo "## Closed-loop paper registrations"
"${PSQL[@]}" -c "SELECT COUNT(*) AS arena_ml_registered, COUNT(*) FILTER (WHERE enabled) enabled, COUNT(*) FILTER (WHERE trading_mode='paper') paper, MAX(created_at) latest FROM strategies WHERE meta->>'source'='arena_ml_closed_loop'; SELECT id, name, engine, enabled, trading_mode, created_at, meta->>'arenaCandidateId' AS arena_candidate_id, meta->>'liveTradingApproved' AS live_approved, meta->>'tradeIntentGenerationApproved' AS intent_approved FROM strategies WHERE meta->>'source'='arena_ml_closed_loop' ORDER BY created_at DESC LIMIT 20;"
echo
echo "## Candidate/prompt draft state"
"${PSQL[@]}" -c "SELECT COUNT(*) AS drafts, state, MAX(drafted_at) latest FROM strategy_prompt_drafts GROUP BY state ORDER BY latest DESC NULLS LAST; SELECT status, COUNT(*) runs, MAX(created_at) latest FROM strategy_prompt_runs GROUP BY status ORDER BY latest DESC NULLS LAST;"
echo
echo "## Recent scheduler logs"
docker logs --since=24h sycodetrading-server 2>&1 | tr -d '\000' | grep -Ei 'StrategyArenaScheduler|ArenaCounterfactualJob|StrategyOutcomeService|StrategyPromptCoach|StrategyArenaClosedLoop|SHAP|ml validation' | tail -120 || true
