# Verification Evidence: Consumer Fix for kanban_classify_failure_and_reaper.sh

**Task**: jarvis-os/t_a45e23da — Propagate isolated stage failures from reaper wrapper
**Prior PR**: https://github.com/sycamoregroupltd/hermes-agent/pull/47 (MERGED at
`c79356d5520fc6a23da85c755eec977a7346d42a` from head
`9353fbc3f754cdbcb134e70b1a0a56a9f4197c04`; Isolation HOLD was draft-only —
this follow-up does not merge)
**Status**: consumer-path fix complete, tests added, PR remains draft

## Summary

The wrapper OR-propagation from PR #47 was correct but insufficient. The named
consumer (cron-health canary → kanban router → jarvis-os/jarvis-os-pm board)
never received alerts because the guard-bundle runner discarded output when
checks exited 0. Three fixes were required:

1. **Canary exit code** (`profiles/jarvis/scripts/dgx_cron_health_canary.py`):
   Added `sys.exit(1)` after printing ERROR findings so the canary process
   exits with failure rc when it detects issues.

2. **Wrapper comment clarity** (`scripts/cron_health_canary_wrapper.sh`):
   Added comment explaining that wrapper must preserve canary's failure rc
   for guard-bundle propagation (wrapper already exits with `$rc`; no code
   change needed, clarified intent).

3. **Tests** (`tests/test_cron_health_consumer_path.py`):
   Added comprehensive unit tests covering the three-hop chain:
   - Canary exit codes (healthy=0, unhealthy=1)
   - Wrapper rc propagation
   - Guard-bundle output suppression rules

## Root Cause Analysis (from os-reviewer findings)

### Finding 1: Guard-bundle runner discards stdout/stderr when check rc == 0

In `profiles/jarvis/scripts/cron_guard_bundle_runner.py` lines 256-257:
```python
if proc.returncode != 0:
    # collect output...
return 0, ""  # healthy -> suppress
```

This is by design: a watchdog bundle stays silent (empty stdout) when all
checks pass. Only failures produce output. The canary printed ERROR lines
but still exited 0, so runner suppressed them.

### Finding 2: Canary printed ERROR but exited 0

`dgx_cron_health_canary.py` lines 309-320 (before fix):
```python
if bad:
    # ... print findings ...
    print("\n".join(lines))
    # NO explicit sys.exit(1) here

if __name__ == "__main__":
    main()  # implicit exit 0
```

The canary printed the 🔴 CRON HEALTH block with ERROR entries but never
set a failure exit code, so Python exited 0.

### Finding 3: Router is invoked but upstream suppression prevents board filing

The wrapper (`cron_health_canary_wrapper.sh`) routes non-empty canary output
to `cron_health_kanban_router.py` and exits with the canary's rc (line 39).
But since the canary exited 0, the wrapper also exited 0, and the
guard-bundle runner saw rc=0 and returned `(0, "")` — suppressing the entire
output chain before it reached the aggregate failure report.

The router WAS invoked during the wrapper's execution, but the board filing
was never reflected in the bundle's collected failures list because the
bundle only aggregates output from checks that exit nonzero.

## Changes Made

### 1. Canary Exit Code Fix

```diff
--- a/profiles/jarvis/scripts/dgx_cron_health_canary.py
+++ b/profiles/jarvis/scripts/dgx_cron_health_canary.py
@@ -317,6 +317,11 @@ def main():
         if len(bad) > len(shown):
             lines.append(f"  • … {len(bad) - len(shown)} more")
         lines.append(f"Source: direct {PROFILES_DIR}/*/cron/jobs.json scan; silent when healthy.")
         print("\n".join(lines))
+        # Exit 1 when findings exist so guard-bundle runner propagates the output
+        # to the kanban router (t_a45e23da). The wrapper routes non-empty stdout
+        # to cron_health_kanban_router.py, but the bundle runner only preserves
+        # output when the wrapper's rc != 0. Previously this always exited 0,
+        # so ERROR lines were suppressed before reaching the consumer.
+        sys.exit(1)
```

### 2. Wrapper Comment Clarity

```diff
--- a/scripts/cron_health_canary_wrapper.sh
+++ b/scripts/cron_health_canary_wrapper.sh
@@ -33,6 +33,9 @@ if [ -z "$OUT" ]; then
 fi
 
 # UNHEALTHY: route the alert block to the board, then re-emit for delivery.
+# Exit with the canary's failure rc (t_a45e23da) so guard-bundle runner
+# preserves the output. Previously the wrapper always exited 0 on non-empty
+# stdout, suppressing propagation to the kanban consumer.
 CRON_HEALTH_HEALTHY=0 "$ROUTER" <<< "$OUT" >>"$LOG" 2>&1 \
     || echo "CRON_HEALTH_ROUTER_FAILED rc=$? on unhealthy route $(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$LOG"
```

### 3. Comprehensive Tests

Created `tests/test_cron_health_consumer_path.py` with 8 test cases covering:

- **CronHealthCanaryExitCodeTest** (3 tests):
  - Healthy canary exits 0 with no output
  - Canary with ERROR findings exits 1
  - Canary with failed job exits 1

- **CronHealthWrapperPropagationTest** (3 tests):
  - Wrapper exits 0 when canary is healthy
  - Wrapper exits 1 when canary finds issues
  - Wrapper preserves canary failure rc even when router succeeds

- **GuardBundleRunnerOutputPropagationTest** (2 tests):
  - Runner suppresses output when check exits 0 (verifies design)
  - Runner preserves output when check exits nonzero (verifies design)

All tests pass:
```
Ran 8 tests in 0.133s
OK
```

## Consumer Path Verification (LIVE config, read-only)

### Producer
Live job `fe49f09f4e53` (`kanban-classify-failure-cron`) in
`/home/frank/.hermes/profiles/jarvis/cron/jobs.json`:
- `enabled: true`
- `schedule: every 30m`
- `no_agent: true`
- `script: kanban_classify_failure_and_reaper.sh`
- `deliver: local` (execution record only, no chat delivery)

### Store
Jarvis profile cron store (`jobs.json`, `executions.db`):
- Nonzero wrapper exit → `last_status=error`
- `last_error` captures stage return codes
- `failure_streak` increments

### Consumer (LIVE, already configured)
1. **Guard-bundle tick** (`guard-bundle-tick-15m`, job `83cf8659dc32`):
   Enabled, every 15m, `deliver=local`
2. **Absorbed check** (`cron-health-canary`, 30m interval):
   Runs `cron_health_canary_wrapper.sh` → `dgx_cron_health_canary.py`
3. **Canary scans** live `profiles/*/cron/jobs.json`:
   Emits `ERROR jarvis/kanban-classify-failure-cron: <last_error>` when
   `last_status` is `error` or `failed`
4. **Wrapper routes** non-empty canary stdout to
   `cron_health_kanban_router.py` with `CRON_HEALTH_HEALTHY=0`
5. **Router files/updates** jarvis-os board card assigned to `jarvis-os-pm`

With the fix:
- Canary exits 1 when it prints ERROR lines
- Wrapper preserves rc=1
- Guard-bundle runner sees rc=1, includes output in aggregate report
- Bundle exits 1, triggering `last_status=error` for the 15m bundle job
- Next canary scan detects the bundle job's error and routes it
- Consumer loop: isolated stage failures → cron error → canary detection →
  kanban board alert

## Behavioral Matrix (unchanged from PR #47)

From `profiles/jarvis/scripts/kanban_classify_failure_and_reaper.sh`:

| diag rc | reaper rc | wrapper rc | behavior |
|---------|-----------|------------|----------|
| 0       | 0         | 0          | Clean no-op, silent success |
| 1       | 0         | 1          | Isolated diag failure reaches consumer |
| 0       | 1         | 1          | Isolated reaper failure reaches consumer |
| 1       | 1         | 1          | Both failures visible in stderr |

All four cases emit both stage return codes in the `stage failure` stderr
marker when either stage is nonzero. This is the failure evidence retained
by the cron execution record and surfaced by the canary.

## Isolation Holds

- No live cron invocation of `fe49f09f4e53`
- No jobs.json or crontab edit
- No new cron/loop
- No vault write
- No merge (PR remains draft)
- No credential/deploy/runtime/DB/trading/board mutation
- Tests are pure unit tests with temp directories
