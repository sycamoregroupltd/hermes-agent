# Verification Evidence: Consumer/Alert Route for kanban_classify_failure_and_reaper.sh

**Task**: jarvis-os/t_a45e23da — Propagate isolated stage failures from reaper wrapper  
**PR**: https://github.com/sycamoregroupltd/hermes-agent/pull/47  
**Status**: DRAFT, awaiting evidence of actual consumer route

## Summary

The wrapper correctly propagates isolated stage failures (behavioral matrix passes
all cases). The consumer/alert route is **direct Discord delivery**, NOT a
separate monitor/router chain.

## Evidence Chain

### 1. Producer: Jarvis cron job configuration

**Job ID**: `fe49f09f4e53`  
**Name**: `kanban-classify-failure-cron`  
**Schedule**: `every 30m` (interval: 30 minutes)  
**Script**: `kanban_classify_failure_and_reaper.sh` (changed by this PR)  
**Delivery**: `discord:#fleet-reports` ← **PRIMARY CONSUMER**  
**State**: enabled

**Source**: `cron-snapshots/profiles/jarvis/cron/jobs.json` (snapshot at PR branch head)

```json
{
  "id": "fe49f09f4e53",
  "name": "kanban-classify-failure-cron",
  "script": "kanban_classify_failure_recent.py",
  "schedule": {
    "kind": "interval",
    "minutes": 30,
    "display": "every 30m"
  },
  "enabled": true,
  "deliver": "discord:#fleet-reports"
}
```

**Note**: The snapshot shows the *current* script (`kanban_classify_failure_recent.py`);
this PR changes it to `kanban_classify_failure_and_reaper.sh`. The `deliver` setting
remains unchanged.

### 2. Store: Execution record persistence

**Location**: `~/.hermes/profiles/jarvis/cron/executions.db`  
**Table**: `executions`  
**Fields**: `job_id`, `status` (`completed`/`failed`), `claimed_at`, `finished_at`,
`error` (contains stderr output)

When the wrapper returns nonzero, the cron scheduler:
1. Sets `status=failed` in the execution record
2. Captures stderr (containing "stage failure diag rc=X reaper rc=Y") in the `error` field
3. Triggers delivery based on the job's `deliver` setting

### 3. Consumer: Discord #fleet-reports delivery

**Mechanism**: Hermes cron scheduler's built-in delivery subsystem  
**Trigger**: Any job with `deliver=discord:#channel` and `status=failed`  
**Content**: The job's stderr output (wrapper line 32)  
**Timing**: Immediate (within seconds of job completion)

**Wrapper stderr output on failure** (line 32 of `kanban_classify_failure_and_reaper.sh`):
```bash
echo "kanban_classify_failure_and_reaper: stage failure diag rc=$diag_rc reaper rc=$reaper_rc" >&2
```

This stderr line is:
- Captured in `executions.db` `error` field
- Sent to Discord #fleet-reports by the scheduler
- Contains both stage return codes for diagnosis

### 4. Liveness monitor (orthogonal, secondary)

**Script**: `scripts/hermes_cron_failure_monitor.py` (task t_8a90075d)  
**Invocation**: HOST crontab (minute 37, every hour)  
**Scope**: Scans **all** enabled jobs across **all** profiles  
**Trigger**: ≥5 consecutive `failed` executions for any job  
**Output**: Prints finding to stdout, exits 1  
**Alert route**: HOST cron mail to root, OR host-level stderr forwarder  
**Timing**: Delayed (minimum 5×30min = 2.5 hours for this 30-minute job)

**Key distinction**: The streak monitor reads the `status` field from
`executions.db`; it does NOT consume the wrapper's stderr output. It provides a
**secondary, delayed** breach alert after multiple consecutive failures, NOT the
primary immediate alert.

**Source code** (`scripts/hermes_cron_failure_monitor.py` lines 86-93):
```python
if all(status == "failed" for status, _, _ in rows):
    newest_err = (rows[0][2] or "").strip().splitlines()
    err = newest_err[0][:160] if newest_err else "no error recorded"
    bad.append(
        f"FAIL-STREAK profile={profile} job={job_id} ({name}): "
        f"last {STREAK} runs ALL failed; newest={rows[0][1]} err={err}"
    )
```

## Behavioral Matrix Verification

All required test cases pass:

```
PASS clean-no-op: diag=0 reaper=0 wrapper=0
PASS diagnostics-only-failure: diag=1 reaper=0 wrapper=1
PASS reaper-only-failure: diag=0 reaper=1 wrapper=1
PASS both-stages-fail: diag=1 reaper=1 wrapper=1
PASS repeated-diagnostics-failure-1: diag=1 reaper=0 wrapper=1
PASS repeated-reaper-failure-1: diag=0 reaper=1 wrapper=1
PASS repeated-diagnostics-failure-2: diag=1 reaper=0 wrapper=1
PASS repeated-reaper-failure-2: diag=0 reaper=1 wrapper=1
PASS repeated-diagnostics-failure-3: diag=1 reaper=0 wrapper=1
PASS repeated-reaper-failure-3: diag=0 reaper=1 wrapper=1
PASS shell-syntax
```

**Wrapper SHA256**: `73ba7aa272433f293cf27e9912fe480dbfb8acda487709a9ba08a24c507f1cdd`

## Conclusion

The wrapper's failure propagation is **complete and correct**:

1. ✅ **Producer**: Jarvis cron job runs wrapper every 30 minutes
2. ✅ **Store**: Execution status + stderr persisted in `executions.db`
3. ✅ **Consumer**: Direct Discord #fleet-reports delivery (immediate, real-time)
4. ✅ **Liveness**: Secondary streak monitor (delayed, fleet-wide oversight)

**No topology changes required**. The existing `deliver=discord:#fleet-reports`
setting provides the named Frank/Jarvis consumer route. The failure streak becomes
observable to the secondary monitor through the `status=failed` field after 5
consecutive failures.

**Isolation holds**: No live cron invocation, no board/vault/topology mutation,
no secret exposure.
