#!/bin/bash
# Wave 2: Orthogonal data stream goal cards
# After all 14 data foundation cards confirmed complete
BOARD="sycode-trading"
echo "=== Wave 2: Orthogonal Data Streams ==="

# W2.1 — Stablecoin flow → forward return (highest priority from research, direct data match)
hermes kanban --board $BOARD create "W2.1: Build stablecoin exchange inflow → forward return feature from signal_journeys" \
  --assignee trading-data-oracle --priority 95 \
  --body "Frank-APPROVED 2026-07-04. Paper-only read-only. SAFETY BOUNDS: No live trading no strategy enablement no production deploy.

Gap: Research (Chi 2024, BIS 2025, Paeng 2024) shows USDT exchange inflows positively predict BTC/ETH forward returns with strong statistical significance. Our stablecoin_flows field in signal_journeys should capture this but is not yet a first-class feature feature.

Goal: 
1. Verify what stablecoin_flows actually captures (supply change? exchange-directed flows? both?)
2. Create a stablecoin_flow signal feature that computes: USDT inflow direction, magnitude, and lag
3. Add to signal_training_features_v1 view
4. Produce a 1-page documented test plan against clean_outcome_binary_24h labels

Acceptance: stablecoin_flow feature exists in training dataset with documented test plan.

Source: obsidian/quant-team/research/on-chain-cross-asset-lead-lag-edges-2026-06-29.md Priority 1" \
  --goal --goal-max-turns 12 --max-runtime 4h --idempotency-key w2-20260704-sf

# W2.2 — Funding rate + OI delta feature (second priority, all data in signal_journeys)
hermes kanban --board $BOARD create "W2.2: Build funding rate + OI delta → forward return feature" \
  --assignee trading-data-oracle --priority 93 \
  --body "Frank-APPROVED 2026-07-04. Paper-only read-only. SAFETY BOUNDS: No live trading.

Gap: Palazzi 2026 shows funding rate and OI predict BTC returns in ALL regimes. We have market_funding_rate and open_interest in signal_journeys but no consolidated feature.

Goal:
1. Create funding+OI composite feature: funding_rate + oi_delta + regime interaction
2. Test crowded-long squeeze signal (high positive funding + high OI → long liquidation cascade)
3. Add to signal_training_features_v1

Acceptance: funding_oi_squeeze feature exists in training dataset with documented test plan.

Source: obsidian/quant-team/research/on-chain-cross-asset-lead-lag-edges-2026-06-29.md Priority 2" \
  --goal --goal-max-turns 10 --max-runtime 4h --idempotency-key w2-20260704-foi

# W2.3 — Wallet-following signal integration (S-TIER already active, route into features)
hermes kanban --board $BOARD create "W2.3: Route wallet-following sidecar signals into signal_journeys features" \
  --assignee trading-data-oracle --priority 90 \
  --body "Frank-APPROVED 2026-07-04. Paper-only read-only. SAFETY BOUNDS: No live trading.

Gap: Wallet-following sidecar is active with S-TIER candidates identified. Pro-trader persistence is fixed. But wallet activity signals are not yet routing into signal_journeys as features.

Goal:
1. Catalog which wallet signals are available (pro-trader positions, wallet-forward-test roundtrips)
2. Create a walelt_signals feature mapping: top trader direction bias, position change velocity, profit-taking indicators
3. Add to signal_training_features_v1
4. Document which tier of wallet signals maps to which signal quality

Acceptance: wallet_signal features exist in training dataset with mapping documented.

Source: sidecar catalog + pro-trader persistence fix completed" \
  --goal --goal-max-turns 10 --max-runtime 4h --idempotency-key w2-20260704-ws

# W2.4 — Catalyst feed → market_news production (staging ready, build the remaining pieces)
hermes kanban --board $BOARD create "W2.4: Advance catalyst feed staging → meet 1000-row production gate" \
  --assignee trading-data-oracle --priority 88 \
  --body "Frank-APPROVED 2026-07-04. SAFETY BOUNDS: No production market_news write without Frank A3 approval. Staging-only.

Status: Catalyst feed staging is live at reports/catalyst-feed/market_news_ready/ (266 rows, 44d span). trading-risk-reviewer verdict was NEEDS_FIX_PROD_WRITE_NOT_APPROVED because corpus is 266/1000 rows.

Goal:
1. Let the feed accrue to 1000+ rows (accruing at ~6-8 rows/day from 6 RSS + Grok X sources)
2. Add a row-count monitor/alert that triggers at 1000
3. When triggered, route a NEW card to trading-risk-reviewer for final production write approval
4. DO NOT write to production market_news until Frank explicitly approves

Acceptance: Staging pipeline continues accruing. Monitor deployed. Review-ready at 1000 rows.

Source: sycode-trading t_0a634d59" \
  --goal --goal-max-turns 8 --max-runtime 4h --idempotency-key w2-20260704-cf

# W2.5 — Cross-exchange funding rate differentials (new data stream)
hermes kanban --board $BOARD create "W2.5: Build cross-exchange funding rate differential collector (Bybit Binance OKX)" \
  --assignee trading-devops --priority 85 \
  --body "Frank-APPROVED 2026-07-04. SAFETY BOUNDS: Paper-only read-only API calls. No trading.

Gap: We track Hyperliquid funding rates but funding rate ARBITRAGE across Bybit/Binance/OKX is a well-documented signal. Differential between venues predicts capital flows and directional pressure.

Goal:
1. Extend existing funding_rate_collector.py to pull funding from 3 additional exchanges (Bybit perpetual, Binance futures, OKX perpetual)
2. Store in funding_rate_history table
3. Compute cross-exchange funding differential features
4. Add to signal_training_features_v1
5. Produce a PIT-correct view for signal joins

Acceptance: Cross-exchange funding collector deployed, data flowing, PIT view created. No trading.

Source: funding_rate_collector.py exists for Hyperliquid" \
  --goal --goal-max-turns 10 --max-runtime 4h --idempotency-key w2-20260704-xfr

# W2.6 — BTC→alt lead-lag feature (lower priority, derivative from candles)
hermes kanban --board $BOARD create "W2.6: Build BTC→altcoin lead-lag spillover feature from candles" \
  --assignee trading-strategy-dev --priority 80 \
  --body "Frank-APPROVED 2026-07-04. Paper-only read-only. SAFETY BOUNDS: No live trading.

Gap: Balcilar 2023 confirms asymmetric BTC→altcoin volatility/return spillover at frequency-dependent lags. We have multi-symbol candles.

Goal:
1. Compute lagged BTC returns at L = {1,3,5,10,20} periods
2. Regress altcoin forward returns on lagged BTC returns
3. Condition on BTC dominance regime and BTC return sign
4. Add BTC_lead_lag features to signal_training_features_v1

Acceptance: BTC_lead_lag features exist in training dataset with documented test plan.

Source: obsidian/quant-team/research/on-chain-cross-asset-lead-lag-edges-2026-06-29.md Priority 3" \
  --goal --goal-max-turns 8 --max-runtime 4h --idempotency-key w2-20260704-bl

echo "=== DONE ==="
