# Verification Evidence: job-only named consumer for kanban-classify-failure-cron

**Task**: jarvis-os/t_ed054723 — NEEDS-FIX r7 named-consumer proof
**Parent**: jarvis-os/t_a45e23da (wrapper OR-propagation already landed)
**Isolation HOLD**: draft PR only; no merge; no live `~/.hermes` mutate;
no new cron/loop; no credentials/deploy.

## Round-7 blockers addressed

1. **P1 — Do not treat blanket canary `sys.exit(1)` as job-only proof.**
   Round-6 `sys.exit(1)` on any `if bad` dumps every existing canary ERROR
   into guard-bundle-tick-15m. Live MAX_ALERTS=25 already truncates; this
   job's ERROR sorts after UNPINNED/DRIFT and would be `… N more`.
   **Fix:** revert that as the consumer proof. Canary still prints ERROR
   lines and **exits 0**. Wrapper already routes non-empty stdout to
   `cron_health_kanban_router.py` regardless of canary rc. Canary
   `select_shown_alerts` reserves a MAX_ALERTS slot so
   `ERROR jarvis/kanban-classify-failure-cron` is always in
   consumer-visible output.

2. **P1 — Router constant `cronhealth_current` recurrence-suppresses
   against closed t_a3055cd5 (14d window).** Even a printed ERROR does not
   create a new jarvis-os-pm card today.
   **Fix:** amend the already-configured router (no new cron) so this
   job's ERROR uses dedicated key `cronhealth_jarvis_kanban_classify_failure`
   and a job-only alert body. That key is not the closed fleet card.

3. **P2 — verification-matrix.md contradiction.** Matrix must not claim
   the canary currently returns 0 while also treating `sys.exit(1)` as
   the consumer, or claim exit 0 after a PR that exits 1. Round-7 matrix
   states the canary **returns 0** when it prints ERROR lines, and that
   this is intentional.

## Producer / store / deliver (LIVE, read-only)

Live job `fe49f09f4e53` (`kanban-classify-failure-cron`) in
`/home/frank/.hermes/profiles/jarvis/cron/jobs.json`:
- `enabled: true`, `every 30m`, `no_agent: true`
- `script: kanban_classify_failure_and_reaper.sh`
- `deliver: local` — **local execution record only**, not Discord

Exact executed wrapper SHA256 (must remain):
`73ba7aa272433f293cf27e9912fe480dbfb8acda487709a9ba08a24c507f1cdd`

## Named consumer (already-configured path)

```
fe49f09f4e53 last_status=error
  -> dgx_cron_health_canary.py prints ERROR jarvis/kanban-classify-failure-cron
     (reserved in shown[:MAX_ALERTS]; canary rc=0)
  -> cron_health_canary_wrapper.sh pipes non-empty stdout to
     cron_health_kanban_router.py (CRON_HEALTH_HEALTHY=0)
  -> router key cronhealth_jarvis_kanban_classify_failure
  -> jarvis-os card assigned jarvis-os-pm (job-only body)
```

Non-consumers (unchanged):
- `hermes_cron_failure_monitor.py` crontab → `/tmp` log
- `cron_liveness_wrapper.sh` missed-occurrence only
- loop-registry / cron-snapshots discord deliver (stale)
- guard-bundle RTB card (requires bundled check rc != 0)

## Behavioral matrix (unchanged wrapper)

| diag rc | reaper rc | wrapper rc | behavior |
|---------|-----------|------------|----------|
| 0       | 0         | 0          | Clean no-op |
| 1       | 0         | 1          | Isolated diag failure |
| 0       | 1         | 1          | Isolated reaper failure |
| 1       | 1         | 1          | Both failures visible |

Re-run `bash verify_wrapper.sh` on this branch. Wrapper file is not
modified this round.

## Splice-not-replace (t_b2d79d73)

Do **not** wholesale-replace the live executed router with the PR 61
repo copy. That copy still used content-hash `cronhealth_<md5>` for
non-named issues and would re-ratchet cards.

This packet copies the LIVE constant-key router (`return
"cronhealth_current"`, `ACTIVE_STATUSES` includes `blocked`, sqlite
`mode=ro` without `immutable=1`) and splices only:

- `NAMED_JOB_KEY = cronhealth_jarvis_kanban_classify_failure`
- `named_job_issues` / `named_job_alert_text`
- `derive_key` exception for the named ERROR
- `process_tick` job-only body when that ERROR is present

Fleet noise without the named job still keys `cronhealth_current`.
Unittest `test_fleet_noise_without_named_job_does_not_use_named_key`
now asserts that constant, not a hash prefix.

Executed canary after Frank-GO:
`profiles/jarvis/scripts/dgx_cron_health_canary.py` (paused_at skip
kept). `scripts/dgx_cron_health_canary.py` reconverged with the same
paused_at skip so a scripts-path land cannot drop it.

## Isolation Holds

- No live cron invocation of `fe49f09f4e53`
- No jobs.json or crontab edit
- No new cron/loop
- No live `~/.hermes` mutate
- No vault write
- No merge
- No credential/deploy/runtime/DB/trading/board mutation
