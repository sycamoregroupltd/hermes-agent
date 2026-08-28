# t_dd27733b — real consumers for residual Sycode local-only monitors

Paper/read-only implementation. No live `~/.hermes` mutation, no cron enable,
no allowlist write, no deploy/restart. Isolation paper/live remains FAIL.

## Residual jobs

| Job | Name | Cadence | Consumer | Handoff |
|---|---|---|---|---|
| `45e0b154b41c` | sycode-candle-per-symbol-freshness | `*/30 * * * *` | sycode-trading kanban incident route | `scripts/sycode_candle_per_symbol_freshness_route.sh` → `hermes kanban --board sycode-trading` key `sycode-residual-45e0b154b41c` |
| `965b5d5d4cb4` | PIT context-join validation | `0 7 * * *` | sycode-trading kanban incident route | `scripts/sycode_pit_context_join_route.sh` → same board, key `sycode-residual-965b5d5d4cb4` |
| `53d45f13ff65` | Drift Monitor (hourly, quiet) | every 60m | sycode-trading kanban incident route | `scripts/sycode_drift_monitor_route.sh` → same board, key `sycode-residual-53d45f13ff65` |
| `ea20e2bc47c2` | signal-fusion-fill-rate-check | hourly (paused) | none — SUPERSEDED | Jul-5 acceptance probe; keep paused; do not allowlist |

## Contract

- Healthy ticks: silent (no card).
- Breach: one idempotent kanban card; recurrences comment; 7-day ledger.
- Delivery failure: exit 2 (fail-visible).
- Detector operational error: exit 1 (fail-visible).
- Machine allowlist + CONSUMER-REGISTRY update only after review + cron retarget.

## Tests

```
python3 scripts/sycode_residual_monitor_kanban_router.py --selftest
python3 scripts/sycode_residual_monitor_route.py --selftest
python3 -m unittest scripts.test_sycode_residual_monitor_kanban_router
```
