# drift_baseline — persisted PIT training-time feature distributions

Purpose: give the Sycode drift monitor a **real training-time reference** so a PSI
reading can mean "this feature moved away from the distribution the deployed model
was fitted on" instead of "the newer part of the live window differs from the older
part of the live window" (decision `t_94d36612`, answering Q1 + Q2-ii).

Owner of the *detector* is `fleet-engineer` (`~/.hermes/scripts/drift_monitor.py`).
This directory only **produces and persists the reference**; it does not touch the
monitor's numeric logic.

## Files

- `build_training_baseline.py` — the regeneration script (DB-free self-test included).
- `windows.json` — *declared* PIT training windows. Only add an entry backed by a
  repo artefact that documents the window; everything else stays `inferred`.
- `baselines/<model_version>.json` — one persisted baseline per model version.
- `baselines/MANIFEST.json` — every artifact with its sha256, verdict and counts.
- `baselines/live_vs_baseline_<UTC>.json` — a real live-window-vs-baseline PSI report.

## Regenerate

```bash
cd /home/frank/.hermes/scripts/drift_baseline
python3 build_training_baseline.py --self-test                      # DB-free, 26 checks
python3 build_training_baseline.py --all-deployed --window-map windows.json \
        --max-rows 120000 --out-dir baselines
python3 build_training_baseline.py --rebuild-manifest --out-dir baselines   # re-verify sha256
python3 build_training_baseline.py --model-json <one.json> --check-live 30 \
        --out-dir baselines                                         # live-vs-baseline PSI
```

Sampling is `uniform_hash`: rows are taken in `md5(correlation_id || SAMPLE_SALT)`
order, so a capped sample is uniform over the whole window rather than the earliest
prefix, and re-running reproduces the same reference byte-for-byte.

Reproducibility: an independent re-run of the same command yields an identical
`artifact_hash` and identical `features` / `excluded` / `unresolvable` / `counts`.
Whole-file bytes differ only in `generated_at` — compare `artifact_hash`, and use
`MANIFEST.json` `sha256` for on-disk integrity.

Read-only: the connection sets `default_transaction_read_only=on`. No writes to the
DB, no model promotion, no trading path.

## Artifact schema (`drift-baseline/1`)

```
schema_version        "drift-baseline/1"
model                 {model_type, version, trained_at, metadata_path,
                       metadata_sha256, feature_names_declared}
pit.source            "signal_journeys (postgres, read-only transaction)"
pit.population        mfe_first | pnl | resolved   (+ population_predicate)
pit.window            {since, until, basis, declared_by, exclude_months,
                       until_granularity, trained_at_exact}
pit.immutability_basis  signal_journeys.indicators is an immutable root column
                        (server/src/infra/persistence/signalJourneysImmutableColumns.ts)
                        => signal-time snapshot, safe as a PIT input
pit.rows_used / effective_window / truncated / max_rows_cap
pit.frozen_thresholds  e.g. bb_squeeze_p20 (frozen from the reference window)
coverage_gate          e.g. 0.95   — a key below this is EXCLUDED, never "unmonitored"
features.<name>       {live_projection, n, coverage, mean, std, min, max,
                       quantiles{p01..p99},
                       psi_reference{bins, boundaries, expected_fractions},
                       kind, note}
excluded.<name>       {reason: coverage_below_gate|no_values_in_window, coverage, n}
unresolvable.<name>   {reason: derived_feature_requires_trainer_pipeline |
                       autoregressive_sequence_feature | not_persisted_at_signal_time, ...}
verdict               {state: usable|thin_baseline|no_monitored_features, usable, why}
counts                declared / monitored / excluded / unresolvable  (sums to declared)
artifact_hash         sha256 over the canonical payload (generator-independent of time)
```

## How a consumer computes PSI

```
ref   = art["features"][name]["psi_reference"]
psi   = sum((live_frac_i - ref_frac_i) * ln(live_frac_i / ref_frac_i))
```
where the live side is bucketed on `ref["boundaries"]` (endpoints are ±inf).
Do not re-derive boundaries from a live window.
`live_projection` is the exact signal-time expression for that feature — a live
reader that uses a different path is measuring a different quantity.

## Honesty rules baked in

1. **Fail-closed verdict.** Zero monitored features is `no_monitored_features`,
   never "no drift".
2. **Excluded keys are absent from the denominator** and carry their measured
   coverage, so a coverage-failing key cannot silently read as stable.
3. **Window basis is recorded.** `inferred_trailing_Nd_before_trained_at` is NOT a
   declared window; only `declared` entries in `windows.json` are documented.
4. **Truncation is recorded** (`truncated`, `rows_used`, `effective_window`).
5. **Derived/autoregressive features are named as unresolvable** instead of being
   approximated: re-implementing the trainer's rolling/EWM/lag pipeline from a
   stored window would silently produce a fabricated reference distribution.

## Known limits (measured 2026-09-23, read-only)

- `signal_journeys.indicators` coverage is not uniform over time. Month-by-month,
  sampled 4,000 resolved rows/month (`mfe_first IS NOT NULL`):
  `adx` / `atrPercent` / `ema34Slope` / `emaSpread` are ~100% in 2025-09..11 and
  from 2026-06, but **0.1–2.3% in 2025-12..2026-05**; `confluenceScore` only
  exists from 2026-07. So for models trained in 2026-02..2026-05 the training-time
  reference for those keys is not reconstructible from the current schema
  (the Feb-2026 exporter read `signal_journeys.<column>` fields that no longer exist).
- `ml_feature_store` is empty; there is no other historical feature store.
- `oracle_allowed` / `correlation_adj` (composite-scorer contract) are not
  populated on signal_journeys at all.
- The composite scorer's own training export is not retrievable from this host:
  `training-datasets/composite_scorer_training.jsonl` is only a DVC pointer
  (`...jsonl.dvc`, artifact not on disk), and the MLflow instance on `:5000`
  requires a JWT and does not serve `/api/2.0/mlflow/*` — so its 22-feature
  contract has no derivable reference today (see follow-up card `t_a29cab1e`:
  persist the reference at train time).
- `ml_model_versions` records no training *window* (`dataset_rows` only, and only
  for the retrain path), so every inferred window here is a declared convention,
  not a recovered fact.

## Measured result — live 30d vs persisted training baseline (2026-09-23)

137,409 live rows (population `mfe_first`), pooled PSI per monitored feature:

| model version | trained | monitored | flagged (PSI>=0.25) | fraction |
|---|---|---|---|---|
| `direction_quality_xgb_20260407_085746` | 2026-04-07 | 20 | 4 | 0.200 |
| `mfe_first_cb_20260218_081806` (declared window) | 2026-02-18 | 20 | 9 | 0.450 |
| `drawdown_risk_cb_20260216` | 2026-02-16 | 16 | 7 | 0.438 |

Top movers are `trigger_score`, `macd_histogram`, `macd_cross_strength`,
`trend_strength`, `bollinger_width`. This is a **measurement, not a verdict**:
pooled PSI over a symbol/timeframe mixture also measures composition, so the
consumer must pair it with the per-symbol split (`t_7d1fd0b5` C3) before calling
anything degraded.