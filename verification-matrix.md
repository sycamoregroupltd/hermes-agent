# Verification matrix: kanban_classify_failure_and_reaper.sh

Scope: source-only verification of the exact tracked wrapper at
`profiles/jarvis/scripts/kanban_classify_failure_and_reaper.sh`; the installed
copy is `/home/frank/.hermes/profiles/jarvis/scripts/kanban_classify_failure_and_reaper.sh`.
`verify_wrapper.sh` replaces both stage commands with harmless stubs and never
opens the board, cron store, digest, or live reaper.

Live-config rule: the producer/store/consumer chain below is taken from LIVE
`/home/frank/.hermes/profiles/jarvis/cron/jobs.json`, not from
`cron-snapshots/` and not from `/home/frank/.hermes/loop-registry/registry.yaml`.
Those two files currently disagree with live deliver and must not be cited as
the live route.

| diagnostics rc | reaper rc | wrapper rc | required behavior |
|---:|---:|---:|---|
| 0 | 0 | 0 | clean no-op remains successful |
| 1 | 0 | 1 | isolated diagnostics failure reaches cron failure status |
| 0 | 1 | 1 | isolated reaper failure reaches cron failure status |
| 1 | 1 | 1 | both failures remain visible |

The harness also repeats each one-stage failure three times. Every nonzero case
must emit both stage return codes in the `stage failure` stderr marker; this is
the failure evidence retained by the cron execution record.

## Producer, store, consumer, and liveness (LIVE config)

- **Producer**: existing Jarvis cron job `fe49f09f4e53`,
  `kanban-classify-failure-cron`, enabled, `every 30m`, `no_agent=true`,
  script `kanban_classify_failure_and_reaper.sh`. Live `jobs.json` has
  `deliver: "local"`. That is the **local execution record only**. It is
  **not** Discord `#fleet-reports`. The Hermes scheduler
  `_resolve_delivery_targets` returns no chat targets when
  `deliver == "local"`.

- **Store**: Jarvis profile cron `jobs.json` (`last_status`, `last_error`,
  `failure_streak`) plus `executions.db`. A nonzero wrapper exit is recorded
  as `last_status=error` by `cron.jobs.mark_job_run` (`success=False`).

- **Named consumer / alert route (already-configured path, no new cron)**:
  Paused standalone `cron-health-canary` was absorbed into live
  `guard-bundle-tick-15m` (`83cf8659dc32`). That bundle still runs
  `cron_health_canary_wrapper.sh` every 30m of check cadence. The wrapper
  executes `dgx_cron_health_canary.py`, which scans LIVE
  `profiles/*/cron/jobs.json` and emits `ERROR jarvis/<name>: ...` when
  `last_status` is `error` or `failed`. **Non-empty canary stdout is piped
  to existing `cron_health_kanban_router.py` regardless of canary rc**
  (`CRON_HEALTH_HEALTHY=0`). Round-7 amends that already-configured path
  so this job is received without dumping all canary ERRORs:

  1. Canary `select_shown_alerts` reserves a MAX_ALERTS slot for
     `ERROR jarvis/kanban-classify-failure-cron`, so the line is
     consumer-visible and is **not** truncated behind UNPINNED/DRIFT.
  2. Canary **does not** `sys.exit(1)` on findings. Blanket nonzero would
     dump every existing canary ERROR into guard-bundle-tick-15m
     (Isolation HOLD / job-only scope).
  3. Router keys this ERROR as `cronhealth_jarvis_kanban_classify_failure`
     and files/comments a jarvis-os card assigned to `jarvis-os-pm` with
     **only** the named-job lines. That key is **not**
     `cronhealth_current` and is therefore **not** recurrence_suppressed
     against closed `t_a3055cd5` (14d window).

- **Canary exit code (do not contradict)**: the canary Python process
  currently **returns 0** even when it prints ERROR lines. That is
  intentional after round-7. Consumer proof is wrapper stdin routing +
  reserved alert line + dedicated router key, not a blanket canary
  `sys.exit(1)`. Guard-bundle `report-to-board.py`
  (`RTB_KEY=guard-bundle-15m`) is **not** this job's consumer: it only
  files when a bundled check exits nonzero, which this canary does not
  produce.

- **What this is not**:
  - `deliver=local` is not Discord and not Telegram.
  - `cron-snapshots/` and loop-registry `consumer: discord:#fleet-reports`
    are stale. Do not use them as live deliver.
  - Host crontab `hermes_cron_failure_monitor.py` → `/tmp` log only. Not
    consumed by `cron_liveness_wrapper.sh`. Not a Jarvis/Frank chat
    consumer.

- **Liveness argument (source-only; no live cron invocation of fe49f09f4e53)**:
  Producer ticker is live (`profiles/jarvis/cron/ticker_heartbeat` advancing;
  job `last_run_at` advancing on the 30m cadence). Repeated one-stage or
  both-stage wrapper failures keep `last_status=error` on every subsequent
  30m fire. The absorbed canary then keeps emitting
  `ERROR jarvis/kanban-classify-failure-cron` in the reserved slot, and the
  existing kanban router opens or updates the dedicated jarvis-os /
  jarvis-os-pm card until the wrapper returns 0/0 again. A 0/0 run remains
  a quiet successful no-op (`last_status=ok`, no canary ERROR line for this
  job).

Safety: no live cron invocation of this producer, no jobs.json/topology
edit, no board mutation from this verification, no vault write, no deploy,
credential change, runtime restart, database mutation, or trading action.
