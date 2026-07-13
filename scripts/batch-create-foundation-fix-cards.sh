#!/bin/bash
# Batch-create data foundation fix cards on sycode-trading board
# Generated 2026-07-04

BOARD="sycode-trading"
TAG="data-foundation-blocker"

echo "=== Wave 1 — P0 ==="

# P0.1
hermes kanban --board $BOARD create "P0.1: Fix composite_confidence_trace persistence" \
  --assignee trading-ml-ensemble --priority 99 \
  --body "Frank-APPROVED 2026-07-04. SAFETY BOUNDS: Paper-only read-only. Gap: composite_confidence_trace NULL in 98pct of recent signal_journeys despite COMPOSITE_SCORER_MODE=shadow. Goal: Trace CompositeSignalScorer output through JourneyUpdateParams to persistence. Fix write path. Acceptance: 24h after fix non-null in >90pct new rows. Source: data-persistence-audit.md I3." \
  --goal --goal-max-turns 15 --max-runtime 4h --idempotency-key data-fdn-20260704-p01

# P0.2
hermes kanban --board $BOARD create "P0.2: Fix execution_advisory persistence" \
  --assignee trading-ml-ensemble --priority 99 \
  --body "Frank-APPROVED 2026-07-04. SAFETY BOUNDS: Paper-only read-only. Gap: execution_advisory NULL in 100pct recent rows. Goal: Trace advisory output from validator to persistence. Fix write path. Acceptance: 24h after fix non-null in >90pct new rows. Source: data-persistence-audit.md I3." \
  --goal --goal-max-turns 15 --max-runtime 4h --idempotency-key data-fdn-20260704-p02

# P0.3
hermes kanban --board $BOARD create "P0.3: Fix filter_attribution_facts write failures" \
  --assignee trading-devops --priority 99 \
  --body "Frank-APPROVED 2026-07-04. SAFETY BOUNDS: Paper-only deploy. Gap: Non-blocking insert failures for blank numeric values. Fix was reviewed but never deployed. Goal: Verify deploy status deploy fix add regression test. Acceptance: 24h after deploy zero write failures in logs. Source: data-persistence-audit.md I2." \
  --goal --goal-max-turns 10 --max-runtime 4h --idempotency-key data-fdn-20260704-p03

# P0.4
hermes kanban --board $BOARD create "P0.4: Build canonical frozen training dataset signal_training_features_v1" \
  --assignee trading-data-oracle --priority 98 \
  --body "Frank-APPROVED 2026-07-04. Paper-only read-only DB. No backfill. Gap: Every research run regenerates own training set. Goal: Create signal_training_features_v1 view with strict PIT joins feature columns (conviction_score mfe_mae_ratio regime bars_held hour_utc trajectory_label) and label columns (clean_outcome_binary_24h clean_pnl_net_24h). Acceptance: view exists PIT correct. Source: world-class-learnings 6." \
  --goal --goal-max-turns 20 --max-runtime 4h --idempotency-key data-fdn-20260704-p04

# P0.5
hermes kanban --board $BOARD create "P0.5: Build signal_feature_registry catalog" \
  --assignee trading-data-oracle --priority 98 \
  --body "Frank-APPROVED 2026-07-04. Paper-only documentation. Gap: Zero documentation of computed features. Goal: YAML catalog of every computed feature with source PIT policy SLA availability date. Acceptance: catalog in obsidian. Source: feature-lineage Gap 1." \
  --goal --goal-max-turns 10 --max-runtime 4h --idempotency-key data-fdn-20260704-p05

# P0.6
hermes kanban --board $BOARD create "P0.6: Fix macro_context_daily PIT timestamps" \
  --assignee trading-data-oracle --priority 97 \
  --body "Frank-APPROVED 2026-07-04. Paper-only queries no migration. Gap: macro_context_daily timestamps are NOW() not publish date. CPI M2 BalanceSheet join leakage. Goal: published_at view with per-field lag offsets. Source: feature-lineage section 2 highest leakage." \
  --goal --goal-max-turns 15 --max-runtime 4h --idempotency-key data-fdn-20260704-p06

echo "=== Wave 2 — P1 ==="

# P1.1
hermes kanban --board $BOARD create "P1.1: Fix volume_ratio_at_entry persistence 41pct missing" \
  --assignee trading-strategy-dev --priority 90 \
  --body "Frank-APPROVED 2026-07-04. Paper-only. Gap: volume_ratio_at_entry NULL in 41pct recent rows. Goal: Fix write path. Acceptance: >90pct fill rate. Source: data-persistence-audit.md I3." \
  --goal --goal-max-turns 10 --max-runtime 4h --idempotency-key data-fdn-20260704-p11

# P1.2
hermes kanban --board $BOARD create "P1.2: Diagnose pro-trader DB persistence stale since May 1" \
  --assignee trading-data-oracle --priority 90 \
  --body "Frank-APPROVED 2026-07-04. Read-only diagnostics. Gap: pro_trader_profiles and pro_trader_positions stale since May. Goal: Diagnose root cause produce fix plan. Do NOT re-enable without review. Source: data-persistence-audit.md I1." \
  --goal --goal-max-turns 10 --max-runtime 4h --idempotency-key data-fdn-20260704-p12

# P1.3
hermes kanban --board $BOARD create "P1.3: Build canonical instrument universe 126 symbols no candle data" \
  --assignee trading-data-oracle --priority 90 \
  --body "Frank-APPROVED 2026-07-04. Read-only. Gap: 126 signal symbols zero candle coverage. Goal: Bridge table of coverage gaps. Source: data-persistence-audit.md I6." \
  --goal --goal-max-turns 10 --max-runtime 4h --idempotency-key data-fdn-20260704-p13

# P1.4
hermes kanban --board $BOARD create "P1.4: Deploy PIT validation cron" \
  --assignee trading-devops --priority 88 \
  --body "Frank-APPROVED 2026-07-04. Monitoring-only. Gap: No PIT join enforcement. Cycle 07 found 1.8M stale labels. Goal: Cron checking context joins for PIT correctness. Source: feature-lineage Gap 2." \
  --goal --goal-max-turns 8 --max-runtime 4h --idempotency-key data-fdn-20260704-p14

# P1.5
hermes kanban --board $BOARD create "P1.5: Deploy freshness SLA monitors funding OI macro liq orderbook" \
  --assignee trading-devops --priority 88 \
  --body "Frank-APPROVED 2026-07-04. Monitoring-only. Gap: No stale-pipeline alerts. Goal: 5 monitors with SLA thresholds. Source: feature-lineage Gap 4." \
  --goal --goal-max-turns 8 --max-runtime 4h --idempotency-key data-fdn-20260704-p15

echo "=== Wave 3 — P2 ==="

# P2.1
hermes kanban --board $BOARD create "P2.1: Catalog sidecar SQLite stores" \
  --assignee trading-data-oracle --priority 75 \
  --body "Frank-APPROVED 2026-07-04. Gap: 4 sidecar SQLite stores undocumented. Goal: Catalog all with schema freshness consumer. Source: data-persistence-audit.md I9." \
  --goal --goal-max-turns 5 --max-runtime 2h --idempotency-key data-fdn-20260704-p21

# P2.2
hermes kanban --board $BOARD create "P2.2: Deploy causal-ordering PIT checks cron" \
  --assignee trading-devops --priority 75 \
  --body "Frank-APPROVED 2026-07-04. Monitoring-only. Gap: No causal-ordering checks child-before-root orphan. Goal: Deploy cron from trigger-lineage audit. Source: world-class-learnings 4." \
  --goal --goal-max-turns 5 --max-runtime 2h --idempotency-key data-fdn-20260704-p22

# P2.3
hermes kanban --board $BOARD create "P2.3: Fix signal_journey_events FK CASCADE to NO ACTION" \
  --assignee platform-db-migrator --priority 75 \
  --body "Frank-APPROVED 2026-07-04. Paper-only DDL reviewed path. Gap: FK CASCADE erases audit trail on signal delete. Goal: DDL plus rollback gated through trading-risk-reviewer. Source: world-class-learnings 1." \
  --goal --goal-max-turns 8 --max-runtime 4h --idempotency-key data-fdn-20260704-p23

echo "=== DONE ==="
