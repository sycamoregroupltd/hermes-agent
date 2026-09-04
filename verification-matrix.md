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
  script `kanban_classify_failure_and_reaper.sh`. Live `jobs.json` (id at
  line 2528) has `deliver: "local"` (line 2524). That is the **local
  execution record only**. It is **not** Discord `#fleet-reports`. The
  Hermes scheduler `_resolve_delivery_targets` returns no chat targets when
  `deliver == "local"` (`hermes-agent/cron/scheduler.py`).

- **Store**: Jarvis profile cron `jobs.json` (`last_status`, `last_error`,
  `failure_streak`) plus `executions.db` (status `completed`/`failed`,
  claimed_at, error/stderr). A nonzero wrapper exit is recorded as
  `last_status=error` by `cron.jobs.mark_job_run` (`success=False`).

- **Named consumer / alert route (live, already configured, no new cron)**:
  The paused standalone job `cron-health-canary` (`082ceadcc6d6`) was
  absorbed into live `guard-bundle-tick-15m` (`83cf8659dc32`, enabled,
  every 15m, `deliver=local`). That bundle still runs
  `cron_health_canary_wrapper.sh` every 30m of check cadence. The wrapper
  executes `dgx_cron_health_canary.py`, which scans LIVE
  `profiles/*/cron/jobs.json` and emits `ERROR jarvis/<name>: ...` when
  `last_status` is `error` or `failed` (canary lines 283-286). Non-empty
  canary stdout is piped to existing `cron_health_kanban_router.py`
  (`CRON_HEALTH_HEALTHY=0`), which files/comments a jarvis-os card assigned
  to `jarvis-os-pm` (board/assignee defaults in the router). That board card
  is the named Jarvis/Frank remediation pipe (voice/board), independent of
  this job's `deliver=local`. One isolated stage failure is enough: the
  canary keys off current `last_status`, not a five-run streak.

- **What this is not**:
  - `deliver=local` is not Discord and not Telegram. Scheduler delivery is
    suppressed (`delivery_outcome=suppressed`).
  - `cron-snapshots/profiles/jarvis/cron/jobs.json` still shows this job as
    `script=kanban_classify_failure_recent.py` and
    `deliver=discord:#fleet-reports`. That snapshot is stale. Do not use it.
  - Loop-registry row `kanban-classify-failure-cron` still lists
    `consumer: discord:#fleet-reports`. That is not live-config proof.
  - Host crontab line 74 runs `hermes_cron_failure_monitor.py` and appends
    stdout to `/tmp/hermes_cron_failure_monitor.log`. That script only prints
    and exits 1 after five consecutive `executions.db` failures. It is not
    consumed by `cron_liveness_wrapper.sh` / `cron_liveness_kanban_router.py`
    (those are missed-occurrence only). It is **not** a Jarvis/Frank chat
    consumer.
  - Guard-bundle `report-to-board.py` (`RTB_KEY=guard-bundle-15m`) files a
    card only when a bundled check exits nonzero. The canary Python process
    currently returns 0 even when it prints ERROR lines, so the 15m RTB card
    is **not** claimed as this job's consumer. The consumer that actually
    receives non-empty canary stdout is `cron_health_kanban_router.py`.

- **Liveness argument (source-only; no live cron invocation of fe49f09f4e53)**:
  Producer ticker is live (`profiles/jarvis/cron/ticker_heartbeat` advancing;
  job `last_run_at` advancing on the 30m cadence). Repeated one-stage or
  both-stage wrapper failures keep `last_status=error` on every subsequent
  30m fire. The absorbed 15m/30m canary then keeps emitting `ERROR
  jarvis/kanban-classify-failure-cron` and the existing kanban router keeps
  the jarvis-os / jarvis-os-pm card current until the wrapper returns 0/0
  again. A 0/0 run remains a quiet successful no-op (`last_status=ok`, no
  canary ERROR line for this job).

Safety: no live cron invocation of this producer, no jobs.json/topology
edit, no board mutation from this verification, no vault write, no deploy,
credential change, runtime restart, database mutation, or trading action.
