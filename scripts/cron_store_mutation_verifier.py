#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies.
"""cron-store mutation verifier: post-mutation store re-read assertion.

Closes the CRON-STORE WRITE/PERSISTENCE DRIFT class found in t_d450cf24, where
a reviewed `hermes cron pause` DID persist but was silently reverted ~3.5h later
by `git checkout -- profiles/<p>/cron/jobs.json` run from an unrelated task.

Two modes:

  --assert-state   Re-read the live store NOW and assert a job's expected
                   enabled/state/paused_at. This is the invariant a worker MUST
                   run after any cron-store mutation, and again before it calls
                   kanban_complete. Exit 1 on mismatch.

  --assert-disabled  Re-read the live store NOW and sweep every job currently
                   marked enabled=false/state=paused, confirming (a) paused_at
                   is still set (durable) and (b) no run was CLAIMED (scheduler
                   dispatch) after paused_at plus one claim window (≈ the job's
                   scheduler interval) — i.e. the paused job has NOT been
                   silently re-scheduled and run again after its pause. The
                   regression test is on the run's claimed_at (from the parallel
                   executions.db ledger), NOT on last_run_at (which is the run's
                   FINISH time): a run that was claimed before paused_at and only
                   finished after it (in-flight at pause time) is benign and is
                   NOT flagged. This is the scheduled post-ticker re-verification
                   gate (t_fcb6141f / t_24a685ed): a reviewed scheduler disable
                   is only GREEN once a ticker cycle has passed and this sweep
                   still reports it paused with no post-pause claim. Silent +
                   exit 0 when healthy; print divergences + exit 1 on any
                   regression. A missing executions ledger degrades to a WARN
                   (no post-pause claims verifiable) rather than a false alert.

  --selftest       Build a throwaway store + executions ledger with one
                   in-flight-at-pause job (claimed before pause, finished after)
                   and one truly regressed job (claimed a full interval after
                   pause); assert the former is NOT flagged and the latter IS.
                   Exit 0 on correct classification, 1 otherwise.

  --watch          Compare the live store against the git-tracked copy of the
                   SAME file and report jobs whose enabled/state differ. A
                   difference here means the live store and the VC snapshot
                   disagree about schedule state — the exact condition under
                   which a `git checkout --` of this path silently reverts a
                   reviewed pause. Exit 1 when any enabled/state divergence
                   exists. NOTE: this mode is git-path-aware — an UNTRACKED
                   store (gitignored / skip-worktree-free, as all live cron
                   stores now are since commit 8630119) is SKIP + exit 0, so it
                   never false-alarms as a "revert-pending RISK" on a store that
                   is structurally protected from git clobber by not being
                   tracked at all.

Read-only. Never mutates a cron store, schedule, provider, or credential.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

STATE_KEYS = ("enabled", "state", "paused_at")
# A reviewed pause is "re-scheduled" only when a run lands a real ticker cycle
# (or more) AFTER paused_at. Sub-second drift (pause recorded microseconds after
# the final in-flight run) is benign; a 90s grace cleanly separates it from the
# dangerous "job ran again a full cycle after pause" class.
DEFAULT_TICKER_GRACE_SECONDS = 90
# When a paused job's scheduler interval cannot be derived from its schedule
# (non-interval / unrecognised cron expression), fall back to this post-pause
# claim window. A run must be CLAIMED (scheduler dispatch) more than this far
# after paused_at to count as a regression; a run claimed before pause and only
# FINISHING after it (in-flight at pause time) is benign.
DEFAULT_CLAIM_WINDOW_SECONDS = 900.0


def _estimate_interval_seconds(schedule: dict | None, default: float) -> float:
    """Best-effort scheduler interval (seconds) for a job's schedule dict.

    The regression window for a paused job is one full scheduler interval: a run
    claimed within that window of the pause is the benign boundary/in-flight
    race, while a claim more than a full cycle later is a genuine re-schedule.
    """
    sched = schedule or {}
    if sched.get("kind") == "interval" and sched.get("minutes"):
        return float(sched["minutes"]) * 60
    expr = (sched.get("expr") or sched.get("display") or "").strip()
    if not expr:
        return default
    parts = expr.split()
    if len(parts) != 5:
        return default
    minute, hour, dom, month, dow = parts
    if minute in ("0", "*") and hour.startswith("*/") and hour[2:].isdigit():
        n = int(hour[2:])
        if n > 0:
            return n * 3600
    if hour == "*" and minute.startswith("*/") and minute[2:].isdigit():
        n = int(minute[2:])
        if n > 0:
            return n * 60
    if minute == "0" and hour.isdigit() and dom == "*" and month == "*":
        if dow == "*":
            return 86400.0
        if dow.isdigit():
            return 604800.0
    return default


def _load_executions(store: Path) -> dict[str, list[dict]] | None:
    """Load the parallel executions ledger for a cron store (jobs.json sidecar).

    The ledger lives next to the store as <dir>/executions.db and is the only
    authoritative source of per-run claimed_at/started_at/finished_at. Returns
    {job_id: [run dicts]} or None when the ledger is missing/unreadable.
    """
    exdb = store.with_name("executions.db")
    if not exdb.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{exdb}?mode=ro", uri=True, timeout=10)
        try:
            rows = conn.execute(
                "SELECT job_id, id, status, claimed_at, started_at, finished_at "
                "FROM executions"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    out: dict[str, list[dict]] = {}
    for jid, eid, status, claimed, started, finished in rows:
        out.setdefault(jid, []).append(
            {
                "id": eid,
                "status": status,
                "claimed_at": claimed,
                "started_at": started,
                "finished_at": finished,
            }
        )
    return out


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def load_store(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text())
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    return {j["id"]: j for j in jobs if isinstance(j, dict) and j.get("id")}


def load_git_copy(path: Path, ref: str) -> dict[str, dict] | None:
    repo = path
    while repo != repo.parent and not (repo / ".git").exists():
        repo = repo.parent
    if not (repo / ".git").exists():
        return None
    rel = path.relative_to(repo)
    try:
        out = subprocess.run(
            ["git", "show", f"{ref}:{rel.as_posix()}"],
            cwd=repo, capture_output=True, text=True, check=True, timeout=60,
        ).stdout
    except Exception:
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    return {j["id"]: j for j in jobs if isinstance(j, dict) and j.get("id")}


def cmd_assert_state(args) -> int:
    store = Path(args.jobs_file)
    jobs = load_store(store)
    job = jobs.get(args.job_id)
    if job is None:
        print(f"FAIL: job {args.job_id} not present in {store}")
        return 1
    expected = {}
    if args.expect_enabled is not None:
        expected["enabled"] = args.expect_enabled == "true"
    if args.expect_state:
        expected["state"] = args.expect_state
    if args.expect_paused_at_set:
        expected["paused_at"] = "<set>"

    bad = []
    for key, want in expected.items():
        got = job.get(key)
        if want == "<set>":
            if not got:
                bad.append(f"{key}: expected a non-null timestamp, got {got!r}")
        elif got != want:
            bad.append(f"{key}: expected {want!r}, got {got!r}")

    label = f"{job.get('name', '?')} ({args.job_id})"
    if bad:
        print(f"** POST-MUTATION ASSERTION FAILED for {label} in {store}")
        for line in bad:
            print(f"   - {line}")
        print("   The mutation did NOT persist as claimed. Do NOT complete the task.")
        return 1
    shown = ", ".join(f"{k}={job.get(k)!r}" for k in STATE_KEYS)
    print(f"OK: {label} persisted state verified by re-read -> {shown}")
    return 0


def cmd_assert_disabled(args) -> int:
    store = Path(args.jobs_file)
    jobs = load_store(store)
    executions = _load_executions(store)
    if executions is None:
        print(
            f"WARN: no executions ledger ({store.with_name('executions.db')}); "
            f"cannot verify post-pause claims for {len(jobs)} job(s) in {store}",
            file=sys.stderr,
        )
    problems = []
    regressed_ids = set()
    checked = 0
    for job_id, job in jobs.items():
        if job.get("enabled") is not False:
            continue
        state = str(job.get("state") or "").lower()
        if state != "paused":
            # Not a paused disable (e.g. one-shot 'completed' or 'disabled').
            # Only paused disables carry the durable-pause invariant.
            continue
        checked += 1
        name = job.get("name", "?")
        paused_raw = job.get("paused_at")
        paused_at = _parse_ts(paused_raw) if isinstance(paused_raw, str) else None
        if paused_at is None:
            problems.append(
                f"{name} ({job_id}): enabled=false/state=paused but paused_at is "
                f"missing or unparseable ({paused_raw!r})"
            )
            continue
        claim_window = args.claim_window_seconds
        if claim_window is None:
            claim_window = _estimate_interval_seconds(job.get("schedule"), DEFAULT_CLAIM_WINDOW_SECONDS)
        exes = (executions or {}).get(job_id, [])
        # A paused job "regressed" only when a run was CLAIMED (scheduler
        # dispatch) strictly after paused_at + one claim window (≈ scheduler
        # interval). A run that merely FINISHED after paused_at but was claimed
        # before it — in-flight at pause time — is benign and must NOT alert.
        regressed = []
        for e in exes:
            claimed = _parse_ts(e["claimed_at"])
            if claimed is None:
                continue
            delta = (claimed - paused_at).total_seconds()
            if delta > claim_window:
                regressed.append((delta, e["claimed_at"], e["started_at"],
                                  e["finished_at"], e["id"]))
        for delta, claimed_raw, started_raw, finished_raw, run_id in sorted(regressed):
            regressed_ids.add(job_id)
            problems.append(
                f"{name} ({job_id}): run {run_id} claimed_at {claimed_raw} "
                f"(started {started_raw or '?'}, finished {finished_raw or '?'}) "
                f"is {delta:.0f}s AFTER paused_at {paused_raw} (claim window "
                f"{claim_window:.0f}s) — job was re-scheduled and ran again after "
                f"its reviewed pause (post-ticker re-verify FAILED)"
            )
    if not problems:
        return 0
    print(
        f"** CRON_STORE_DISABLED_STATE_DIVERGENCE in {store}: {len(regressed_ids)} "
        f"paused job(s) regressed after re-verify ({checked} paused checked)."
    )
    for line in problems:
        print(f"   - {line}")
    print(
        "   The reviewed scheduler disable(s) did NOT stay durable across the "
        "ticker cycle. Do NOT issue GREEN / complete the reviewed-disable."
    )
    return 1


def cmd_watch(args) -> int:
    store = Path(args.jobs_file)
    live = load_store(store)
    tracked = load_git_copy(store, args.ref)
    if tracked is None:
        print(f"SKIP: {store} is not git-tracked at {args.ref}; no revert risk from git checkout")
        return 0

    diffs = []
    diverged_ids = set()
    for job_id, job in live.items():
        other = tracked.get(job_id)
        if other is None:
            continue
        for key in ("enabled", "state"):
            if job.get(key) != other.get(key):
                diverged_ids.add(job_id)
                diffs.append(
                    f"{job.get('name', '?')} ({job_id}) {key}: live={job.get(key)!r} "
                    f"vs {args.ref}={other.get(key)!r}"
                )
    if not diffs:
        return 0
    print(
        f"** CRON_STORE_VC_DIVERGENCE in {store}: {len(diverged_ids)} job(s) whose schedule "
        f"state differs from the git-tracked copy at {args.ref}."
    )
    for line in diffs:
        print(f"   - {line}")
    print(
        "   RISK: any `git checkout -- <this path>` (or a keeper/deploy restore) will "
        "silently revert these reviewed schedule-state decisions. Commit the reviewed "
        "state, or stop tracking live schedule state in VC."
    )
    return 1


def cmd_selftest() -> int:
    """Fixture verifying the claimed_at-vs-finished_at regression rule.

    Builds a throwaway store + executions ledger containing (a) one in-flight-at-
    pause job (run CLAIMED before paused_at, only FINISHED after it) which must
    NOT be flagged, and (b) one truly regressed job (run CLAIMED a full interval
    after paused_at) which MUST be flagged. Exits 0 only on correct classification.
    """
    import contextlib
    import io
    import shutil
    import tempfile
    import types

    tmp = Path(tempfile.mkdtemp(prefix="cronstore-selftest-"))
    try:
        jobs_file = tmp / "jobs.json"
        exdb = tmp / "executions.db"
        jobs = {
            "jobs": [
                {
                    "id": "in-flight-at-pause", "name": "fixture-inflight",
                    "enabled": False, "state": "paused",
                    "paused_at": "2026-08-22T12:00:00+01:00",
                    "last_run_at": "2026-08-22T12:03:00+01:00",
                    "schedule": {"kind": "interval", "minutes": 5},
                },
                {
                    "id": "truly-regressed", "name": "fixture-regressed",
                    "enabled": False, "state": "paused",
                    "paused_at": "2026-08-22T12:00:00+01:00",
                    "last_run_at": "2026-08-22T13:05:00+01:00",
                    "schedule": {"kind": "interval", "minutes": 5},
                },
            ],
            "updated_at": "2026-08-22T15:00:00+01:00",
        }
        jobs_file.write_text(json.dumps(jobs))
        conn = sqlite3.connect(exdb)
        conn.execute(
            "CREATE TABLE executions (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, "
            "source TEXT, process_id TEXT, pid INTEGER, process_started_at INTEGER, "
            "status TEXT, claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, "
            "error TEXT)"
        )
        rows = [
            # in-flight at pause: claimed 11:57 (< paused 12:00), finished 12:03 (> pause)
            ("run-inflight", "in-flight-at-pause", "cron", "p", 1, None, "completed",
             "2026-08-22T11:57:00+01:00", "2026-08-22T11:58:00+01:00",
             "2026-08-22T12:03:00+01:00", None),
            # truly regressed: claimed 13:00, a full interval (3600s) after pause
            ("run-regressed", "truly-regressed", "cron", "p", 2, None, "completed",
             "2026-08-22T13:00:00+01:00", "2026-08-22T13:00:00+01:00",
             "2026-08-22T13:05:00+01:00", None),
        ]
        conn.executemany("INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()

        args = types.SimpleNamespace(
            jobs_file=str(jobs_file), claim_window_seconds=None,
            ticker_grace_seconds=DEFAULT_TICKER_GRACE_SECONDS,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_assert_disabled(args)
        out = buf.getvalue()
        flags_regressed = "run-regressed" in out
        flags_inflight = "run-inflight" in out
        if rc != 0 and flags_regressed and not flags_inflight:
            print("SELFTEST PASS: truly-regressed flagged; in-flight-at-pause NOT flagged.")
            return 0
        print("SELFTEST FAIL:")
        print(out)
        print(
            f"classification: rc={rc} flags_regressed={flags_regressed} "
            f"flags_inflight={flags_inflight}"
        )
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    p = argparse.ArgumentParser(description="cron-store mutation verifier")
    p.add_argument("--jobs-file", required=False)
    sub = p.add_mutually_exclusive_group(required=True)
    sub.add_argument("--assert-state", action="store_true")
    sub.add_argument("--assert-disabled", action="store_true")
    sub.add_argument("--watch", action="store_true")
    sub.add_argument("--selftest", action="store_true")
    p.add_argument("--job-id")
    p.add_argument("--expect-enabled", choices=["true", "false"])
    p.add_argument("--expect-state")
    p.add_argument("--expect-paused-at-set", action="store_true")
    p.add_argument("--ticker-grace-seconds", type=float, default=DEFAULT_TICKER_GRACE_SECONDS)
    p.add_argument("--claim-window-seconds", type=float, default=None,
                   help="post-pause claim window (s); defaults to one scheduler interval per job")
    p.add_argument("--ref", default="HEAD")
    args = p.parse_args()

    if args.selftest:
        return cmd_selftest()
    if not args.jobs_file:
        p.error("--jobs-file is required for --assert-state/--assert-disabled/--watch")
    if args.assert_state:
        if not args.job_id:
            p.error("--assert-state requires --job-id")
        return cmd_assert_state(args)
    if args.assert_disabled:
        return cmd_assert_disabled(args)
    return cmd_watch(args)


if __name__ == "__main__":
    sys.exit(main())
