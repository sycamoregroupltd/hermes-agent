# Signal Data Foundation Fix Plan — Complete Gap Inventory & Agent Dispatch
**Generated:** 2026-07-04
**Scope:** All gaps blocking model tuning (NeMo, cuFOLIO, cuDF) on signal_journeys data
**Gate:** ALL items must reach CONFIRMED status before any model training begins

## GAP CATALOG — 14 items total

### TIER P0 — Blocks clean training from signal_journeys (6 items)

| # | Gap | Source | Target Agent |
|---|-----|--------|-------------|
| P0.1 | **`composite_confidence_trace` missing 98% of recent rows** — COMPOSITE_SCORER_MODE=shadow but trace isn't persisted. Without this, we have no calibrated P(win) estimate for training | data-persistence-audit I3 | `trading-ml-ensemble` |
| P0.2 | **`execution_advisory` missing 100% of recent rows** — zero advisory payloads in signal_journeys. Can't audit what advisory decisions were made | data-persistence-audit I3 | `trading-ml-ensemble` |
| P0.3 | **`filter_attribution_facts` still has runtime write failures** — silent holes in the decision-reason audit ledger. Fix was reviewed but never deployed | data-persistence-audit I2 + trading-ops doc | `trading-devops` |
| P0.4 | **No canonical frozen training dataset** — every research run regenerates training sets. Can't prove reproducibility. Need `signal_training_features_v1` | world-class-learnings + feature-lineage Gap 6 | `trading-data-oracle` |
| P0.5 | **No feature registry/catalog** — zero documentation of what features exist, how computed, when available. Features computed ad-hoc in raw SQL | feature-lineage Gap 1 + data-persistence-audit | `trading-data-oracle` |
| P0.6 | **Macro data has wrong timestamps** — `macro_context_daily.as_of_ts` is `NOW()` at insert, not publish date. CPI/M2/BalanceSheet joined at wrong lag leaks future info | feature-lineage §2 (HIGHEST LEAKAGE) | `trading-data-oracle` |

### TIER P1 — Degrades feature quality (5 items)

| # | Gap | Source | Target Agent |
|---|-----|--------|-------------|
| P1.1 | **`volume_ratio_at_entry` missing 41% of recent rows** — can't mine volume×confluence patterns | data-persistence-audit I3 | `trading-strategy-dev` |
| P1.2 | **Pro-trader DB persistence stale since May 1** — `pro_trader_profiles` (45 rows, all 1000 elo), `pro_trader_positions` stale. Any pro-trader flow feature is dead | data-persistence-audit I1 | `trading-data-oracle` |
| P1.3 | **126 symbols with zero candle coverage** — signal_journeys has 589 distinct symbols, candles has 475. 126 symbols have no price path for synthetic labels | data-persistence-audit I6 | `trading-data-oracle` |
| P1.4 | **No per-field PIT validation cron** — feature-lineage map documented exact PIT join patterns but no cron enforces them. One missing `WHERE ts <= triggered_at` = silent look-ahead | feature-lineage Gap 2 | `trading-devops` |
| P1.5 | **No freshness SLA monitors** — no alerts if funding_rate_history, OI, or macro_context_daily pipelines go stale | feature-lineage Gap 4 | `trading-devops` |

### TIER P2 — Infrastructure hygiene (3 items)

| # | Gap | Source | Target Agent |
|---|-----|--------|-------------|
| P2.1 | **Sidecar SQLite stores not cataloged** — 4 verified sidecars (orthogonal-capture, catalyst, x-capture, wallet forward-test) exist with no Postgres home or catalog entry | data-persistence-audit I9 | `trading-data-oracle` |
| P2.2 | **Causal ordering PIT checks not cronned** — research recommends child-before-root, orphan, mutable-overwrite checks run periodically | world-class-learnings #4 | `trading-devops` |
| P2.3 | **`signal_journey_events` FK still CASCADE** — research flagged as event-sourcing anti-pattern. Delete journey = delete audit trail | world-class-learnings #1 | `platform-db-migrator` |

## DISPATCH SEQUENCE

Wave 1 (P0 — Blockers):
  → trading-ml-ensemble: Fix composite trace + advisory persistence telemetry (P0.1 + P0.2)
  → trading-devops: Fix filter_attribution_facts write failures in runtime (P0.3)
  → trading-data-oracle: Canonical frozen training dataset + feature registry (P0.4 + P0.5)
  → trading-data-oracle: Fix macro_context_daily PIT timestamps (P0.6)

Wave 2 (P1 — Quality):
  → trading-strategy-dev: Fix volume_ratio_at_entry persistence gap (P1.1)
  → trading-data-oracle: Diagnose & fix pro-trader persistence (P1.2)
  → trading-data-oracle: Build canonical instrument universe bridge (P1.3)
  → trading-devops: Deploy PIT validation cron + freshness SLA monitors (P1.4 + P1.5)

Wave 3 (P2 — Hygiene):
  → trading-data-oracle: Catalog sidecar stores (P2.1)
  → trading-devops: Deploy causal-ordering PIT cron (P2.2)
  → platform-db-migrator: Fix signal_journey_events FK CASCADE (P2.3)

## VERIFICATION GATE

Every item must produce a verifiable artifact:
- P0.x → SQL/patch applied + monitor showing non-null rows in next 24h
- P1.x → Diagnostic report + fix deployed + monitor showing improvement
- P2.x → Runbook/DDL/gate-merging evidence

Kanban route: all cards on `sycode-trading` board, assignee per target agent,
with `data-foundation-blocker` tag. PM `sycode-trading-pm` to triage progress.
