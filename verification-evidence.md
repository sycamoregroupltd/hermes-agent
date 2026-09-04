# Verification Evidence: Consumer/Alert Route for kanban_classify_failure_and_reaper.sh

**Task**: jarvis-os/t_a45e23da — Propagate isolated stage failures from reaper wrapper
**Prior PR**: https://github.com/sycamoregroupltd/hermes-agent/pull/47 (MERGED at
`c79356d5520fc6a23da85c755eec977a7346d42a` from head
`9353fbc3f754cdbcb134e70b1a0a56a9f4197c04`; Isolation HOLD was draft-only —
this follow-up does not merge)
**Status**: source-only consumer proof rewritten against LIVE jobs.json

## Summary

The wrapper OR-propagation is unchanged and still required:
`diag_rc!=0 OR reaper_rc!=0` exits 1 and emits both stage rc values; `0/0`
exits 0. The round-4 blocker was a false consumer claim. Live
`/home/frank/.hermes/profiles/jarvis/cron/jobs.json` for `fe49f09f4e53` is
`deliver=local`. That is a local execution record only. It is not
`discord:#fleet-reports`. The named Jarvis/Frank alert consumer of a
resulting `last_status=error` is the already-configured cron-health canary
path absorbed into live `guard-bundle-tick-15m`, which feeds
`cron_health_kanban_router.py` (jarvis-os / jarvis-os-pm).

## Evidence chain (read-only, live config)

### 1. Producer — LIVE jobs.json, not the recovery snapshot

Read `/home/frank/.hermes/profiles/jarvis/cron/jobs.json` job `fe49f09f4e53`
(id at line 2528):

- `name`: `kanban-classify-failure-cron`
- `enabled`: true
- `schedule`: interval 30m
- `no_agent`: true
- `script`: `kanban_classify_failure_and_reaper.sh`
- `deliver`: `local`  ← local cron record only
- `failure_streak`: 0 at the time of this read (healthy; no live fire of
  the failure path was performed)

Explicitly **not** used as live deliver:

- `cron-snapshots/profiles/jarvis/cron/jobs.json` still has this id with
  `script: kanban_classify_failure_recent.py` and
  `deliver: discord:#fleet-reports` (snapshot ~line 3090). Stale.
- `/home/frank/.hermes/loop-registry/registry.yaml` row
  `kanban-classify-failure-cron` still says
  `consumer: discord:#fleet-reports`. Registry is not the ticking store.

### 2. Store — local execution record

On nonzero wrapper exit the Hermes no_agent path returns
`success=False` (`scheduler.py` ~5861-5878). `mark_job_run` then writes
`last_status="error"` and `last_error` (`cron/jobs.py` ~3198-3204) and
`finish_execution(..., success=False)` records `executions.db`
`status=failed`. Because `deliver=local`, `_resolve_delivery_targets`
returns `[]` and `delivery_outcome` is `suppressed` — no Discord/Telegram
send.

That local record is what later readers consume. It is not itself a chat
alert.

### 3. Named consumer — existing cron-health kanban router

Already-configured chain (no new cron, no topology edit):

1. Live job `guard-bundle-tick-15m` (`83cf8659dc32`, enabled, every 15m,
   `deliver=local`, script `guard_bundle_tick_15m.sh`). Observed
   `last_run_at` advancing; jarvis `ticker_heartbeat` fresh.
2. Bundle manifest still includes absorbed check `cron-health-canary`
   (`cron_guard_bundle_runner.py` CHECKS, interval 30m) which execs
   profile shim → `/home/frank/.hermes/scripts/cron_health_canary_wrapper.sh`.
3. Wrapper runs `/home/frank/.hermes/profiles/jarvis/scripts/dgx_cron_health_canary.py`.
   For each enabled, unpaused job it does:
   `if status in {"error", "failed"}: bad.append(f"ERROR {prefix}: {reason}")`
   (lines 283-286). It prints a `CRON HEALTH` block when `bad` is nonempty.
4. If stdout is nonempty, the wrapper invokes
   `/home/frank/.hermes/scripts/cron_health_kanban_router.py` with
   `CRON_HEALTH_HEALTHY=0`. Router defaults: `BOARD=jarvis-os`,
   `ASSIGNEE=jarvis-os-pm`. That is the named Jarvis/Frank remediation
   consumer (board card; voice bridge reads boards).

Repeated one-stage or both-stage wrapper failures keep `last_status=error`
across 30m fires, so the canary continues to emit the ERROR line and the
router updates the same signature card. A healed `0/0` run restores
`last_status=ok` and the canary stops listing this job.

Honesty bound (do not over-claim):

- `dgx_cron_health_canary.py` prints findings and still exits 0
  (`main()` has no `sys.exit(1)`). Guard-bundle `report-to-board.py`
  (`RTB_KEY=guard-bundle-15m`) only files when a check's process rc != 0,
  so that RTB card is **not** this job's consumer. The consumer of the
  printed ERROR block is the kanban router inside the canary wrapper.
- Standalone `cron-health-canary` job `082ceadcc6d6` is paused
  (`enabled: false`); the live executor is the 15m guard bundle.

### 4. Orthogonal / non-consumers (documented so they are not re-cited)

- Host crontab line 74:
  `37 * * * * python3 .../hermes_cron_failure_monitor.py >> /tmp/hermes_cron_failure_monitor.log 2>&1`
  Read-only streak printer after five consecutive `executions.db` failures.
  Output is the log file. `cron_liveness_wrapper.sh` drives
  `cron_liveness_monitor.py` (missed occurrence) +
  `cron_liveness_kanban_router.py`; it does not read the failure-monitor
  log. Not a Jarvis/Frank chat route.
- Host crontab `cron_liveness_wrapper.sh` every 10m: missed-occurrence
  class only. An errored-but-recent job is explicitly not a miss
  (`cron_liveness_monitor.py` comments).

## Behavioral matrix (re-run in this isolated clone)

See `verify_wrapper.sh` output recorded in the follow-up commit message /
handoff. Required rows:

```
PASS clean-no-op: diag=0 reaper=0 wrapper=0
PASS diagnostics-only-failure: diag=1 reaper=0 wrapper=1
PASS reaper-only-failure: diag=0 reaper=1 wrapper=1
PASS both-stages-fail: diag=1 reaper=1 wrapper=1
PASS repeated-diagnostics-failure-{1,2,3}
PASS repeated-reaper-failure-{1,2,3}
PASS shell-syntax
```

**Installed wrapper SHA256** (live file, independently hashed; matches the
tracked wrapper in this tree):
`73ba7aa272433f293cf27e9912fe480dbfb8acda487709a9ba08a24c507f1cdd`

## Isolation holds

No live cron invocation of `fe49f09f4e53`, no jobs.json or crontab edit, no
new cron/loop, no vault write, no merge, no credential/deploy/runtime/DB/
trading/board mutation from this verification.
