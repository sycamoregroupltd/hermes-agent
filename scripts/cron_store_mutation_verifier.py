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

  --watch          Compare the live store against the git-tracked copy of the
                   SAME file and report jobs whose enabled/state differ. A
                   difference here means the live store and the VC snapshot
                   disagree about schedule state — the exact condition under
                   which a `git checkout --` of this path silently reverts a
                   reviewed pause. Exit 1 when any enabled/state divergence
                   exists.

Read-only. Never mutates a cron store, schedule, provider, or credential.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

STATE_KEYS = ("enabled", "state", "paused_at")


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
    sub.add_argument("--watch", action="store_true")
    p.add_argument("--job-id")
    p.add_argument("--expect-enabled", choices=["true", "false"])
    p.add_argument("--expect-state")
    p.add_argument("--expect-paused-at-set", action="store_true")
    p.add_argument("--ref", default="HEAD")
    args = p.parse_args()

    if args.assert_state:
        if not args.job_id:
            p.error("--assert-state requires --job-id")
        return cmd_assert_state(args)
    return cmd_watch(args)


if __name__ == "__main__":
    sys.exit(main())
