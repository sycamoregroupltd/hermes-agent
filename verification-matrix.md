# Verification matrix: kanban_classify_failure_and_reaper.sh

Scope: source-only verification of the exact tracked wrapper at
`profiles/jarvis/scripts/kanban_classify_failure_and_reaper.sh`; the installed
copy is `/home/frank/.hermes/profiles/jarvis/scripts/kanban_classify_failure_and_reaper.sh`.
`verify_wrapper.sh` replaces both stage commands with harmless stubs and never
opens the board, cron store, digest, or live reaper.

| diagnostics rc | reaper rc | wrapper rc | required behavior |
|---:|---:|---:|---|
| 0 | 0 | 0 | clean no-op remains successful |
| 1 | 0 | 1 | isolated diagnostics failure reaches cron failure status |
| 0 | 1 | 1 | isolated reaper failure reaches cron failure status |
| 1 | 1 | 1 | both failures remain visible |

The harness also repeats each one-stage failure three times. Every nonzero case
must emit both stage return codes in the `stage failure` stderr marker; this is
the failure evidence retained by the cron execution record.

## Producer, store, consumer, and liveness

- Producer: existing Jarvis cron job `fe49f09f4e53`,
  `kanban-classify-failure-cron`, enabled, `every 30m`, `no_agent=true`,
  script `kanban_classify_failure_and_reaper.sh`.
- Store: Jarvis cron `jobs.json` and its `executions.db` execution record,
  plus the existing board/digest outputs produced by the two stages.
- Consumer/alert route: the job's current `deliver=local` means the local cron
  output/record is retained locally rather than directly sent to a chat. The
  existing read-only `hermes_cron_failure_monitor.py` consumes the execution
  store and raises a failure finding after five consecutive failed executions;
  the existing liveness/router path turns that finding into the named
  Jarvis/Frank remediation route. This change does not alter cron topology or
  delivery settings.
- Liveness argument: with either stage failing repeatedly, the wrapper returns
  nonzero on every 30-minute run, so the producer cannot continue recording
  `last_status=ok`; the failure streak becomes observable to the monitor. A
  both-stage failure follows the same path and includes both return codes.
  A 0/0 run remains a quiet successful no-op.

Safety: no live cron invocation, board mutation, vault write, deploy,
credential change, runtime restart, database mutation, or trading action.
