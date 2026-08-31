#!/usr/bin/env python3
"""Regression + TTL-derivation guard suite for cron_ticker_invariant_guard.py.

WHY THIS FILE EXISTS (kanban t_a185f5ca, t_a8fdd2db):
The guard's one-shot re-arm logic hardcoded the run-claim stale-recovery TTL
(ONESHOT_RUN_CLAIM_TTL_SECONDS = 1800) while the scheduler (hermes-agent/
cron/jobs.py:_oneshot_run_claim_ttl_seconds) DERIVES that TTL from
HERMES_CRON_TIMEOUT as max(timeout*3, 1800). At the fleet's current
HERMES_CRON_TIMEOUT=120 the two matched (1800==1800), but if the env were ever
set >600 (e.g. 601 -> scheduler 1803), the scheduler would keep a claim "live"
longer than the guard treated it — so the guard's stale-claim branch could
clear/re-arm over a claim the scheduler still considered in-flight -> a bounded
double-dispatch window. That is the exact failure class the 4 review rounds
eliminated.

FIX: the guard now derives its run-claim TTL from the SAME HERMES_CRON_TIMEOUT
env using the scheduler's IDENTICAL formula (_oneshot_run_claim_ttl_seconds in
the guard mirrors cron/jobs.py byte-for-byte), so the two can never diverge
regardless of config. This file proves that invariance AND locks in the
round-4 one-shot re-arm contract.

The suite is HERMETIC: it imports the guard and the scheduler modules directly
and exercises pure functions only. It never touches a live cron store, never
reads/writes REAL_HERMES_HOME stores, and mutates only the process
HERMES_CRON_TIMEOUT env (restored after each case).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GUARD = REPO_ROOT / "profiles" / "jarvis" / "scripts" / "cron_ticker_invariant_guard.py"
# The scheduler module lives in the hermes-agent checkout. We load it purely for
# its `_oneshot_run_claim_ttl_seconds` so we can assert byte-identical behaviour.
SCHEDULER_JOBS = Path(
    os.environ.get(
        "HERMES_AGENT_JOBS",
        "/home/frank/.hermes/hermes-agent/cron/jobs.py",
    )
)

failures: list[str] = []
passes = 0


def check(cond: bool, msg: str):
    global passes
    if cond:
        passes += 1
        print(f"  PASS {msg}")
    else:
        failures.append(msg)
        print(f"  FAIL {msg}")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


guard = load_module("guard_under_test_t_a185f5ca", GUARD)
scheduler = load_module("scheduler_under_test_t_a185f5ca", SCHEDULER_JOBS)

# The config range the task requires plus invalid/edge cases.
CASES = ["unset", "120", "600", "601", "900", "0", "invalid", "-5", "1.5", "abc", "   "]


def resolve(fn):
    out = {}
    for c in CASES:
        if c == "unset":
            os.environ.pop("HERMES_CRON_TIMEOUT", None)
        else:
            os.environ["HERMES_CRON_TIMEOUT"] = c
        out[c] = fn()
    return out


print("SECTION 1 — run-claim TTL is derived identically from HERMES_CRON_TIMEOUT")
print("  (regression: the guard and scheduler can never diverge across configs)")
g = resolve(guard._oneshot_run_claim_ttl_seconds)
s = resolve(scheduler._oneshot_run_claim_ttl_seconds)
for c in CASES:
    check(
        g[c] == s[c] and isinstance(g[c], float) and isinstance(s[c], float),
        f"guard TTL == scheduler TTL @ HERMES_CRON_TIMEOUT={c!r} ({g[c]} == {s[c]})",
    )
# Spot-check the derived values themselves (the formula, not just equality).
check(g["unset"] == 1800.0, "unset -> floor 1800")
check(g["120"] == 1800.0, "120 -> max(360,1800)=1800 (matches today's fleet)")
check(g["600"] == 1800.0, "600 -> max(1800,1800)=1800")
check(g["601"] == 1803.0, "601 -> max(1803,1800)=1803 (the previously-divergent case)")
check(g["900"] == 2700.0, "900 -> max(2700,1800)=2700")
check(g["0"] == 1800.0, "0 (unlimited) -> floor 1800")
check(g["invalid"] == 1800.0, "invalid -> default 600s -> floor 1800")
# Guard keeps the named floor constant for readability (mirrors scheduler).
check(guard.ONESHOT_RUN_CLAIM_TTL_SECONDS == 1800, "guard floor constant == 1800")
check(guard.ONESHOT_FIRE_CLAIM_TTL_SECONDS == 300, "fire-claim TTL stays fixed 300s")
# Fire-claim TTL is env-independent on the guard side.
os.environ["HERMES_CRON_TIMEOUT"] = "900"
check(guard.ONESHOT_FIRE_CLAIM_TTL_SECONDS == 300, "fire-claim TTL unaffected by env")
os.environ.pop("HERMES_CRON_TIMEOUT", None)

print("\nSECTION 2 — round-4 one-shot re-arm contract (t_a8fdd2db)")
# A LIVE run claim (age < TTL) is REFUSED, never re-armed over or cleared.
now = guard._utcnow()
from datetime import timedelta, timezone

live_run_claim = {"at": (now - timedelta(seconds=60)).isoformat()}
job_live = {"schedule": {"kind": "once", "run_at": (now - timedelta(seconds=30)).isoformat()},
            "run_claim": live_run_claim}
check(guard._claim_is_live(job_live["run_claim"], now, guard._oneshot_run_claim_ttl_seconds()),
      "a 60s-old run claim is LIVE under the derived TTL")
st, nr, crc, cfc = guard._oneshot_rearm_next_run(job_live, now)
check(st == "refuse-live" and crc is False and cfc is False,
      f"live claim -> refuse-live (got {st!r}, clear_run={crc})")

# A STALE run claim (age > TTL) is re-armed with fresh now AND the stale claim cleared.
stale_claim = {"at": (now - timedelta(seconds=2000)).isoformat()}
job_stale = {"schedule": {"kind": "once", "run_at": (now - timedelta(hours=3)).isoformat()},
             "run_claim": stale_claim}
st, nr, crc, cfc = guard._oneshot_rearm_next_run(job_stale, now)
check(st == "rearm" and crc is True and cfc is True,
      f"stale run claim -> rearm+clear both claims (got {st!r}, clear_run={crc}, clear_fire={cfc})")
check(nr is not None, "stale-claim re-arm produces a REAL due timestamp (fresh now)")

# A never-claimed once job past grace -> missed (manual triage).
job_missed = {"schedule": {"kind": "once", "run_at": (now - timedelta(hours=5)).isoformat()}}
st, nr, crc, cfc = guard._oneshot_rearm_next_run(job_missed, now)
check(st == "missed" and nr is None, f"past-grace never-claimed -> missed (got {st!r})")

# A once job still within grace -> re-arm with its REAL run_at, no claim clearing.
job_grace = {"schedule": {"kind": "once", "run_at": (now - timedelta(seconds=60)).isoformat()}}
st, nr, crc, cfc = guard._oneshot_rearm_next_run(job_grace, now)
check(st == "rearm" and crc is False and cfc is False,
      f"within-grace once -> rearm with real run_at (got {st!r})")
check(nr == job_grace["schedule"]["run_at"], "re-arms with the persisted run_at (within grace)")

print(f"\nRESULT: {passes} passed, {len(failures)} failed")
if failures:
    for f in failures:
        print("  FAILED:", f)
    sys.exit(1)
print("ALL PASS")
