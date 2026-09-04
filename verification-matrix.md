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

- **Producer**: existing Jarvis cron job `fe49f09f4e53`,
  `kanban-classify-failure-cron`, enabled, `every 30m`, `no_agent=true`,
  script `kanban_classify_failure_and_reaper.sh` (changed by this PR from
  `kanban_classify_failure_recent.py`).

- **Store**: Jarvis cron `jobs.json` and its `executions.db` execution record
  (status, claimed_at, finished_at, error field containing stderr), plus the
  existing board/digest outputs produced by the two stages.

- **Consumer/alert route**: The job's `deliver=discord:#fleet-reports` means
  cron scheduler sends the job's stderr to Discord #fleet-reports on every
  failed execution. The wrapper's line 32 emits `stage failure diag rc=X reaper
  rc=Y` to stderr when either stage returns nonzero, so that failure evidence
  reaches the Discord channel directly. A 0/0 clean run emits no stderr and
  sends no message. This is the **primary, immediate** alert path.

- **Liveness / streak monitor (orthogonal)**: The existing read-only
  `hermes_cron_failure_monitor.py` (task t_8a90075d, HOST crontab minute 37
  hourly) scans `executions.db` across all profiles and prints a finding to
  stdout when ANY enabled job accumulates ≥5 consecutive failed executions. It
  does NOT consume this specific job's stderr output; it reads the durable
  status field. HOST cron mails root on nonzero exit, or stderr is captured by
  a host-level alert forwarder. This is the **secondary, delayed** (5×30min =
  2.5h minimum) streak-breach detector, not the primary consumer of this
  wrapper's failure propagation.

- **Liveness argument**: With either stage failing repeatedly, the wrapper
  returns nonzero on every 30-minute run. Each failure:
  1. Immediately alerts via Discord #fleet-reports (primary, real-time)
  2. Records `status=failed` in `executions.db`
  3. After 5 consecutive failures, triggers the hourly streak monitor
     (secondary, delayed)

  A both-stage failure follows the same paths and includes both return codes in
  the Discord alert text. A 0/0 run remains a quiet successful no-op (no alert,
  `status=completed`).

Safety: no live cron invocation, board mutation, vault write, deploy,
credential change, runtime restart, database mutation, or trading action.
