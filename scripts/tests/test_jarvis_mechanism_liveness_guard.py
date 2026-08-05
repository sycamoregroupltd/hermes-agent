#!/usr/bin/env python3
"""Standing regression coverage for the mechanism-liveness classifier (C1).

Guards ``jarvis_mechanism_liveness_collect.classify_job`` for the liveness
definition landed in t_2d87b476 (C1): a job is alive when ANY of:

  1. jobs.json ``last_run_at`` is fresh (existing rule), OR
  2. executions.db has a claimed/running row with claim age
     <= max_age_minutes + LIVENESS_GRACE_MIN (same writer, same profile
     store), OR
  3. the newest output artifact mtime is fresh within
     max_age_minutes + LIVENESS_GRACE_MIN.

Only when ALL THREE signals are stale may the row classify DEAD. This is the
durable invariant from the 2026-08-03 root-cause note (t_592db3b5): the jarvis
single-worker cron pool holds claims 25-30 min behind provider-starved LLM
jobs, so ``last_run_at`` lags while executions.db + artifacts prove liveness.

Why this file exists: t_a9783eff / t_3c33bc49 shipped the output-artifact
guard with a ``replay_proof.py`` that was never committed, so the guard had
zero standing coverage. This test is the persisted replacement, extended for
the executions.db claimed/running signal.

Cases covered:
  (a) no_agent + stale last_run_at + FRESH artifact  -> OK  (artifact rescue)
  (b) no_agent + stale last_run_at + STALE artifact  -> DEAD (all three stale)
  (c) no_agent + stale last_run_at + NO artifact dir -> DEAD (all three stale)
  (d) agent job (no_agent falsy) + stale            -> DEAD (no executions/
      artifact signal for agent jobs)
  (e) reason-string attribution: the OK reason names the rescue signal
  (f) output_artifact_age_minutes() returns None (not 0.0) for missing/empty
      output dir, otherwise case (c) would flip to OK
  (g) end-to-end via a real artifact dir
  (h) C1: stale last_run_at + FRESH claimed/running execution (executions.db)
      -> OK even when artifact is absent and the job is an agent job
  (i) C1: stale in ALL THREE signals -> DEAD (never-run + no claim + no
      artifact), and stale last_run + stale claim + stale artifact -> DEAD
  (j) C1: fresh claimed execution rescues a stale-last_run row from the real
      executions.db path (row_for_expected wiring)

Read-only: everything runs against tmp roots via monkeypatching the module's
OUTPUT_ROOT / PROFILES. The live jarvis cron store is never touched.

Run:  python3 -m pytest scripts/tests/test_jarvis_mechanism_liveness_guard.py -q
   or python3 scripts/tests/test_jarvis_mechanism_liveness_guard.py   (no pytest needed)
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
COLLECTOR = SCRIPTS / "jarvis_mechanism_liveness_collect.py"


def _load_collector():
    name = "_liveness_collector_under_test"
    spec = importlib.util.spec_from_file_location(name, COLLECTOR)
    assert spec and spec.loader, f"cannot load {COLLECTOR}"
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: the collector defines a @dataclass, and
    # dataclasses resolves cls.__module__ through sys.modules at class-creation
    # time. Without this the import fails with an opaque AttributeError.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_collector()
setattr_mod = setattr  # noqa: N816  (alias keeps the patch sites terse)

NOW = datetime(2026, 7, 31, 6, 0, 0, tzinfo=timezone.utc)
MAX_AGE = 90  # minutes; matches the pm-triage-upero row in EXPECTED
STALE_LAST_RUN = (NOW - timedelta(hours=47)).isoformat()
# Deliberately far in the future so the one-strike grace branch (which needs
# next_run within LIVENESS_GRACE_MIN) cannot mask what we are testing.
FAR_NEXT_RUN = (NOW + timedelta(hours=6)).isoformat()


def _job(no_agent: object = True, **over) -> dict:
    job = {
        "id": "jtest",
        "name": "test-job",
        "enabled": True,
        "state": "scheduled",
        "no_agent": no_agent,
        "last_status": "ok",
        "last_run_at": STALE_LAST_RUN,
        "next_run_at": FAR_NEXT_RUN,
        "created_at": "2026-07-01T00:00:00+00:00",
        "schedule": {"kind": "interval", "minutes": 10, "display": "every 10m"},
    }
    job.update(over)
    return job


def _classify(job: dict, claim_age: float | None = None, artifact_age: float | None = None):
    return mod.classify_job(
        "jarvis", job, NOW, MAX_AGE, allow_not_due=True,
        claimed_execution_age=claim_age,
        output_artifact_age=artifact_age,
    )


# --------------------------------------------------------------------------
# The four required cases (ported from the cc4819c-era guard test, updated to
# the new signature: a fresh artifact OR fresh claimed execution rescue;
# stale-in-all-three is DEAD)
# --------------------------------------------------------------------------

def test_a_no_agent_stale_metadata_fresh_artifact_is_ok():
    status, reason, age = _classify(_job(no_agent=True), artifact_age=5.0)
    assert status == "OK", f"expected OK, got {status}: {reason}"
    assert age is not None and age > MAX_AGE, "last_run age should still report the real drift"


def test_b_no_agent_stale_metadata_stale_artifact_is_dead():
    # Stale artifact beyond max_age+LIVENESS_GRACE_MIN threshold must not rescue
    # (the old guard used max_age alone; C1 adds grace so we must exceed the grace threshold)
    status, reason, _ = _classify(_job(no_agent=True), artifact_age=MAX_AGE + mod.LIVENESS_GRACE_MIN + 1.0)
    assert status == "DEAD", f"stale artifact must not rescue a stale job: {reason}"


def test_c_no_agent_stale_metadata_no_artifact_dir_is_dead():
    status, reason, _ = _classify(_job(no_agent=True), artifact_age=None)
    assert status == "DEAD", f"absent artifact must not rescue a stale job: {reason}"


def test_d_agent_job_stale_is_dead_without_liveness_signal():
    for falsy in (False, None, 0):
        status, reason, _ = _classify(_job(no_agent=falsy))
        assert status == "DEAD", (
            f"agent job with no executions/artifact signal must stay DEAD; "
            f"no_agent={falsy!r} classified {status}: {reason}"
        )


# --------------------------------------------------------------------------
# Structural regressions that would silently defeat the guard
# --------------------------------------------------------------------------

def test_e_guard_reason_is_attributable():
    _, reason, _ = _classify(_job(no_agent=True), artifact_age=5.0)
    assert "output-artifact liveness" in reason, (
        "the OK reason must state it passed via output-artifact liveness, not a real "
        f"fresh last_run_at; got: {reason}"
    )
    _, reason, _ = _classify(_job(no_agent=False), claim_age=3.0)
    assert "executions.db" in reason, (
        "the executions rescue reason must name executions.db liveness; "
        f"got: {reason}"
    )


def test_f_artifact_age_none_for_missing_and_empty_dir():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        original = getattr(mod, "OUTPUT_ROOT")
        try:
            setattr_mod(mod, "OUTPUT_ROOT", root)
            assert mod.output_artifact_age_minutes("no-such-job", NOW) is None, \
                "missing output dir must be None, not 0.0"

            empty = root / "empty-job"
            empty.mkdir()
            assert mod.output_artifact_age_minutes("empty-job", NOW) is None, \
                "empty output dir must be None, not 0.0"

            live = root / "live-job"
            live.mkdir()
            artifact = live / "2026-07-31_05-50-00.md"
            artifact.write_text("x")
            mtime = (NOW - timedelta(minutes=10)).timestamp()
            os.utime(artifact, (mtime, mtime))
            age = mod.output_artifact_age_minutes("live-job", NOW)
            assert age is not None and 9.0 <= age <= 11.0, f"expected ~10m, got {age}"
        finally:
            setattr_mod(mod, "OUTPUT_ROOT", original)


def test_g_end_to_end_via_real_artifact_dir():
    """Case (a) again, but sourcing the age from a real file on disk rather
    than a hand-passed float — proves the collector's own plumbing wires the
    artifact age into classify_job correctly."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        original = getattr(mod, "OUTPUT_ROOT")
        try:
            setattr_mod(mod, "OUTPUT_ROOT", root)
            d = root / "jtest"
            d.mkdir()
            f = d / "2026-07-31_05-55-00.md"
            f.write_text("# Cron Job\n**Status:** silent (empty output)\n")
            mtime = (NOW - timedelta(minutes=5)).timestamp()
            os.utime(f, (mtime, mtime))

            age = mod.output_artifact_age_minutes("jtest", NOW)
            status, reason, _ = _classify(_job(no_agent=True), artifact_age=age)
            assert status == "OK", f"expected OK from real artifact, got {status}: {reason}"

            # And the negative: age the same artifact out past the window.
            old = (NOW - timedelta(hours=5)).timestamp()
            os.utime(f, (old, old))
            age = mod.output_artifact_age_minutes("jtest", NOW)
            status, reason, _ = _classify(_job(no_agent=True), artifact_age=age)
            assert status == "DEAD", f"aged-out artifact must be DEAD, got {status}: {reason}"
        finally:
            setattr_mod(mod, "OUTPUT_ROOT", original)


# --------------------------------------------------------------------------
# C1 — executions.db claimed/running freshness (t_2d87b476)
# --------------------------------------------------------------------------

def _seed_executions_db(profiles_root, job_id, statuses_claims):
    """Create an executions.db with the production schema and seed rows.

    statuses_claims: list of (status, claimed_dt_iso) tuples.
    Drops any existing table before creating.
    """
    db = profiles_root / "jarvis" / "cron" / "executions.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("DROP TABLE IF EXISTS executions")
    con.execute(
        "CREATE TABLE executions ("
        " id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,"
        " process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER,"
        " status TEXT NOT NULL, claimed_at TEXT NOT NULL, started_at TEXT,"
        " finished_at TEXT, error TEXT)"
    )
    for i, (status, claimed_at) in enumerate(statuses_claims):
        con.execute(
            "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at)"
            " VALUES (?, ?, 'test', 'p', 1, ?, ?)",
            (f"seed-{i}", job_id, status, claimed_at),
        )
    con.commit()
    con.close()


def test_h_fresh_claimed_execution_rescues_stale_metadata():
    """AC: a job with stale last_run_at but a fresh claimed execution classifies
    OK (not DEAD) — even for an agent job with no artifact."""
    status, reason, _ = _classify(_job(no_agent=False), claim_age=5.0)
    assert status == "OK", f"fresh claimed execution must rescue: {status}: {reason}"
    assert "executions.db" in reason


def test_i_stale_in_all_three_is_dead():
    """AC: a job stale in ALL THREE signals still classifies DEAD."""
    # never-run + no claim + no artifact
    status, reason, _ = _classify(
        _job(no_agent=True, last_run_at=None, next_run_at=None),
        claim_age=None, artifact_age=None,
    )
    assert status == "DEAD", f"never-run + no signals must be DEAD: {status}: {reason}"
    # stale last_run + stale claim + stale artifact
    status, reason, _ = _classify(
        _job(no_agent=True),
        claim_age=MAX_AGE + mod.LIVENESS_GRACE_MIN + 10,
        artifact_age=MAX_AGE + mod.LIVENESS_GRACE_MIN + 10,
    )
    assert status == "DEAD", f"all-three-stale must be DEAD: {status}: {reason}"


import glob, os as _os

def test_j_claimed_execution_age_reads_real_executions_db():
    """The executions.db plumbing: fresh_claimed_execution_age_minutes() reads
    the newest claimed/running row for the job from the profile store."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        original_profiles = getattr(mod, "PROFILES")
        original_output = getattr(mod, "OUTPUT_ROOT")
        try:
            setattr_mod(mod, "PROFILES", root)
            # Clean any leftover executions.db from prior tests
            db_dir = root / "jarvis" / "cron"
            if db_dir.exists():
                for f in glob.glob(str(db_dir / "*.db")):
                    _os.remove(f)
            _seed_executions_db(
                root, "jtest",
                [
                    ("completed", (NOW - timedelta(hours=5)).isoformat()),
                    ("claimed", (NOW - timedelta(minutes=3)).isoformat()),
                ],
            )
            age = mod.fresh_claimed_execution_age_minutes("jarvis", "jtest", NOW)
            assert age is not None and 2.0 <= age <= 4.0, f"expected ~3m, got {age}"
            # row_for_expected wiring: classify via the real DB path
            status, reason, _ = _classify(_job(no_agent=False))
            assert status == "OK", f"real executions.db claim must rescue: {status}: {reason}"

            # stale claim (older than max_age+grace) must NOT rescue
            setattr_mod(mod, "PROFILES", root)
            _seed_executions_db(
                root, "jtest",
                [("claimed", (NOW - timedelta(hours=4)).isoformat())],
            )
            status, reason, _ = _classify(_job(no_agent=False))
            assert status == "DEAD", f"stale claim must not rescue: {status}: {reason}"
        finally:
            setattr_mod(mod, "PROFILES", original_profiles)


def main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures.append((name, str(exc)))
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
