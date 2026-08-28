#!/usr/bin/env python3
# CANONICAL SOURCE — unified fleet health probe.
#
# Consolidates four overlapping infra-liveness probes into ONE deterministic,
# read-only, no-agent probe with a SINGLE escalation path:
#   - dgx-jarvis-health-canary      (hermes CLI + gateway systemd)
#   - jarvis-gateway-liveness-collector (gateway state + cron freshness)
#   - sycode-minimal-host-health     (docker health + disk)
#   - fleet-health-watchdog          (read-only parts: kanban crashes, fallback,
#                                     gateway, disk, cron failures)
#
# jarvis-daily-mechanism-liveness is folded in READ-ONLY: its mechanism
# matrix is classified here so there is one place to look. Its gated
# repair-card creation path stays a SEPARATE agent job (only escalation
# output is emitted here) — we never hand-create kanban cards from a probe.
#
# SINGLE ESCALATION PATH: `hermes send discord:#fleet-reports` (uses stored
# credentials, needs no running gateway). Absence of issues => silent (exit 0,
# JSONL record only). Presence of issues => send one consolidated alert and
# exit non-zero so the scheduler can also surface a delivery error if send fails.
from __future__ import annotations

import datetime as dt
import errno
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
HOME = Path("/home/frank")
HERMES = "/home/frank/.local/bin/hermes"
PROFILE = "jarvis"
GATEWAY_UNIT = "hermes-gateway-jarvis.service"
GATEWAY_STATE = HOME / ".hermes" / "profiles" / "jarvis" / "gateway_state.json"
CRON_OUTPUT = HOME / ".hermes" / "profiles" / "jarvis" / "cron" / "output"
LEGCY_CANARY = CRON_OUTPUT / "health_canary.jsonl"
UNIFIED_LOG = CRON_OUTPUT / "unified_health_canary.jsonl"
DISK_WATCHDOG = HOME / ".hermes" / "scripts" / "dgx_disk_space_watchdog.sh"
MECH_COLLECT = HOME / ".hermes" / "scripts" / "jarvis_mechanism_liveness_collect.py"
BOARDS_DIR = HOME / ".hermes" / "kanban" / "boards"
JARVIS_OS_KANBAN_DB = BOARDS_DIR / "jarvis-os" / "kanban.db"
SYCODE_TRADING_KANBAN_DB = BOARDS_DIR / "sycode-trading" / "kanban.db"
DEVOPS_READY_ASSIGNEES = ("devops",)
# Pseudo / test-only assignees that are NOT real dispatchable profiles. A
# task assigned to one of these can never be legitimately dispatched to a real
# worker, so any crash/gave_up on it is a TEST HARNESS ARTIFACT (e.g. the
# dead-PID e2e harness that leaks tasks onto the live jarvis-os board with a
# mock claimer like ``spark-4be3:mock`` and a fake pid), never a real infra
# failure. Such crashes must never drive the fleet verdict to BLOCK
# (t_9762d1e7). Verified: ``worker`` has no profile on disk and all active
# tasks assigned to it are e2e/soak test residue.
PSEUDO_TEST_ASSIGNEES = ("worker",)
READY_BACKLOG_TOP_LIMIT = 5
# oldest_ready_days above this on ANY tracked board degrades PASS -> WARN
# (t_bf11a0ce): the probe run 2026-08-03 showed a green fleet verdict while
# sycode-trading held a 24.4d-old approved review awaiting PR merge — a 24d
# stalled P0a that the probe's jarvis-only backlog metric never surfaced.
READY_BACKLOG_WARN_DAYS = 7
ALERT_TARGET = "discord:#fleet-reports"
CRON_STALE_MIN = 35          # cron ticker considered stale past this
CANARY_STALE_MIN = 40        # newest INDEPENDENT health_canary.jsonl write
                             # considered stale past this. The probe's own
                             # substrate_source bridge records are excluded so
                             # the check cannot grade its own liveness (t_0050991e).
CRASH_LOOKBACK_MIN = 60      # kanban crash/gave_up window
# A crashed/gave_up run is "superseded" (observed, NOT a BLOCK cause) when the
# owning task holds a NEWER run that is genuinely live: status='running', an
# alive worker pid, and a heartbeat within this many seconds. 15m is well below
# the dispatcher's reclaim window yet far above a busy agent's normal heartbeat
# cadence, so a productive in-flight run is never mistaken for a stale crash
# while a genuinely dead run (no recent heartbeat) still blocks (t_047d91e7).
RUN_LIVE_HEARTBEAT_MAX_SEC = 900

# Cron forced-release observability (t_615aa245): the scheduler mirrors every
# stale in-flight claim it force-releases to <hermes_home>/cron/
# inflight_forced_releases.jsonl (cron/scheduler.py _record_forced_release;
# also exposed in-process via get_inflight_guard_stats()['forced_releases']).
# This probe is the OUT-OF-PROCESS reader of that mirror so a wedged cron job
# is visible in-cycle (a forced release means a cron job wedged past its
# allowance and was recovered by the stale sweep) instead of only when a
# downstream liveness key goes dead hours later. Read-only: the probe never
# writes this file.
CRON_FORCED_RELEASES_LOG = (
    HOME / ".hermes" / "profiles" / "jarvis" / "cron"
    / "inflight_forced_releases.jsonl"
)
# Rolling window (hours) inside which releases count toward the verdict.
CRON_FORCED_RELEASE_WINDOW_H = 24
# >= 1 recent release degrades PASS -> WARN (a wedge happened — attention).
CRON_FORCED_RELEASE_WARN_MIN = 1
# >= this many recent releases => BLOCK: repeated wedges mean the scheduler
# keeps losing in-flight claims to the stale sweep (a crash class, not noise).
CRON_FORCED_RELEASE_BLOCK_MIN = 3

# Stale source=direct execution-row observability (t_84b68726): the cron
# scheduler's per-tick terminalizer (cron/executions.py
# terminalize_stale_executions) reclaims execution rows whose owner pid is
# provably dead and older than this age, marking them 'unknown' with an error
# note and mirroring a row to <profile>/cron/stale_terminalized.jsonl. This
# probe is the OUT-OF-PROCESS reader of the residual LITMUS: it scans every
# profile's executions.db for the fleet-wide zombie predicate
# (status='running' AND started_at > 2h AND finished_at IS NULL) and WARNs when
# a stale row still exists — i.e. the terminalizer has NOT cleared it yet
# (gateway down / sweep not running). Read-only: the probe never writes these.
CRON_STALE_DIRECT_MIN_AGE_H = 2
PROFILES_DIR = HOME / ".hermes" / "profiles"
# A stale row surviving past this many probe cycles is suspicious enough to
# WARN; a single fresh one may simply predate the gateway's next tick.
CRON_STALE_DIRECT_WARN_MIN = 1

# Repeat-BLOCK hard-alert escalation (t_7a97ba51 proposal #3 / t_cafc1119 C3).
# If the canary re-emits BLOCK >= CRITICAL_ALERT_MIN_COUNT times within
# CRITICAL_ALERT_WINDOW_H hours, escalate BEYOND #fleet-reports to
# #critical-alerts. This closes the gap where a recurring crash class (e.g. the
# rc=0-no-signal worker-exit class owned by t_61f0e7e6) re-BLOCKs the canary
# every cycle without ever reaching a higher-severity channel. Read-only except
# for a small append-only JSON state file (same dir as the other canary logs).
CRITICAL_ALERT_TARGET = "discord:#critical-alerts"
CRITICAL_ALERT_WINDOW_H = 24      # rolling window for repeat detection
CRITICAL_ALERT_MIN_COUNT = 3      # >= this many BLOCKs in window => escalate
CRITICAL_ALERT_STATE = CRON_OUTPUT / "unified_health_block_history.jsonl"

# Fork pressure mitigation — absorb transient EAGAIN fork() failures.
# On Linux, a fork()/clone() under memory/swap pressure (or PID/cgroup
# accounting saturation) can raise BlockingIOError/Resource temporarily
# unavailable (errno 11, EAGAIN) from subprocess.Popen. This is a transient
# resource signal, NOT a code bug — so the probe must retry with backoff and,
# if it still cannot fork after exhausting retries, surface a clearly-flagged
# DEGRADED verdict (rc=0, probe continues) instead of crashing. See kanban
# task t_b39441a2 (CEO governor pressure card, 2026-07-13).
FORK_RETRY_ATTEMPTS = 2          # extra attempts after the first failure (3 total)
FORK_RETRY_BACKOFF_S = 1.0       # seconds; grows per attempt (1s, then 2s)

# errno 11 == EAGAIN on Linux (the value raised as BlockingIOError by Popen).
_EAGAIN = getattr(errno, "EAGAIN", 11)

SECRET_PATTERNS = [
    re.compile(r"ctx7sk-[A-Za-z0-9-]+"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def run(argv: list[str], timeout: int = 25) -> dict[str, Any]:
    """Run a subprocess, absorbing transient fork()/resource-pressure failures.

    Returns a result dict with keys: rc, out, err, timeout, and
    fork_resource_pressure (bool). On a transient EAGAIN fork() failure the
    call is retried with backoff; if retries are exhausted the dict is flagged
    fork_resource_pressure=True (rc=None) so the caller can emit a DEGRADED
    verdict rather than crashing the whole probe.
    """
    attempt = 0
    while True:
        try:
            proc = subprocess.run(
                argv, text=True, capture_output=True, timeout=timeout, check=False
            )
            return {
                "rc": proc.returncode,
                "out": proc.stdout or "",
                "err": proc.stderr or "",
                "timeout": False,
                "fork_resource_pressure": False,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "rc": None,
                "out": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
                "err": (exc.stderr or "") if isinstance(exc.stderr, str) else "",
                "timeout": True,
                "fork_resource_pressure": False,
            }
        except FileNotFoundError as exc:
            return {
                "rc": None,
                "out": "",
                "err": str(exc),
                "timeout": False,
                "fork_resource_pressure": False,
            }
        except (BlockingIOError, OSError) as exc:
            # Transient fork()/resource-pressure failure (errno 11 EAGAIN, or
            # other transient OS errors surfaced from clone/exec). Retry with
            # incremental backoff before giving up.
            errno_val = getattr(exc, "errno", None)
            if errno_val == _EAGAIN and attempt < FORK_RETRY_ATTEMPTS:
                attempt += 1
                time.sleep(FORK_RETRY_BACKOFF_S * attempt)
                continue
            # Exhausted retries (or non-retryable errno) -> flagged DEGRADED.
            return {
                "rc": None,
                "out": "",
                "err": (
                    f"fork_resource_pressure: {type(exc).__name__} "
                    f"errno={errno_val} after {attempt} retry(ies): {exc}"
                ),
                "timeout": False,
                "fork_resource_pressure": True,
            }


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------------
def check_hermes_cli() -> tuple[bool, str, bool]:
    r = run([HERMES, "-p", PROFILE, "--version"], timeout=30)
    ok = (r["rc"] == 0) and not r["timeout"]
    return ok, f"hermes -p {PROFILE} --version rc={r['rc']} timeout={r['timeout']}", bool(r.get("fork_resource_pressure"))


def check_gateway_unit() -> tuple[bool, str, bool]:
    r = run(["systemctl", "--user", "is-active", GATEWAY_UNIT], timeout=10)
    active = (r["rc"] == 0) and (r["out"].strip() == "active")
    return active, f"systemctl is-active={r['out'].strip()!r} rc={r['rc']}", bool(r.get("fork_resource_pressure"))


def check_gateway_runtime() -> tuple[bool, bool, str]:
    """Returns (present_ok, pid_match, detail). present_ok True if exists+running."""
    data = load_json(GATEWAY_STATE)
    if data is None:
        return False, False, f"gateway_state.json missing/unparseable at {GATEWAY_STATE}"
    state = data.get("gateway_state")
    pid = data.get("pid")
    ok = state == "running"
    detail = f"gateway_state={state!r} pid={pid}"
    return ok, True, detail


def check_cron_ticker() -> tuple[bool, str, bool]:
    """Probe the gateway cron ticker. A single transient stall (the gateway
    ticker momentarily stops firing jobs, then self-heals) must not flip the
    whole fleet to BLOCK — retry once with a longer timeout so a brief stall is
    absorbed. A genuine outage fails both attempts and is still reported.
    See kanban t_631685fb (periodic ~20-30m ticker stalls observed 2026-07-25)."""
    last_detail = "cron status never returned"
    last_fork = False
    for attempt in (1, 2):
        r = run([HERMES, "-p", PROFILE, "cron", "status"], timeout=40)
        last_fork = bool(r.get("fork_resource_pressure"))
        if r["timeout"] or r["rc"] != 0:
            last_detail = f"cron status rc={r['rc']} timeout={r['timeout']}"
            if attempt == 1:
                time.sleep(2)  # absorb a transient stall before concluding
                continue
            return False, last_detail, last_fork
        text = r["out"] + "\n" + r["err"]
        if "Gateway is running" not in text:
            return False, "cron status did not report 'Gateway is running'", last_fork
        if not re.search(r"\b\d+ active job\(?s\)?", text):
            return False, "cron active job count line not found", last_fork
        return True, "cron ticker reports running with active jobs", last_fork
    return False, last_detail, last_fork


def check_canary_freshness() -> tuple[bool, str]:
    """Verify an INDEPENDENT writer is still refreshing health_canary.jsonl.

    The unified probe itself appends a substrate bridge record to this same
    file every cycle (write_legacy_substrate_record, substrate_source=
    "unified-health-probe") for legacy downstream consumers. The check must
    NOT grade the probe's own liveness — that is circular and self-BLOCKs the
    first run after any probe outage >CANARY_STALE_MIN with no real fleet
    defect present (incident 2026-08-01, t_0050991e). So we read the file
    content and consider only records whose writer is NOT this probe, taking
    the most recent independent timestamp as the freshness signal.

    Independent writers observed in the file: data-freshness-probe,
    per-entity-freshness, news-macro-integrity-sentinel, and the historical
    legacy records (no substrate_source / not from this probe). If the file is
    absent or has no independent records yet, the check is informational-only
    (returns ok=True) rather than a hard BLOCK, because an empty/independent-
    only stream is not evidence of an active fleet failure.
    """
    if not LEGCY_CANARY.exists():
        return True, "legacy health_canary.jsonl absent (probe not yet run historically)"
    try:
        last_indep: dt.datetime | None = None
        indep_sources: set[str] = set()
        for line in LEGCY_CANARY.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            # Skip the probe's own bridge record — it cannot vouch for itself.
            if rec.get("substrate_source") == LEGACY_SUBSTRATE_SOURCE:
                continue
            ts = rec.get("ts")
            if not isinstance(ts, str):
                continue
            try:
                t = dt.datetime.fromisoformat(ts)
            except Exception:
                continue
            indep_sources.add(str(rec.get("substrate_source") or rec.get("source") or "<legacy>"))
            if last_indep is None or t > last_indep:
                last_indep = t
    except Exception as exc:
        return False, f"cannot read legacy canary: {exc}"

    if last_indep is None:
        # No independent writer has ever recorded here. That is a monitoring
        # gap, not an infra outage — do NOT BLOCK the fleet on it.
        return True, "no independent health_canary writer has recorded yet (informational only)"
    age_min = (utc_now() - last_indep).total_seconds() / 60.0
    if age_min > CANARY_STALE_MIN:
        return False, (
            f"last INDEPENDENT health_canary write {age_min:.1f}m ago "
            f"(> {CANARY_STALE_MIN}m); independent sources seen: {sorted(indep_sources)}"
        )
    return True, (
        f"last INDEPENDENT health_canary write {age_min:.1f}m ago "
        f"(sources: {sorted(indep_sources)})"
    )


def check_cron_forced_releases(now: dt.datetime | None = None) -> dict[str, Any]:
    """Read the scheduler's forced-release mirror (read-only, fail-open).

    The scheduler appends one JSON row per forced release:
      {"job_id": ..., "name": ..., "age_seconds": ..., "allowance_seconds": ...,
       "at": <iso>}
    (cron/scheduler.py ``_record_forced_release``, event=forced_release).  A
    forced release is a stale in-flight claim the sweep cut — i.e. a cron job
    wedged past its allowance and was recovered without a gateway restart.

    Fail-open contract (t_615aa245): an absent file means no release has ever
    been recorded (healthy); an unreadable or partially-corrupt file degrades
    to count-0 telemetry. The probe never BLOCKs the fleet on a monitoring gap.

    Returns:
      available — file exists and was parsed (or exists but window empty)
      count     — releases inside CRON_FORCED_RELEASE_WINDOW_H
      recent    — newest releases, newest first (job_id/name/at/age_seconds)
      block     — count >= CRON_FORCED_RELEASE_BLOCK_MIN (repeated wedges)
      warn      — 1 <= count < block threshold (single/rare wedge)
      detail    — human-readable summary
    """
    now = now or utc_now()
    if not CRON_FORCED_RELEASES_LOG.exists():
        return {
            "available": False, "count": 0, "recent": [],
            "block": False, "warn": False,
            "detail": "inflight_forced_releases.jsonl absent — no forced release "
                      "recorded by the scheduler yet",
        }
    cutoff = now.timestamp() - CRON_FORCED_RELEASE_WINDOW_H * 3600
    records: list[dict[str, Any]] = []
    try:
        for line in CRON_FORCED_RELEASES_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue  # skip a corrupt row; never fail the probe on it
            at = rec.get("at")
            if not isinstance(at, str):
                continue
            try:
                t = dt.datetime.fromisoformat(at)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=dt.timezone.utc)
            except Exception:
                continue
            if t.timestamp() >= cutoff:
                records.append({
                    "job_id": rec.get("job_id"),
                    "name": rec.get("name"),
                    "at": at,
                    "age_seconds": rec.get("age_seconds"),
                    "allowance_seconds": rec.get("allowance_seconds"),
                })
    except Exception as exc:
        return {
            "available": False, "count": 0, "recent": [],
            "block": False, "warn": False,
            "detail": f"cannot read forced-release mirror "
                      f"{CRON_FORCED_RELEASES_LOG}: {type(exc).__name__}: {exc}",
        }
    records.sort(key=lambda r: r["at"], reverse=True)
    count = len(records)
    block = count >= CRON_FORCED_RELEASE_BLOCK_MIN
    warn = (not block) and count >= CRON_FORCED_RELEASE_WARN_MIN
    if not records:
        detail = (f"no forced release within {CRON_FORCED_RELEASE_WINDOW_H}h "
                  f"(log present at {CRON_FORCED_RELEASES_LOG.name})")
    else:
        recent_names = ", ".join(
            str(r["name"] or r["job_id"] or "?") for r in records[:3]
        )
        detail = (f"{count} forced release(s) within "
                  f"{CRON_FORCED_RELEASE_WINDOW_H}h: {recent_names}")
        if block:
            detail += (f"  <-- BLOCK cause: repeated cron wedges "
                       f"(>= {CRON_FORCED_RELEASE_BLOCK_MIN} releases)")
        elif warn:
            detail += "  (single/rare wedge — attention, not an outage)"
    return {
        "available": True,
        "count": count,
        "recent": records[:10],
        "block": block,
        "warn": warn,
        "detail": detail,
    }


def check_cron_stale_direct_rows() -> dict[str, Any]:
    """Scan every profile's executions.db for stale running rows (read-only).

    The acceptance litmus for t_84b68726 is the fleet-wide zombie scan
    ``status='running' AND started_at > 2h AND finished_at IS NULL`` — the same
    predicate the scheduler's per-tick terminalizer
    (``cron/executions.py::terminalize_stale_executions``) reclaims. This
    probe is the OUT-OF-PROCESS reader of that predicate: it WARNs when a stale
    running row still exists after the gateway's sweep has had a chance to run
    (i.e. the terminalizer is not clearing it, or its owning gateway is down).

    Fail-open contract: an absent/unreadable executions.db degrades to count-0
    for that profile and never BLOCKs the fleet on a monitoring gap. Never a
    BLOCK cause on its own — stale rows are a self-healing class once the
    terminalizer runs, so a persistent residue degrades PASS -> WARN only.

    Returns:
      scanned     — number of profile executions.db files read
      count       — total stale running rows across all profiles
      by_profile  — profile name -> stale row count (profiles with residue)
      warn        — count >= CRON_STALE_DIRECT_WARN_MIN
      detail      — human-readable summary
    """
    cutoff = (utc_now() - dt.timedelta(hours=CRON_STALE_DIRECT_MIN_AGE_H)).isoformat()
    scanned = 0
    by_profile: dict[str, int] = {}
    if not PROFILES_DIR.is_dir():
        return {
            "scanned": 0, "count": 0, "by_profile": {}, "warn": False,
            "detail": f"profiles dir {PROFILES_DIR} absent — cannot scan",
        }
    for db in sorted(PROFILES_DIR.glob("*/cron/executions.db")):
        profile = db.parent.parent.name
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            try:
                row = con.execute(
                    "SELECT COUNT(*) FROM executions "
                    "WHERE status='running' AND started_at IS NOT NULL "
                    "AND finished_at IS NULL "
                    "AND datetime(started_at) < datetime(?)",
                    (cutoff,),
                ).fetchone()
            finally:
                con.close()
        except Exception:
            continue  # unreadable/locked DB fails open for that profile
        scanned += 1
        n = int(row[0]) if row and row[0] else 0
        if n:
            by_profile[profile] = n
    count = sum(by_profile.values())
    warn = count >= CRON_STALE_DIRECT_WARN_MIN
    if not scanned:
        detail = "no profile executions.db found to scan"
    elif count == 0:
        detail = (f"no stale running cron execution rows across {scanned} "
                  f"profiles (status=running AND started_at > "
                  f"{CRON_STALE_DIRECT_MIN_AGE_H}h AND finished_at IS NULL)")
    else:
        detail = (f"{count} stale running cron execution row(s) across {scanned} "
                  f"profiles: {sorted(by_profile)} — the per-tick terminalizer "
                  f"has NOT cleared them (sweep/gateway may be down)")
    return {"scanned": scanned, "count": count, "by_profile": by_profile,
            "warn": warn, "detail": detail}


def check_docker() -> tuple[bool, str, bool]:
    probe_r = run(["command", "-v", "docker"])
    if not (Path("/usr/bin/docker").exists() or probe_r["rc"] == 0):
        return True, "docker not installed (skipped)", bool(probe_r.get("fork_resource_pressure"))
    r = run(["docker", "ps", "--filter", "health=unhealthy",
              "--format", "{{.Names}}={{.Status}}"], timeout=20)
    if r["rc"] != 0:
        return True, f"docker unhealthy query failed rc={r['rc']} (non-fatal)", bool(r.get("fork_resource_pressure"))
    out = r["out"].strip()
    if not out:
        return True, "no unhealthy containers", bool(r.get("fork_resource_pressure"))
    return False, f"unhealthy containers: {out}", bool(r.get("fork_resource_pressure"))


def check_disk() -> tuple[bool, str, bool]:
    if not DISK_WATCHDOG.exists():
        return True, "disk watchdog script absent (skipped)", False
    r = run(["bash", str(DISK_WATCHDOG)], timeout=20)
    out = (r["out"] + r["err"]).strip()
    if not out:
        return True, "disk within thresholds", bool(r.get("fork_resource_pressure"))
    return False, out.replace("\n", " | "), bool(r.get("fork_resource_pressure"))


def check_mechanism_matrix() -> dict[str, Any]:
    """Fold in jarvis-daily-mechanism-liveness read-only classification."""
    if not MECH_COLLECT.exists():
        return {"available": False, "detail": "mechanism collector absent",
                "fork_resource_pressure": False}
    r = run([sys.executable, str(MECH_COLLECT)], timeout=60)
    if r["timeout"] or r["rc"] != 0 or not r["out"].strip():
        return {"available": True, "ok": False,
                "detail": f"collector rc={r['rc']} timeout={r['timeout']} err={r['err'][:200]}",
                "fork_resource_pressure": bool(r.get("fork_resource_pressure"))}
    try:
        rep = json.loads(r["out"])
        summary = rep.get("summary", {})
        return {
            "available": True,
            "overall": summary.get("overall"),
            "ok": summary.get("ok", 0),
            "dead": summary.get("dead", 0),
            "warn": summary.get("warn_visibility", 0),
            "dead_keys": summary.get("dead_keys", []),
            "detail": "mechanism matrix classified",
            "fork_resource_pressure": bool(r.get("fork_resource_pressure")),
        }
    except Exception as exc:
        return {"available": True, "ok": False, "detail": f"parse error: {exc}",
                "fork_resource_pressure": False}


# Task statuses that mean the owning task is still live / actionable.
# Anything else (blocked/done/archived/scheduled/todo/triage) is a parked or
# resolved task — a crash/gave_up on it is a *historical* event, not a live
# infra failure, and must not pin the global verdict.
ACTIVE_TASK_STATES = ("running", "ready")


# --- Repeat-BLOCK hard-alert escalation (t_7a97ba51 #3 / t_cafc1119 C3) -------
def record_block_event(ts: dt.datetime) -> int:
    """Persist this BLOCK event to the rolling state file and return the
    number of BLOCKs recorded within the trailing CRITICAL_ALERT_WINDOW_H.

    Append-only + read-only resilience: a write failure must never break the
    probe, and a read failure degrades to 'unknown' (count 0) rather than
    crashing. Called exactly once per BLOCK verdict.
    """
    try:
        CRITICAL_ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
        with CRITICAL_ALERT_STATE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": ts.isoformat()}) + "\n")
    except Exception:
        pass
    return _count_recent_blocks(ts)


def _count_recent_blocks(now: dt.datetime) -> int:
    """Count BLOCK events within CRITICAL_ALERT_WINDOW_H hours of `now`."""
    cutoff = now.timestamp() - CRITICAL_ALERT_WINDOW_H * 3600
    count = 0
    try:
        if not CRITICAL_ALERT_STATE.exists():
            return 0
        for line in CRITICAL_ALERT_STATE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                ev_ts = dt.datetime.fromisoformat(ev["ts"]).timestamp()
            except Exception:
                continue
            if ev_ts >= cutoff:
                count += 1
    except Exception:
        return count
    return count


def critical_alert_due(now: dt.datetime) -> bool:
    """True once BLOCK has re-occurred >= CRITICAL_ALERT_MIN_COUNT times in the
    trailing window — i.e. a crash class is re-BLOCKing the canary repeatedly.
    """
    return record_block_event(now) >= CRITICAL_ALERT_MIN_COUNT


def _pid_alive(pid: int | None) -> bool:
    """True if a kanban worker pid maps to a live process.

    None (no pid recorded) and any process that no longer exists => False.
    A PermissionError means the pid exists but is owned by another user; we
    treat it as alive (the worker is still resident)."""
    if pid is None:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, OverflowError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def _task_crash_superseded(cur, task_id: str, crash_run_id: int, now: int) -> bool:
    """True when a crashed/gave_up run on `task_id` is superseded by a LATER
    lifecycle on the same task.

    A prior crashed/gave_up attempt is NOT a live fleet failure (and must not
    pin the verdict to BLOCK) when the SAME task has since either:
      * reached a terminal completed/done lifecycle via a LATER run
        (status='done', outcome='completed') — the task was successfully
        resolved by a subsequent attempt (t_e27d602a: the crash->successful
        retry->done shape), OR
      * been retried and the newer run is genuinely in flight —
        status='running', an alive worker pid, and a recent heartbeat
        (t_047d91e7: a productive retry aging out a false BLOCK).

    Only runs strictly NEWER than the crash run (id greater) count as
    superseding, so a genuine unresolved crash on a still-active task with no
    later success or live retry still BLOCKs. Any of the checks failing (no
    newer run, dead pid, no/stale heartbeat) returns False so the task stays a
    BLOCK.
    """
    try:
        row = cur.execute(
            "SELECT status, worker_pid, last_heartbeat_at FROM task_runs "
            "WHERE task_id = ? AND id > ? "
            "ORDER BY id DESC LIMIT 1",
            (task_id, crash_run_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    if not row:
        return False
    status, pid, hb = row
    if status == "done":
        # The task was resolved by a later successful/terminal run — the crash
        # is a historical event, not a live failure.
        return True
    if status != "running" or not _pid_alive(pid):
        return False
    if hb is None:
        return False
    return (now - int(hb)) <= RUN_LIVE_HEARTBEAT_MAX_SEC


def check_kanban_crashes() -> tuple[int, list[str], int, list[str]]:
    """Read-only scan for recent crashed/gave_up runs across boards.

    Crash-signal precision (task t_7b1ceb0f): a run counts as a FRESH/active
    infra failure only when BOTH hold:
      * the run ended within CRASH_LOOKBACK_MIN, AND
      * the owning task is still active (running/ready).
    A crashed/gave_up run on an already-parked/resolved task (blocked/done/
    archived/scheduled/todo/triage/transient) is STALE — it already happened
    and the task is owned/resolved. Re-counting it every cycle keeps the whole
    fleet verdict at BLOCK and drowns out real signals. Stale runs are counted
    for transparency but do NOT drive the verdict.

    Newer-live-run supersession (t_047d91e7): even on an ACTIVE task, a prior
    crashed/gave_up attempt is SUPERSEDED (observed, NOT a BLOCK cause) when
    the same task has a newer run that is still genuinely in flight
    (status='running', alive pid, recent heartbeat). Without this the fleet
    stays at BLOCK while a productive retry is running — an aging-out false
    BLOCK, not an infra failure.
    """
    if not BOARDS_DIR.exists():
        return 0, [], 0, []
    now = int(utc_now().timestamp())
    cutoff = now - CRASH_LOOKBACK_MIN * 60
    active_hits: list[str] = []
    stale_count = 0
    superseded_hits: list[str] = []
    seen_task_ids: set[str] = set()
    for db in sorted(BOARDS_DIR.glob("*/kanban.db")):
        board = db.parent.name
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            cur = con.cursor()
            # task_runs + tasks tables exist in every board DB; LEFT JOIN so a
            # run whose task row is somehow missing is still observed (status
            # falls back to 'unknown' => treated as NOT active, i.e. stale).
            try:
                rows = cur.execute(
                    "SELECT tr.id, tr.task_id, tr.outcome, tr.ended_at, "
                    "COALESCE(t.status, 'unknown') AS task_status, "
                    "COALESCE(t.assignee, '') AS task_assignee, "
                    "EXISTS ("
                    "  SELECT 1 FROM task_links l "
                    "  JOIN tasks p ON p.id = l.parent_id "
                    "  WHERE l.child_id = tr.task_id AND p.status = 'done'"
                    ") AS has_done_parent "
                    "FROM task_runs tr "
                    "LEFT JOIN tasks t ON tr.task_id = t.id "
                    "WHERE tr.outcome IN ('crashed','gave_up') "
                    "AND tr.ended_at >= ? "
                    "ORDER BY tr.ended_at DESC LIMIT 25",
                    (cutoff,),
                ).fetchall()
            except sqlite3.OperationalError:
                # Some minimal test/minimal schema boards may lack the
                # ``tasks.assignee`` column. Fall back to the legacy query
                # shape (assignee '' => pseudo-test isolation inert) so the
                # crash scan still works on those boards.
                try:
                    rows = cur.execute(
                        "SELECT tr.id, tr.task_id, tr.outcome, tr.ended_at, "
                        "COALESCE(t.status, 'unknown') AS task_status, "
                        "'' AS task_assignee, "
                        "EXISTS ("
                        "  SELECT 1 FROM task_links l "
                        "  JOIN tasks p ON p.id = l.parent_id "
                        "  WHERE l.child_id = tr.task_id AND p.status = 'done'"
                        ") AS has_done_parent "
                        "FROM task_runs tr "
                        "LEFT JOIN tasks t ON tr.task_id = t.id "
                        "WHERE tr.outcome IN ('crashed','gave_up') "
                        "AND tr.ended_at >= ? "
                        "ORDER BY tr.ended_at DESC LIMIT 25",
                        (cutoff,),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            for task_id, outcome, ended, status, assignee, has_done_parent in rows:
                if status in ACTIVE_TASK_STATES and not has_done_parent:
                    # Test-harness isolation (t_9762d1e7): a crash/gave_up on a
                    # task assigned to a pseudo/test-only profile (e.g. the
                    # dead-PID e2e harness's ``worker`` assignee, which cannot
                    # be dispatched) is a TEST ARTIFACT leaked onto the board,
                    # not a real infra failure. Count it stale so it never
                    # drives the fleet verdict to BLOCK.
                    if assignee in PSEUDO_TEST_ASSIGNEES:
                        stale_count += 1
                        continue
                    # de-dupe by task_id so one task with several crashed/gave_up
                    # runs in the window is not double-counted.
                    if task_id not in seen_task_ids:
                        if _task_has_newer_live_run(cur, task_id, now):
                            superseded_hits.append(
                                f"{board}/{task_id}:{outcome} "
                                f"(superseded by newer live run)"
                            )
                        elif len(active_hits) < 8:
                            active_hits.append(
                                f"{board}/{task_id}:{outcome} (status={status})"
                            )
                    seen_task_ids.add(task_id)
                else:
                    stale_count += 1
            con.close()
        except Exception:
            continue
    return len(active_hits), active_hits, stale_count, superseded_hits


def _age_days(now: dt.datetime, created_at: int | None) -> float | None:
    if created_at is None:
        return None
    return round(max(0.0, now.timestamp() - int(created_at)) / 86400.0, 3)


def _scan_board_ready_backlog(db: Path, board_label: str,
                               now: dt.datetime | None = None) -> dict[str, Any]:
    """Read-only observability for a single board's ready backlog.

    This is intentionally NON-BLOCKING telemetry: old ready rows are useful for
    PM/devops routing, but backlog age alone must never drive the health probe
    to BLOCK. Missing/unreadable DBs are reported as unavailable telemetry, not
    infrastructure failures. The returned dict carries a ``warn`` flag the
    aggregator inspects to DEGRADE PASS->WARN (t_bf11a0ce) without ever BLOCKing.
    """
    now = now or utc_now()
    result: dict[str, Any] = {
        "available": False,
        "board": board_label,
        "db": str(db),
        "ready_total": 0,
        "oldest_ready_age_days": None,
        "devops_ready_count": 0,
        "oldest_devops_ready_age_days": None,
        "oldest_ready_task_id": None,
        "top_ready_ids": [],
        "top_devops_ready_ids": [],
        "warn": False,
        "detail": f"{board_label} kanban DB absent",
    }
    if not db.exists():
        return result
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        cur = con.cursor()
        ready_rows = cur.execute(
            "SELECT id, assignee, created_at FROM tasks "
            "WHERE status = 'ready' "
            "ORDER BY created_at ASC, id ASC"
        ).fetchall()
        con.close()
    except Exception as exc:
        result["detail"] = (
            f"{board_label} ready backlog scan failed: {type(exc).__name__}: {exc}"
        )
        return result

    devops_rows = [r for r in ready_rows if r[1] in DEVOPS_READY_ASSIGNEES]
    oldest_ready_age = _age_days(now, ready_rows[0][2]) if ready_rows else None
    oldest_devops_age = _age_days(now, devops_rows[0][2]) if devops_rows else None
    warn = bool(oldest_ready_age is not None and oldest_ready_age > READY_BACKLOG_WARN_DAYS)
    oldest_task_id = ready_rows[0][0] if ready_rows else None
    result.update({
        "available": True,
        "ready_total": len(ready_rows),
        "oldest_ready_age_days": oldest_ready_age,
        "oldest_ready_task_id": oldest_task_id,
        "devops_ready_count": len(devops_rows),
        "oldest_devops_ready_age_days": oldest_devops_age,
        "top_ready_ids": [r[0] for r in ready_rows[:READY_BACKLOG_TOP_LIMIT]],
        "top_devops_ready_ids": [r[0] for r in devops_rows[:READY_BACKLOG_TOP_LIMIT]],
        "warn": warn,
        "detail": (
            f"ready={len(ready_rows)} oldest_ready_days="
            f"{oldest_ready_age if oldest_ready_age is not None else 'n/a'} "
            f"oldest_ready_task={oldest_task_id}; "
            f"devops_ready={len(devops_rows)} oldest_devops_ready_days="
            f"{oldest_devops_age if oldest_devops_age is not None else 'n/a'}"
            f"{' [WARN: oldest_ready>'+str(READY_BACKLOG_WARN_DAYS)+'d]' if warn else ''}"
        ),
    })
    return result


def check_jarvis_ready_backlog(now: dt.datetime | None = None) -> dict[str, Any]:
    """Backward-compatible jarvis-os wrapper around _scan_board_ready_backlog."""
    return _scan_board_ready_backlog(JARVIS_OS_KANBAN_DB, "jarvis-os", now)


# ----------------------------------------------------------------------------
# Legacy substrate-signal bridge (t_39c29d42 residual / t_bd9d284e)
# ----------------------------------------------------------------------------
# The historical substrate-liveness signal (hermes_cli + gateway_runtime) lived
# in health_canary.jsonl, written by the now-consolidated + PAUSED
# dgx-jarvis-health-canary job (8827640671f9). That job is intentionally kept
# disabled; the unified probe owns substrate liveness now. To stop any legacy
# consumer (dgx_report_anomaly_detector.py GATEWAY_RULES, t_5311fb77 house-
# liveness channel, multi-day-spine-audit.py EXPECTED_LIVE_JOBS) from reading a
# frozen (~24h-stale) gateway_running field and silently believing substrate is
# healthy, this probe re-stamps a substrate record into health_canary.jsonl
# every cycle. The record keeps the SAME field shape the consumers expect
# (gateway_running: bool, hermes_cli: bool) plus a non-ambiguous
# "substrate_source": "unified-health-probe" marker so downstream readers can
# distinguish a live bridge record from the old canary and never mistake a
# stale legacy hermes_cli:true record for fresh substrate state. Writes to
# CRON_OUTPUT/health_canary.jsonl (same dir as unified log) so a test that
# redirects CRON_OUTPUT does not touch the live file.
LEGACY_SUBSTRATE_SOURCE = "unified-health-probe"


def write_legacy_substrate_record(checks, verdict, now):
    """Append a substrate-liveness record to the legacy health_canary.jsonl.

    Best-effort: any failure must never break the unified probe. Returns the
    written record dict, or None on failure.
    """
    legacy_path = CRON_OUTPUT / "health_canary.jsonl"
    try:
        by_name = {c["name"]: c for c in checks}
        hermes_cli_ok = bool(by_name.get("hermes_cli", {}).get("ok", False))
        gw_unit_ok = bool(by_name.get("gateway_unit", {}).get("ok", False))
        gw_rt_ok = bool(by_name.get("gateway_runtime", {}).get("ok", False))
        # "substrate up" requires the CLI usable AND the gateway actually running.
        gateway_running = gw_unit_ok and gw_rt_ok
        record = {
            "ts": now.isoformat(),
            "substrate_source": LEGACY_SUBSTRATE_SOURCE,
            "hermes_cli": hermes_cli_ok,
            "gateway_running": gateway_running,
            "verdict": verdict,
        }
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        with legacy_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        # Preserve the historical 5000-line rotation the paused canary used to
        # perform on this shared file (data-freshness-probe and
        # news-macro-integrity-sentinel also append here, and nothing else
        # rotates it now). Only rewrite when over the cap, matching the legacy
        # behavior, so we don't rewrite the file every cycle.
        try:
            lines = legacy_path.read_text(encoding="utf-8").splitlines()
            if len(lines) > 5000:
                legacy_path.write_text(
                    "\n".join(lines[-5000:]) + "\n", encoding="utf-8"
                )
        except Exception:
            pass
        return record
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Aggregation + output
# ----------------------------------------------------------------------------
def main() -> int:
    now = utc_now()
    checks: list[dict[str, Any]] = []

    hermes_ok, d, hermes_fork = check_hermes_cli()
    checks.append({"name": "hermes_cli", "ok": hermes_ok, "detail": d,
                   "fork_resource_pressure": hermes_fork})

    gw_unit_ok, d, gw_fork = check_gateway_unit()
    checks.append({"name": "gateway_unit", "ok": gw_unit_ok, "detail": d,
                   "fork_resource_pressure": gw_fork})

    gw_rt_ok, _, d = check_gateway_runtime()
    checks.append({"name": "gateway_runtime", "ok": gw_rt_ok, "detail": d,
                   "fork_resource_pressure": False})

    cron_ok, d, cron_fork = check_cron_ticker()
    checks.append({"name": "cron_ticker", "ok": cron_ok, "detail": d,
                   "fork_resource_pressure": cron_fork})

    canary_ok, d = check_canary_freshness()
    checks.append({"name": "canary_freshness", "ok": canary_ok, "detail": d})

    docker_ok, d, docker_fork = check_docker()
    checks.append({"name": "docker_health", "ok": docker_ok, "detail": d,
                   "fork_resource_pressure": docker_fork})

    disk_ok, d, disk_fork = check_disk()
    checks.append({"name": "disk_space", "ok": disk_ok, "detail": d,
                   "fork_resource_pressure": disk_fork})

    mech = check_mechanism_matrix()
    checks.append({"name": "mechanism_matrix", "ok": mech.get("available", False)
                   and mech.get("overall") in (None, "GREEN"), "detail": mech.get("detail", ""),
                   "fork_resource_pressure": bool(mech.get("fork_resource_pressure", False))})

    crash_count, crash_hits, stale_count, superseded_hits = check_kanban_crashes()
    superseded_count = len(superseded_hits)
    checks.append({"name": "kanban_crashes", "ok": crash_count == 0,
                   "detail": f"{crash_count} active crash/gave_up run(s) within "
                             f"{CRASH_LOOKBACK_MIN}m (stale/parked on resolved "
                             f"tasks: {stale_count}; superseded by newer live "
                             f"run: {superseded_count})",
                   "fork_resource_pressure": False})

    ready_backlog = check_jarvis_ready_backlog(now)  # jarvis-os (back-compat)
    checks.append({"name": "jarvis_ready_backlog", "ok": True,
                   "detail": ready_backlog.get("detail", ""),
                   "fork_resource_pressure": False,
                   "observability_only": True,
                   "warn": ready_backlog.get("warn", False)})

    # Per-board ready backlog (t_bf11a0ce): extend beyond jarvis-os so a stalled
    # approval/review on another active board (e.g. sycode-trading) is visible
    # in the fleet verdict instead of hiding behind a green probe.
    sycode_ready_backlog = _scan_board_ready_backlog(
        SYCODE_TRADING_KANBAN_DB, "sycode-trading", now)
    checks.append({"name": "sycode_trading_ready_backlog", "ok": True,
                   "detail": sycode_ready_backlog.get("detail", ""),
                   "fork_resource_pressure": False,
                   "observability_only": True,
                   "warn": sycode_ready_backlog.get("warn", False)})

    # Cron forced-release observability (t_615aa245): read the scheduler's
    # forced-release mirror. Repeated wedges (>= CRON_FORCED_RELEASE_BLOCK_MIN
    # releases in the window) drive BLOCK; a single/rare release degrades
    # PASS -> WARN. An absent/corrupt mirror fails open (never BLOCKs the
    # fleet on a monitoring gap).
    forced_releases = check_cron_forced_releases(now)
    checks.append({
        "name": "cron_forced_releases",
        "ok": not forced_releases.get("block", False),
        "detail": forced_releases.get("detail", ""),
        "fork_resource_pressure": False,
        "warn": forced_releases.get("warn", False),
    })
    forced_block = bool(forced_releases.get("block"))
    forced_warn = bool(forced_releases.get("warn"))

    # Stale source=direct execution-row observability (t_84b68726): WARN when a
    # stale running row (>2h, dead-owner class) survives across profiles — the
    # per-tick terminalizer has not cleared it. Never a BLOCK cause.
    stale_direct_rows = check_cron_stale_direct_rows()
    checks.append({
        "name": "cron_stale_direct_rows",
        # Mirror forced-releases: `ok` reflects only a hard `block` (always
        # False for stale rows — never a BLOCK cause); `warn` drives the
        # PASS->WARN verdict separately. So a stale row surfaces as WARN and
        # never lands in infra_failed.
        "ok": not stale_direct_rows.get("block", False),
        "detail": stale_direct_rows.get("detail", ""),
        "fork_resource_pressure": False,
        "warn": stale_direct_rows.get("warn", False),
    })
    stale_warn = bool(stale_direct_rows.get("warn"))

    # Boards whose oldest ready task exceeds READY_BACKLOG_WARN_DAYS (observability
    # only — never a BLOCK cause). Each entry: (board, oldest_task_id, age_days).
    warn_boards: list[tuple[str, str | None, float | None]] = []
    for _bl in (ready_backlog, sycode_ready_backlog):
        if _bl.get("warn"):
            _b: str = _bl.get("board") or "unknown"  # type: ignore[assignment]
            _tid: str | None = _bl.get("oldest_ready_task_id")
            _age: float | None = _bl.get("oldest_ready_age_days")
            warn_boards.append((_b, _tid, _age))

    # Fork resource pressure: a transient fork()/EAGAIN stall anywhere. This is
    # NOT a real infra failure and must NOT crash the probe or mask a genuine
    # infra verdict — it produces a DEGRADED verdict (rc=0) so liveness stays
    # observable. Fork-failed checks are reported (ok=False) but are excluded
    # from the infra_failed set so they never drive a BLOCK on their own.
    # See kanban t_b39441a2.
    fork_failed = [c["name"] for c in checks if c.get("fork_resource_pressure")]
    fork_pressure = bool(fork_failed)

    infra_failed = [c for c in checks
                    if not c["ok"] and not c.get("fork_resource_pressure")
                    and c["name"] not in ("mechanism_matrix",)]
    mech_red = bool(mech.get("overall") == "RED")
    blocked = bool(infra_failed) or mech_red or forced_block
    degraded = fork_pressure and not blocked

    record = {
        "ts": now.isoformat(),
        "profile": PROFILE,
        "verdict": ("BLOCK" if blocked else
                    "DEGRADED" if degraded else
                    "WARN" if (bool(warn_boards) or forced_warn or stale_warn)
                    else "PASS"),
        "infra_failed": [c["name"] for c in infra_failed],
        "mechanism_overall": mech.get("overall"),
        "mechanism_dead": mech.get("dead"),
        "kanban_crash_count": crash_count,
        "kanban_crash_stale": stale_count,
        "kanban_crash_superseded": superseded_count,
        "kanban_crash_superseded_hits": superseded_hits,
        "cron_forced_releases": {
            "count": forced_releases.get("count", 0),
            "window_h": CRON_FORCED_RELEASE_WINDOW_H,
            "block": forced_block,
            "warn": forced_warn,
            "recent": forced_releases.get("recent", []),
            "log": str(CRON_FORCED_RELEASES_LOG),
        },
        "cron_forced_release_count": forced_releases.get("count", 0),
        "cron_stale_direct_rows": {
            "count": stale_direct_rows.get("count", 0),
            "scanned": stale_direct_rows.get("scanned", 0),
            "by_profile": stale_direct_rows.get("by_profile", {}),
            "min_age_h": CRON_STALE_DIRECT_MIN_AGE_H,
            "warn": stale_warn,
        },
        "cron_stale_direct_row_count": stale_direct_rows.get("count", 0),
        "jarvis_ready_backlog": ready_backlog,
        "sycode_trading_ready_backlog": sycode_ready_backlog,
        "ready_backlog_warn_boards": [
            {"board": b, "oldest_ready_task_id": t, "oldest_ready_age_days": a}
            for (b, t, a) in warn_boards
        ],
        "fork_resource_pressure": fork_pressure,
        "fork_failed": fork_failed,
        "checks": checks,
    }

    # Rotate + append JSONL
    UNIFIED_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        if UNIFIED_LOG.exists():
            lines = UNIFIED_LOG.read_text(encoding="utf-8").splitlines()
            if len(lines) > 5000:
                UNIFIED_LOG.write_text("\n".join(lines[-5000:]) + "\n", encoding="utf-8")
        with UNIFIED_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        pass
    # Legacy substrate-signal bridge (t_bd9d284e): re-stamp a fresh substrate
    # record into health_canary.jsonl every cycle so legacy consumers never
    # read the frozen ~24h-stale gateway_running left by the paused canary job.
    write_legacy_substrate_record(checks, record["verdict"], now)

    # DEGRADED path — fork() pressure exhausted but no real infra failure.
    # Emit a clearly-flagged verdict, exit 0 (probe healthy, liveness keeps
    # being observed). Never crash on transient resource pressure.
    if degraded:
        body = (
            f"🟡 UNIFIED FLEET HEALTH — {now.isoformat()} — VERDICT: DEGRADED\n"
            f"\n"
            f"## fork_resource_pressure (transient — probe did NOT crash)\n"
            f"  - affected checks: {', '.join(fork_failed)}\n"
            f"  - cause: subprocess fork() returned EAGAIN (errno 11) under "
            f"memory/swap pressure; retries exhausted.\n"
            f"  - action: re-check next cycle; this is a resource signal, not "
            f"an infra outage.\n"
            f"\n"
            f"## Infra checks ({sum(1 for c in checks if c['ok'])}/{len(checks)} ok)\n"
            + "\n".join(f"  - {c['name']}: {c['detail']}" for c in checks)
            + f"\n\nmechanism={mech.get('overall','n/a')} "
              f"active_crashes={crash_count} stale_crashes={stale_count} "
              f"superseded_crashes={superseded_count} "
              f"forced_releases={forced_releases.get('count', 0)} "
              f"stale_direct_rows={stale_direct_rows.get('count', 0)} "
              f"ready_backlog={ready_backlog.get('ready_total')} "
              f"devops_ready={ready_backlog.get('devops_ready_count')}"
        )
        alert_file = CRON_OUTPUT / "unified_health_alert.last"
        try:
            alert_file.write_text(body + "\n", encoding="utf-8")
        except Exception:
            pass
        print(body)
        return 0

    # PASS path — always emit the FULL verdict body to STDOUT and rewrite
    # unified_health_alert.last with verdict:PASS + ts. This is the airtight
    # half of RCA t_bd0828fe step #4: after recovery a prior BLOCK can never
    # be replayed, because the .last artifact is overwritten on EVERY run.
    if not blocked and not degraded:
        # Observability-only backlog age may degrade PASS -> WARN without ever
        # BLOCKing (t_bf11a0ce); a single/rare cron forced release degrades
        # PASS -> WARN too (t_615aa245). WARN names the board + oldest ready
        # task id and/or the forced-release count.
        verdict = "WARN" if (bool(warn_boards) or forced_warn or stale_warn) else "PASS"
        emoji = "🟠" if verdict == "WARN" else "🟢"
        lines = [
            f"{emoji} UNIFIED FLEET HEALTH — {now.isoformat()} — VERDICT: {verdict}",
            "",
            f"## Infra checks ({sum(1 for c in checks if c['ok'])}/{len(checks)} ok)",
        ]
        for c in checks:
            lines.append(f"  - {c['name']}: {c['detail']}")
        lines.append("")
        lines.append("## Ready backlog telemetry (observability-only, NOT a BLOCK cause)")
        for _bl in (ready_backlog, sycode_ready_backlog):
            if _bl.get("available"):
                lines.append(f"  - {_bl.get('board')}: {_bl.get('detail')}")
                lines.append(f"      top_ready_ids={_bl.get('top_ready_ids')} "
                             f"top_devops_ready_ids={_bl.get('top_devops_ready_ids')}")
            else:
                lines.append(f"  - {_bl.get('board')}: {_bl.get('detail')}")
        if warn_boards:
            lines.append("")
            lines.append(f"## Ready-backlog WARN (oldest_ready > {READY_BACKLOG_WARN_DAYS}d)")
            for (b, t, a) in warn_boards:
                lines.append(f"  - {b}: oldest_ready={t} age_days={a}")
            lines.append("      action: route to PM/devops for review-merge; "
                         "not an infra outage.")
        if forced_warn:
            lines.append("")
            lines.append("## Cron forced-release WARN (single/rare wedge — "
                         "attention, not an outage)")
            lines.append(f"  - {forced_releases.get('detail')}")
            for r in forced_releases.get("recent", [])[:3]:
                lines.append(f"      - {r.get('name') or r.get('job_id')}: "
                             f"{r.get('at')} age_s={r.get('age_seconds')}")
            lines.append("      action: check the wedged cron job's last_error; "
                         "a second wedge in the window escalates to BLOCK.")
        if stale_warn:
            lines.append("")
            lines.append("## Cron stale-direct execution-row WARN "
                         "(dead-owner running rows not terminalized)")
            lines.append(f"  - {stale_direct_rows.get('detail')}")
            lines.append("      action: verify the owning gateway is up so the "
                         "per-tick terminalizer can clear these rows.")
        if superseded_count:
            lines.append("")
            lines.append(f"## Kanban SUPERSEDED crash/gave_up runs (newer live "
                         f"run present, last {CRASH_LOOKBACK_MIN}m): "
                         f"{superseded_count}  (observed, NOT a BLOCK cause)")
            for h in superseded_hits:
                lines.append(f"  - {h}")
        lines.append("")
        lines.append(f"mechanism={mech.get('overall','n/a')} "
                     f"active_crashes={crash_count} stale_crashes={stale_count} "
                     f"superseded_crashes={superseded_count} "
                     f"forced_releases={forced_releases.get('count', 0)} "
                     f"stale_direct_rows={stale_direct_rows.get('count', 0)} "
                     f"ready_backlog={ready_backlog.get('ready_total')} "
                     f"devops_ready={ready_backlog.get('devops_ready_count')}")
        body = "\n".join(lines)
        # GUARANTEED LOCAL ARTIFACT — stamp every run so a prior BLOCK can never
        # be replayed after recovery (stale-replay fix, t_bd0828fe step #4).
        alert_file = CRON_OUTPUT / "unified_health_alert.last"
        try:
            alert_file.write_text(body + "\n", encoding="utf-8")
        except Exception:
            pass
        print(body)
        return 0

    # Build consolidated BLOCK alert
    lines = [f"🔴 UNIFIED FLEET HEALTH — {now.isoformat()} — VERDICT: BLOCK"]
    lines.append("")
    lines.append("## Infra checks failed")
    if infra_failed:
        for c in infra_failed:
            lines.append(f"  - {c['name']}: {c['detail']}")
    else:
        lines.append("  - (none)")
    if mech_red:
        lines.append("")
        lines.append("## Mechanism matrix RED")
        lines.append(f"  - overall={mech.get('overall')} dead={mech.get('dead')} "
                     f"warn={mech.get('warn')} keys={mech.get('dead_keys')}")
    if crash_count or stale_count or superseded_count:
        lines.append("")
        if crash_count:
            lines.append(f"## Kanban ACTIVE crashes (last {CRASH_LOOKBACK_MIN}m): "
                         f"{crash_count}  <-- BLOCK cause")
            for h in crash_hits:
                lines.append(f"  - {h}")
        if stale_count:
            lines.append(f"## Kanban STALE crash/gave_up runs on parked/resolved "
                         f"tasks: {stale_count}  (monitored, NOT a BLOCK cause)")
        if superseded_count:
            lines.append(f"## Kanban SUPERSEDED crash/gave_up runs (newer live "
                         f"run present, last {CRASH_LOOKBACK_MIN}m): "
                         f"{superseded_count}  (aging-out false BLOCK, NOT a "
                         f"BLOCK cause)")
            for h in superseded_hits:
                lines.append(f"  - {h}")
    if forced_block:
        lines.append("")
        lines.append(f"## Cron forced releases (repeated wedges, last "
                     f"{CRON_FORCED_RELEASE_WINDOW_H}h): "
                     f"{forced_releases.get('count')}  <-- BLOCK cause")
        for r in forced_releases.get("recent", [])[:5]:
            lines.append(f"  - {r.get('name') or r.get('job_id')}: "
                         f"{r.get('at')} age_s={r.get('age_seconds')}")
        lines.append("      action: inspect the wedged cron job(s) and the "
                     "scheduler in-flight guard; repeated wedges are a crash "
                     "class, not noise.")
    if fork_pressure:
        lines.append("")
        lines.append("## fork_resource_pressure (also present, non-fatal): "
                     f"{', '.join(fork_failed)}")
    lines.append("")
    lines.append("## Ready backlog telemetry (observability-only, NOT a BLOCK cause)")
    for _bl in (ready_backlog, sycode_ready_backlog):
        if _bl.get("available"):
            lines.append(f"  - {_bl.get('board')}: {_bl.get('detail')}")
            lines.append(f"      top_ready_ids={_bl.get('top_ready_ids')} "
                         f"top_devops_ready_ids={_bl.get('top_devops_ready_ids')}")
        else:
            lines.append(f"  - {_bl.get('board')}: {_bl.get('detail')}")
    if warn_boards:
        lines.append("")
        lines.append(f"## Ready-backlog WARN (oldest_ready > {READY_BACKLOG_WARN_DAYS}d)")
        for (b, t, a) in warn_boards:
            lines.append(f"  - {b}: oldest_ready={t} age_days={a}")
    lines.append("")
    lines.append("Single escalation path. Classify + route via devops/os-reviewer. "
                 "Do NOT self-heal from this alert.")
    alert = "\n".join(lines)

    # Secret guard
    for pat in SECRET_PATTERNS:
        if pat.search(alert):
            alert = "[secret-shaped value redacted in alert]\n" + "\n".join(
                ln for ln in lines if "sk-" not in ln and "ctx7sk" not in ln)

    # GUARANTEED LOCAL ARTIFACT — persists even if no messaging platform
    # is credentialed (discord/telegram are unconfigured in this instance,
    # so legacy deliver=discord was silently failing too). Single escalation
    # path is still attempted below, but local evidence is never dropped.
    alert_file = CRON_OUTPUT / "unified_health_alert.last"
    alert_log = CRON_OUTPUT / "unified_health_alerts.jsonl"
    try:
        alert_file.write_text(alert + "\n", encoding="utf-8")
        with alert_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": now.isoformat(),
                "verdict": "BLOCK",
                "infra_failed": [c["name"] for c in infra_failed],
                "mechanism_overall": mech.get("overall"),
                "mechanism_dead_keys": mech.get("dead_keys"),
                "kanban_crash_count": crash_count,
                "kanban_crash_stale": stale_count,
                "kanban_crash_superseded": superseded_count,
                "cron_forced_release_count": forced_releases.get("count", 0),
                "jarvis_ready_backlog": ready_backlog,
                "sycode_trading_ready_backlog": sycode_ready_backlog,
                "ready_backlog_warn_boards": [
                    {"board": b, "oldest_ready_task_id": t, "oldest_ready_age_days": a}
                    for (b, t, a) in warn_boards
                ],
                "fork_resource_pressure": fork_pressure,
                "fork_failed": fork_failed,
            }, sort_keys=True) + "\n")
    except Exception:
        pass

    # GUARANTEED VISIBLE OUTPUT — print the FULL BLOCK alert body to STDOUT on
    # every run (RCA t_bd0828fe step #4). The runner's deliver relay then
    # carries the real signal instead of a swallowed/stale replay; the local
    # artifact above already persisted the same body. This runs BEFORE the
    # send attempt so stdout is guaranteed even if send hangs.
    print(alert)

    # SINGLE ESCALATION PATH — discord (matches legacy intent). If no token
    # is configured, send fails; the local artifact above already holds the
    # evidence, and we still exit non-zero so the scheduler flags it.
    send = run([HERMES, "send", "-t", ALERT_TARGET, alert], timeout=30)
    if send["rc"] != 0 or send["timeout"]:
        # Primary platform (discord) failed. Try a best-effort fallback to a
        # second configured platform (telegram) so a single unconfigured
        # platform does not drop the alert. This does NOT change credentials or
        # provider routing — it only adds a redundant delivery attempt. If both
        # fail, the local artifact already holds the evidence and we exit 1.
        # NOTE: in this environment `hermes send` reports no configured platform
        # (rc=1, "platform likely unconfigured") — see t_631685fb NEEDS-FRANK.
        fb = run([HERMES, "send", "-t", "telegram", alert], timeout=30)
        if fb["rc"] == 0 and not fb["timeout"]:
            print(f"BLOCK escalated OK to telegram (discord failed rc={send['rc']} "
                  f"timeout={send['timeout']}).", file=sys.stderr)
        else:
            print("BLOCK (local artifact written; discord send FAILED rc=%s "
                  "timeout=%s — platform likely unconfigured; telegram fallback "
                  "also failed rc=%s). Alert at %s"
                  % (send["rc"], send["timeout"], fb["rc"], alert_file), file=sys.stderr)
            return 1

    # REPEAT-BLOCK HARD-ALERT ESCALATION (t_7a97ba51 #3 / t_cafc1119 C3).
    # If the same crash class has re-BLOCKed the canary >= CRITICAL_ALERT_MIN_COUNT
    # times within CRITICAL_ALERT_WINDOW_H, escalate beyond #fleet-reports to
    # #critical-alerts. This is additive: the #fleet-reports send above always
    # happens; the #critical-alerts send only fires on a sustained recurrence.
    # critical_alert_due() also records this BLOCK event into the rolling state.
    escalated_critical = False
    try:
        if critical_alert_due(now):
            critical_body = (
                "🚨 CRITICAL REPEAT-ALERT (canary re-BLOCK >= "
                f"{CRITICAL_ALERT_MIN_COUNT}x/{CRITICAL_ALERT_WINDOW_H}h)\n"
                + alert
            )
            csend = run([HERMES, "send", "-t", CRITICAL_ALERT_TARGET,
                         critical_body], timeout=30)
            escalated_critical = (csend["rc"] == 0 and not csend["timeout"])
            if not escalated_critical:
                print("BLOCK critical-alert send FAILED rc=%s timeout=%s "
                      "— local artifact holds evidence."
                      % (csend["rc"], csend["timeout"]), file=sys.stderr)
    except Exception as exc:  # never let the escalation break the probe
        print(f"BLOCK critical-alert escalation exception: {exc}",
              file=sys.stderr)

    # escalated-OK: probe ran correctly AND delivery succeeded. This is NOT a
    # probe bug, so we return 0 — the scheduler must NOT label it "script
    # failed" (task t_7b1ceb0f criterion 4). The BLOCK verdict is faithfully
    # recorded in unified_health_canary.jsonl + unified_health_alert.last +
    # unified_health_alerts.jsonl and delivered to discord; the exit code now
    # reflects PROBE health, not infra verdict.
    print(f"BLOCK escalated OK to {ALERT_TARGET} "
          f"(critical_alerts={'YES' if escalated_critical else 'n/a'}): "
          f"{[c['name'] for c in infra_failed]}"
          f"{(' + mechanism RED ' + str(mech.get('dead_keys'))) if mech_red else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
