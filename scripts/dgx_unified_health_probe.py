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
ALERT_TARGET = "discord:#fleet-reports"
CRON_STALE_MIN = 35          # cron ticker considered stale past this
CANARY_STALE_MIN = 40        # last health_canary.jsonl write considered stale past this
CRASH_LOOKBACK_MIN = 60      # kanban crash/gave_up window

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
    r = run([HERMES, "-p", PROFILE, "cron", "status"], timeout=25)
    if r["timeout"] or r["rc"] != 0:
        return False, f"cron status rc={r['rc']} timeout={r['timeout']}", bool(r.get("fork_resource_pressure"))
    text = r["out"] + "\n" + r["err"]
    if "Gateway is running" not in text:
        return False, "cron status did not report 'Gateway is running'", bool(r.get("fork_resource_pressure"))
    if not re.search(r"\b\d+ active job\(?s\)?", text):
        return False, "cron active job count line not found", bool(r.get("fork_resource_pressure"))
    return True, "cron ticker reports running with active jobs", bool(r.get("fork_resource_pressure"))


def check_canary_freshness() -> tuple[bool, str]:
    if not LEGCY_CANARY.exists():
        return True, "legacy health_canary.jsonl absent (probe not yet run historically)"
    try:
        mtime = dt.datetime.fromtimestamp(
            LEGCY_CANARY.stat().st_mtime, dt.timezone.utc
        )
    except Exception as exc:
        return False, f"cannot stat legacy canary: {exc}"
    age_min = (utc_now() - mtime).total_seconds() / 60.0
    if age_min > CANARY_STALE_MIN:
        return False, f"last health_canary write {age_min:.1f}m ago (> {CANARY_STALE_MIN}m)"
    return True, f"last health_canary write {age_min:.1f}m ago"


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


def check_kanban_crashes() -> tuple[int, list[str], int]:
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
    """
    if not BOARDS_DIR.exists():
        return 0, [], 0
    cutoff = int(utc_now().timestamp()) - CRASH_LOOKBACK_MIN * 60
    active_hits: list[str] = []
    stale_count = 0
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
                    "SELECT tr.task_id, tr.outcome, tr.ended_at, "
                    "COALESCE(t.status, 'unknown') "
                    "FROM task_runs tr "
                    "LEFT JOIN tasks t ON tr.task_id = t.id "
                    "WHERE tr.outcome IN ('crashed','gave_up') "
                    "AND tr.ended_at >= ? "
                    "ORDER BY tr.ended_at DESC LIMIT 25",
                    (cutoff,),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            con.close()
            for task_id, outcome, ended, status in rows:
                if status in ACTIVE_TASK_STATES:
                    # de-dupe by task_id so one task with several crashed/gave_up
                    # runs in the window is not double-counted.
                    if task_id not in seen_task_ids and len(active_hits) < 8:
                        active_hits.append(
                            f"{board}/{task_id}:{outcome} (status={status})"
                        )
                    seen_task_ids.add(task_id)
                else:
                    stale_count += 1
        except Exception:
            continue
    return len(active_hits), active_hits, stale_count


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

    crash_count, crash_hits, stale_count = check_kanban_crashes()
    checks.append({"name": "kanban_crashes", "ok": crash_count == 0,
                   "detail": f"{crash_count} active crash/gave_up run(s) within "
                             f"{CRASH_LOOKBACK_MIN}m (stale/parked on resolved "
                             f"tasks: {stale_count})",
                   "fork_resource_pressure": False})

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
    blocked = bool(infra_failed) or mech_red
    degraded = fork_pressure and not blocked

    record = {
        "ts": now.isoformat(),
        "profile": PROFILE,
        "verdict": "BLOCK" if blocked else ("DEGRADED" if degraded else "PASS"),
        "infra_failed": [c["name"] for c in infra_failed],
        "mechanism_overall": mech.get("overall"),
        "mechanism_dead": mech.get("dead"),
        "kanban_crash_count": crash_count,
        "kanban_crash_stale": stale_count,
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
              f"active_crashes={crash_count} stale_crashes={stale_count}"
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
    if not blocked:
        body = (
            f"🟢 UNIFIED FLEET HEALTH — {now.isoformat()} — VERDICT: PASS\n"
            f"\n"
            f"## Infra checks ({sum(1 for c in checks if c['ok'])}/{len(checks)} ok)\n"
            + "\n".join(f"  - {c['name']}: {c['detail']}" for c in checks)
            + f"\n\nmechanism={mech.get('overall','n/a')} "
              f"active_crashes={crash_count} stale_crashes={stale_count}"
        )
        # GUARANTEED LOCAL ARTIFACT — stamp PASS every run so a prior BLOCK
        # can never be replayed after recovery (stale-replay fix).
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
    if crash_count or stale_count:
        lines.append("")
        if crash_count:
            lines.append(f"## Kanban ACTIVE crashes (last {CRASH_LOOKBACK_MIN}m): "
                         f"{crash_count}  <-- BLOCK cause")
            for h in crash_hits:
                lines.append(f"  - {h}")
        if stale_count:
            lines.append(f"## Kanban STALE crash/gave_up runs on parked/resolved "
                         f"tasks: {stale_count}  (monitored, NOT a BLOCK cause)")
    if fork_pressure:
        lines.append("")
        lines.append("## fork_resource_pressure (also present, non-fatal): "
                     f"{', '.join(fork_failed)}")
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
        # escalation-failed: the BLOCK is real BUT we could not deliver it.
        # This IS a probe-side failure worth flagging (exit 1 = script failed
        # is correct here). Local artifact already holds the evidence.
        print("BLOCK (local artifact written; discord send FAILED rc=%s "
              "timeout=%s — platform likely unconfigured). Alert at %s"
              % (send["rc"], send["timeout"], alert_file), file=sys.stderr)
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
