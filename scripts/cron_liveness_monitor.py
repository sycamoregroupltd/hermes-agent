#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""
cron_liveness_monitor.py — missed-occurrence (liveness) monitor for hermes cron.

WHY (task t_5ed62544, fable PM 2026-08-05):
  Wave-5 found hl-funding-carry-oos-daily, delist-short-oos-daily and
  candles-venue-recon-daily enabled but SILENTLY not firing their 08-05
  occurrences (silent-writer-death class). The structural fix owed per
  silent-failure doctrine is a monitor that alerts when an enabled recurring
  job has no proof of a completed run within (schedule interval + grace).

AMENDED SPEC (jarvis PM 2026-08-05 ~19:40Z):
  Do NOT trust jobs.json last_run alone (it can be None even when a job ran —
  hip3-xyz-funding-observe ran hourly with last_run=None). Read the DURABLE
  execution record (cron/executions.db, the same store `hermes cron runs`
  reads) and alert when an enabled job has no completed attempt within
  (schedule interval + grace). ALSO cover repeated 'Script not found'
  failures (sibling card t_027a2bc9).

RETENTION TRAP (verified 2026-08-11):
  executions.db is capped at MAX_TERMINAL_EXECUTIONS=1000 per store. The
  jarvis store is saturated, so low-frequency jobs (4h, daily) have their
  completed rows EVICTED within ~2-3h even when healthy. A pure executions.db
  check therefore false-alarms on healthy low-frequency jobs. The correct
  signal is the UNION of:
    (a) a recent `completed` execution row in executions.db, AND
    (b) a fresh `last_run_at` in jobs.json whose `last_status` is ok
  (the scheduler reliably persists last_run_at on every fire; hip3 shows
  last_run_at=14:05 today). A job is MISSED only when BOTH are stale by more
  than (period + grace).

BEHAVIOR:
  - Scans every profile cron store + the root cron store.
  - For each ENABLED recurring (interval/cron) job computes the schedule
    period and the (period + grace) liveness threshold.
  - Emits one MISSED line per job with no proof of a completed run within the
    threshold (covers the silent-death and no-recorded-run classes).
  - SKIPS every job in a profile whose owning cron store is under an active
    operator emergency stop (`hermes pause`, ESTOP sentinel at
    `~/.hermes/profiles/<p>/ESTOP` or `*.ESTOP`, or `$HERMES_HOME/ESTOP` for the
    root store). Those profiles are reported as SUSPENDED (excluded) — an
    intentional operator pause, not a silent-writer-death — so no breach card is
    minted while the pause is in effect (task t_6c2abedd). The monitor returns
    to normal MISSED detection for those jobs once the sentinel is lifted.
  - Emits SCRIPT_MISSING lines for enabled recurring jobs whose script does
    not resolve in either the profile-local or global scripts dir (pre-flight
    for the 'Script not found' class that silently fails every tick).
  - Prints a machine-readable JSON summary to stdout for the kanban router,
    and exits 1 when any finding exists (exit 0 clean => healthy).
  - READ-ONLY: opens sqlite in ro mode; never writes anywhere.

CLI:
  --json      emit a single JSON document on stdout (router/alert consumers)
  --selftest  offline deterministic test (no live stores)
Env:
  CRON_LIVENESS_GRACE_H  default 2.0  (hours added to period before alerting)
  CRON_LIVENESS_HOME     default ~/.hermes
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("CRON_LIVENESS_HOME", os.path.expanduser("~/.hermes")))
GRACE_H = float(os.environ.get("CRON_LIVENESS_GRACE_H", "2.0"))
GRACE = timedelta(hours=GRACE_H)
# Never alert on jobs younger than this even if they show no proof of life
# yet — a freshly created job's first occurrence may not have come due.
MIN_JOB_AGE = timedelta(hours=1)

try:
    from croniter import croniter
except Exception:  # pragma: no cover
    croniter = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(v) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def schedule_period_seconds(schedule) -> int | None:
    if not isinstance(schedule, dict):
        return None
    kind = schedule.get("kind")
    if kind == "interval":
        m = schedule.get("minutes")
        try:
            return int(float(str(m)) * 60)
        except Exception:
            return None
    if kind == "cron" and croniter is not None:
        expr = schedule.get("expr")
        if not expr:
            return None
        try:
            it = croniter(expr, _now())
            a = it.get_next(datetime)
            b = it.get_next(datetime)
            return int((b - a).total_seconds())
        except Exception:
            return None
    return None


def _exec_connection(db_path: Path):
    if not db_path.exists():
        return None
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        return con
    except Exception:
        return None


def load_enabled_jobs(cron_dir: Path) -> tuple[list[dict], str | None]:
    """Return (enabled recurring jobs list, profile label)."""
    jobs_file = cron_dir / "jobs.json"
    if not jobs_file.exists():
        return [], None
    try:
        data = json.loads(jobs_file.read_text())
    except Exception as exc:
        return [], f"unreadable:{exc}"
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    profile = cron_dir.parent.name if cron_dir.parent.name != ".hermes" else "root"
    if profile == ".hermes":
        profile = "root"
    return [j for j in jobs if isinstance(j, dict)], profile


def script_resolves(profile: str, job: dict) -> bool:
    """Pre-flight: does the job's script exist in profile-local or global dirs?"""
    script = job.get("script")
    if not script:
        return True  # LLM jobs have no script; not this class
    raw = Path(script).expanduser()
    if raw.is_absolute():
        return True  # absolute paths validated elsewhere
    local = (HERMES_HOME / "profiles" / profile / "scripts" / raw).resolve()
    if local.exists() and local.is_file():
        return True
    global_p = (HERMES_HOME / "scripts" / raw).resolve()
    return bool(global_p.exists() and global_p.is_file())


def profile_estop_active(cron_dir: Path) -> bool:
    """Is the profile owning this cron store under an active operator ESTOP?

    `hermes pause` writes a profile-local sentinel at the owning profile's
    HERMES_HOME (`~/.hermes/profiles/<p>/ESTOP`, or `$HERMES_HOME/ESTOP` for the
    root store) and suppresses cron dispatch at the scheduler tick layer
    (estop.py fail-safe, `check_paused("cron", ...)`). It does NOT set per-job
    `state=paused`, so the old job-level filter below missed it. We match the
    exact `ESTOP` name plus any `*.ESTOP` variant, fail-safe toward "paused"
    (a corrupt/unreadable sentinel still counts via existence).
    """
    is_root = (cron_dir == HERMES_HOME / "cron")
    if is_root:
        candidates = [HERMES_HOME / "ESTOP"]
    else:
        prof_dir = cron_dir.parent
        candidates = [prof_dir / "ESTOP"] + sorted(prof_dir.glob("*.ESTOP"))
    return any(p.exists() for p in candidates)


def check_store(cron_dir: Path, findings: list[dict], scanned: list[str],
                suspended: list[str]) -> None:
    jobs, profile = load_enabled_jobs(cron_dir)
    if not jobs:
        return
    if profile is None:
        return
    if profile_estop_active(cron_dir):
        # Operator emergency stop in effect for this profile — every enabled job
        # is deliberately SUSPENDED (cron dispatch suppressed at the tick layer),
        # not silently dead. Exclude the whole store so no MISSED / SCRIPT_MISSING
        # breach card is minted while the pause is active.
        if profile not in suspended:
            suspended.append(profile)
        return
    exec_db = cron_dir / "executions.db"
    con = _exec_connection(exec_db) if exec_db.exists() else None
    t = _now()
    for job in jobs:
        if not job.get("enabled", True) or job.get("state") == "paused":
            continue
        jid = job.get("id")
        name = job.get("name") or "?"
        if not jid:
            continue
        schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        kind = schedule.get("kind")
        if kind not in ("cron", "interval"):
            continue  # one-shot jobs have no recurring cadence to miss
        period = schedule_period_seconds(schedule)
        if period is None:
            # Unresolvable cadence — flag as unverifiable so it is not silently
            # invisible, but low priority (cannot compute a threshold).
            findings.append({
                "class": "UNVERIFIABLE",
                "profile": profile, "job_id": jid, "name": name,
                "detail": f"schedule={schedule} cannot resolve period",
            })
            scanned.append(f"{profile}/{name}")
            continue
        threshold = timedelta(seconds=period) + GRACE

        # --- Liveness proof (UNION of durable record + scheduler last_run) ---
        last_completed: datetime | None = None
        if con is not None:
            try:
                row = con.execute(
                    "SELECT MAX(claimed_at) FROM executions "
                    "WHERE job_id=? AND status='completed'", (jid,)
                ).fetchone()
                if row and row[0]:
                    last_completed = parse_dt(row[0])
            except Exception:
                pass
        last_run = parse_dt(job.get("last_run_at"))
        last_status = str(job.get("last_status") or "").lower()
        # UNION liveness proof: the scheduler persists last_run_at on EVERY
        # attempted fire (ticker bookkeeping is independent of runtime result),
        # so a fresh last_run_at proves the job FIRED on schedule even if that
        # attempt errored. Errored-but-recent jobs are the failure-streak class
        # (hermes_cron_failure_monitor.py), NOT a missed occurrence.
        # executions.db rows are bounded (MAX_TERMINAL_EXECUTIONS=1000) and get
        # evicted for low-frequency jobs within hours, so last_run_at is the more
        # durable liveness signal. Proof = MAX(fresh last_run_at, latest completed
        # execution) so a stale completed row does not shadow a recent fire.
        proof = None
        if last_completed is not None and last_run is not None:
            proof = max(last_completed, last_run)
        elif last_completed is not None:
            proof = last_completed
        elif last_run is not None:
            proof = last_run
        age = (t - proof).total_seconds() if proof is not None else None

        created = parse_dt(job.get("created_at"))
        next_run = parse_dt(job.get("next_run_at"))
        # Suppress for non-defeat cases:
        #  (a) too young to judge (created < MIN_JOB_AGE) and never fired, OR
        #  (b) younger than its own first due-window (age < period) — a job whose
        #      first scheduled occurrence has not yet come due cannot be "missed".
        #  (c) LOW-FREQUENCY GUARD: the scheduler has pushed next_run_at into the
        #      future relative to now. A stale last_run with a future next_run
        #      means the next occurrence has not yet come due — the job is healthy
        #      and waiting, not missed (verified 2026-08-11: jarvis monthly jobs
        #      last fired Aug 1 but next_run_at is Sept 1; the scheduler recomputes
        #      next_run only on fire, so a healthy low-freq job looks stale here).
        if created is not None and proof is None and (t - created) < MIN_JOB_AGE:
            scanned.append(f"{profile}/{name}")
            continue  # never fired and not old enough to be due yet
        if created is not None and proof is None and (t - created) < threshold:
            scanned.append(f"{profile}/{name}")
            continue  # first occurrence not yet due
        if proof is None and next_run is not None and next_run > t:
            scanned.append(f"{profile}/{name}")
            continue  # next occurrence hasn't come due yet (low-frequency / monthly job)
        if proof is not None and next_run is not None and next_run > t and age is not None and age <= threshold.total_seconds():
            scanned.append(f"{profile}/{name}")
            continue  # healthy low-freq job: last run within threshold AND next is future

        if proof is None or age is None or age > threshold.total_seconds():
            if proof is None:
                detail = f"no completed execution AND no fresh last_run_at ever recorded"
            else:
                detail = f"last proof of completed run {age/3600:.1f}h ago > threshold {threshold.total_seconds()/3600:.1f}h"
            findings.append({
                "class": "MISSED",
                "profile": profile, "job_id": jid, "name": name,
                "period_h": round(period / 3600, 2),
                "last_completed": last_completed.isoformat() if last_completed else None,
                "last_run_at": job.get("last_run_at"),
                "last_status": last_status,
                "detail": detail,
            })

        # --- Script-not-found pre-flight (t_027a2bc9) ---
        if not script_resolves(profile, job):
            findings.append({
                "class": "SCRIPT_MISSING",
                "profile": profile, "job_id": jid, "name": name,
                "script": job.get("script"),
                "detail": f"script '{job.get('script')}' missing from profile-local and global scripts dirs — will fail 'Script not found' every tick",
            })
        scanned.append(f"{profile}/{name}")
    if con is not None:
        con.close()


def run_scan() -> tuple[list[dict], list[str], list[str]]:
    findings: list[dict] = []
    scanned: list[str] = []
    suspended: list[str] = []
    cron_dirs = sorted(HERMES_HOME.glob("profiles/*/cron")) + [HERMES_HOME / "cron"]
    seen: set[str] = set()
    for cron_dir in cron_dirs:
        if not cron_dir.is_dir():
            continue
        real = str(cron_dir.resolve())
        if real in seen:
            continue
        seen.add(real)
        check_store(cron_dir, findings, scanned, suspended)
    return findings, scanned, suspended


def _selftest() -> int:
    """Hermetic self-test wrapper (ESTOP-leak class, Frank 2026-08-28).

    Runs the deterministic tests against a THROWAWAY HERMES_HOME (temp dir) so
    no self-test write — ESTOP sentinels, *.ESTOP variants, fake probe scripts,
    synthetic cron stores — can ever land in a real profile dir. The previous
    structure wrote sentinels under the real ~/.hermes/profiles/... during the
    ESTOP-skip assertions and did not reliably clean up; a stray reason:null
    sentinel froze the jarvis profile (cron dispatch silently skipped) on
    2026-08-28 14:56. try/finally guarantees HERMES_HOME restoration AND temp
    dir removal on every exit path (pass or fail).
    """
    import shutil
    import tempfile
    _orig_home = HERMES_HOME
    tmp = Path(tempfile.mkdtemp())
    globals()["HERMES_HOME"] = tmp
    try:
        return _selftest_body()
    finally:
        globals()["HERMES_HOME"] = _orig_home
        shutil.rmtree(tmp, ignore_errors=True)


def _selftest_body() -> int:
    """Deterministic offline tests for the union liveness logic."""
    from datetime import datetime as _dt
    failures = []
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp())

    # 1. schedule_period_seconds: interval + cron.
    if schedule_period_seconds({"kind": "interval", "minutes": 15}) != 900:
        failures.append("interval period wrong")
    cron_p = schedule_period_seconds({"kind": "cron", "expr": "0 * * * *"})
    if cron_p != 3600:
        failures.append(f"hourly cron period wrong: {cron_p}")

    # 2. script_resolves: absolute and existing profile-local -> True.
    (HERMES_HOME / "profiles" / "zzselftest" / "scripts").mkdir(parents=True, exist_ok=True)
    fake = HERMES_HOME / "profiles" / "zzselftest" / "scripts" / "fake_probe.sh"
    fake.write_text("#!/bin/sh\necho hi\n")
    if not script_resolves("zzselftest", {"script": "fake_probe.sh"}):
        failures.append("existing profile-local script should resolve")
    if script_resolves("zzselftest", {"script": "no_such_script_xyz.py"}):
        failures.append("missing script should NOT resolve")
    fake.unlink(missing_ok=True)

    # 3. Simulate a store with a saturated executions.db (eviction) + fresh
    #    last_run_at (healthy low-frequency job) -> must NOT be MISSED.
    store = tmp / "cron"
    store.mkdir(parents=True)
    jobs = [{
        "id": "daily-job", "name": "daily-test", "enabled": True, "state": "scheduled",
        "schedule": {"kind": "cron", "expr": "35 1 * * *", "display": "35 1 * * *"},
        "created_at": "2026-06-01T00:00:00+01:00",
        "last_run_at": (_dt.now(timezone.utc) - timedelta(hours=20)).isoformat(),
        "last_status": "ok",
    }]
    (store / "jobs.json").write_text(json.dumps({"jobs": jobs, "updated_at": _dt.now(timezone.utc).isoformat()}))
    # executions.db exists but contains ONLY a row for another evicted job.
    import sqlite3
    con = sqlite3.connect(store / "executions.db")
    con.execute("CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY, job_id TEXT, source TEXT, process_id TEXT, pid INTEGER, process_started_at INTEGER, status TEXT, claimed_at TEXT, started_at TEXT, finished_at TEXT, error TEXT)")
    con.execute("INSERT INTO executions VALUES ('x','other-job','builtin','p',1,NULL,'completed','2026-08-01T00:00:00+00:00',NULL,NULL,NULL)")
    con.commit(); con.close()

    f, scanned, suspended = [], [], []
    check_store(store, f, scanned, suspended)
    misses = [x for x in f if x["class"] == "MISSED"]
    if misses:
        failures.append(f"healthy evicted daily job should NOT be MISSED, got {misses}")

    # 4. Truly stale job (no completed exec, stale last_run) -> MISSED.
    jobs[0]["last_run_at"] = (_dt.now(timezone.utc) - timedelta(days=3)).isoformat()
    (store / "jobs.json").write_text(json.dumps({"jobs": jobs, "updated_at": _dt.now(timezone.utc).isoformat()}))
    f2, _, _ = [], [], []
    check_store(store, f2, [], [])
    misses2 = [x for x in f2 if x["class"] == "MISSED"]
    if not misses2:
        failures.append("stale daily job should be MISSED")

    # 5. Script-not-found: enabled job with missing script -> SCRIPT_MISSING.
    jobs.append({"id": "s-1", "name": "scriptless", "enabled": True, "state": "scheduled",
                 "schedule": {"kind": "interval", "minutes": 60},
                 "script": "does_not_exist.py", "created_at": "2026-06-01T00:00:00+01:00",
                 "last_run_at": (_dt.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                 "last_status": "error"})
    (store / "jobs.json").write_text(json.dumps({"jobs": jobs, "updated_at": _dt.now(timezone.utc).isoformat()}))
    f3, _, _ = [], [], []
    check_store(store, f3, [], [])
    sm = [x for x in f3 if x["class"] == "SCRIPT_MISSING"]
    if not sm:
        failures.append("missing-script job should be SCRIPT_MISSING")

    # 6. ESTOP skip (task t_6c2abedd): a profile store under an active operator
    #    ESTOP must be reported SUSPENDED (excluded), never MISSED; once the
    #    sentinel is lifted, MISSED detection returns.
    import tempfile as _tf2
    home2 = Path(_tf2.mkdtemp())
    _orig_home = HERMES_HOME
    try:
        globals()["HERMES_HOME"] = home2
        pdir = home2 / "profiles" / "estopprofile"
        store2 = pdir / "cron"
        store2.mkdir(parents=True)
        jobs2 = [{
            "id": "stale-job", "name": "stale-estop", "enabled": True,
            "state": "scheduled",
            "schedule": {"kind": "interval", "minutes": 60},
            "created_at": "2026-06-01T00:00:00+01:00",
            "last_run_at": (_dt.now(timezone.utc) - timedelta(days=3)).isoformat(),
            "last_status": "ok",
        }]
        (store2 / "jobs.json").write_text(
            json.dumps({"jobs": jobs2, "updated_at": _dt.now(timezone.utc).isoformat()}))

        # (a) control: no sentinel -> stale job is MISSED, not suspended.
        fA, scA, susA = [], [], []
        check_store(store2, fA, scA, susA)
        if not [x for x in fA if x["class"] == "MISSED"]:
            failures.append("stale job in non-ESTOP profile should be MISSED")
        if susA:
            failures.append("no sentinel -> should not be SUSPENDED")

        # (b) exact ESTOP sentinel -> SUSPENDED, zero MISSED.
        (pdir / "ESTOP").write_text('{"reason":"operator test"}')
        fB, scB, susB = [], [], []
        check_store(store2, fB, scB, susB)
        if [x for x in fB if x["class"] == "MISSED"]:
            failures.append("ESTOP'd profile must NOT emit MISSED")
        if "estopprofile" not in susB:
            failures.append("ESTOP'd profile should be recorded as SUSPENDED")

        # (c) *.ESTOP variant name also suspends.
        (pdir / "ESTOP").unlink()
        (pdir / "variant.ESTOP").write_text("x")
        fC, scC, susC = [], [], []
        check_store(store2, fC, scC, susC)
        if [x for x in fC if x["class"] == "MISSED"] or "estopprofile" not in susC:
            failures.append("*.ESTOP variant should also SUSPEND")

        # (d) resume (lift sentinel) -> MISSED restored (acceptance #3).
        (pdir / "variant.ESTOP").unlink()
        fD, scD, susD = [], [], []
        check_store(store2, fD, scD, susD)
        if not [x for x in fD if x["class"] == "MISSED"]:
            failures.append("after resume, stale job should be MISSED again")
        if "estopprofile" in susD:
            failures.append("after resume, profile should NOT be SUSPENDED")
    finally:
        globals()["HERMES_HOME"] = _orig_home
        shutil.rmtree(home2, ignore_errors=True)

    if failures:
        print("SELFTEST_FAIL")
        for fl in failures:
            print(" -", fl)
        return 1
    print("SELFTEST_PASS union_liveness=ok eviction_handled=ok script_missing=ok estop_skip=ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--selftest" in argv:
        return _selftest()
    findings, scanned, suspended = run_scan()
    stamp = _now().strftime("%Y-%m-%dT%H:%MZ")
    if "--json" in argv:
        print(json.dumps({
            "monitor": "cron-liveness", "stamp": stamp,
            "grace_h": GRACE_H, "scanned": len(scanned),
            "suspended_profiles": suspended, "findings": findings,
        }, sort_keys=True))
    else:
        if suspended:
            print(f"  NOTE: {len(suspended)} profile(s) under active ESTOP — skipped: "
                  f"{', '.join(sorted(suspended))}")
        if findings:
            print(f"CRON LIVENESS {stamp} — {len(findings)} finding(s) across {len(scanned)} enabled recurring job(s) scanned:")
            for f in findings:
                print(f"  {f['class']} profile={f['profile']} job={f['name']} [{f['job_id']}]")
                print(f"      {f.get('detail','')}")
        else:
            print(f"CRON LIVENESS {stamp} — healthy ({len(scanned)} enabled recurring jobs scanned)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
