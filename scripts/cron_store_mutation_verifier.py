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
                   is still set (durable) and (b) no last_run_at newer than the
                   pause plus a ticker grace — i.e. the paused job has NOT been
                   silently re-scheduled and run again after its pause. This is
                   the scheduled post-ticker re-verification gate (t_fcb6141f):
                   a reviewed scheduler disable is only GREEN once a ticker
                   cycle has passed and this sweep still reports it paused with
                   no new run. Silent + exit 0 when healthy; print divergences
                   + exit 1 on any regression.

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
    problems = []
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
        last_raw = job.get("last_run_at")
        last_run = _parse_ts(last_raw) if isinstance(last_raw, str) else None
        if last_run is not None:
            # no new run after pause (beyond the benign sub-second race grace)
            too_late = last_run - paused_at
            if too_late.total_seconds() > args.ticker_grace_seconds:
                problems.append(
                    f"{name} ({job_id}): last_run_at {last_raw} is "
                    f"{too_late.total_seconds():.0f}s AFTER paused_at "
                    f"{paused_raw} — job was re-scheduled and ran again "
                    f"after its reviewed pause (post-ticker re-verify FAILED)"
                )
    if not problems:
        return 0
    print(
        f"** CRON_STORE_DISABLED_STATE_DIVERGENCE in {store}: {len(problems)} "
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


def main() -> int:
    p = argparse.ArgumentParser(description="cron-store mutation verifier")
    p.add_argument("--jobs-file", required=True)
    sub = p.add_mutually_exclusive_group(required=True)
    sub.add_argument("--assert-state", action="store_true")
    sub.add_argument("--assert-disabled", action="store_true")
    sub.add_argument("--watch", action="store_true")
    p.add_argument("--job-id")
    p.add_argument("--expect-enabled", choices=["true", "false"])
    p.add_argument("--expect-state")
    p.add_argument("--expect-paused-at-set", action="store_true")
    p.add_argument("--ticker-grace-seconds", type=float, default=DEFAULT_TICKER_GRACE_SECONDS)
    p.add_argument("--ref", default="HEAD")
    args = p.parse_args()

    if args.assert_state:
        if not args.job_id:
            p.error("--assert-state requires --job-id")
        return cmd_assert_state(args)
    if args.assert_disabled:
        return cmd_assert_disabled(args)
    return cmd_watch(args)


if __name__ == "__main__":
    sys.exit(main())
