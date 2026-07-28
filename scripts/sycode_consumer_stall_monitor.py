#!/usr/bin/env python3
"""Sycode pipeline consumer-stall / starvation detector (devops lane, jarvis-os/t_6f3cf9cd).

WHY THIS EXISTS
---------------
The 2026-07-28 fleet data-pipeline RCA (jarvis-os/t_1267e30b) found that the
system has multiple asynchronous write/finalization/labeling paths but no
single end-to-end contract. Its P1#4 action is explicitly:

  "split producer-green from consumer-green — a green producer must not mask a
   red consumer. ... signal-fusion is green, quant-researcher is fail-closed by
   N=183<300, and fusion-calibration timed out after 3600s. These are three
   different states and need different alerts."

RCA evidence: the signal-fusion PRODUCER was green and core tables fresh, yet
the fusion-calibration CONSUMER timed out after 3600s on its latest run. No
existing monitor flags "the scheduled consumer did not complete inside its
window" — the closest (quant_researcher_6h_validator.py) only inspects the
output artifact AFTER a run, so a consumer that STALLS at the scheduler level
(last_run_at goes stale / never advances) is invisible.

This monitor closes that gap read-only: it reads each configured consumer
cron job's own scheduler state (last_run_at / last_status / last_error / enabled)
from the Hermes cron store and alerts when a job has not completed within
expected_interval * STALL_MULTIPLIER, or has a recent error status. It NEVER
mutates the cron store, runtime, queue, or database. It is fail-closed: a
probe/read failure is reported as an alert, never swallowed.

SCOPE GUARD (per Sycode PM t_6424c5a5): this is the devops-lane COMPLEMENT to
the already-owned DLQ resilience chain (trading-devops dlq_growth_monitor.py +
dlq_redrive_audited.py + dlq-redrive-classifier cron). It does NOT duplicate
that chain and does NOT touch the queue. The DLQ lane stays owned by
trading-devops. This monitor only watches consumer *cadence*.

Run:
  python3 sycode_consumer_stall_monitor.py            # live (alerts if stalled)
  python3 sycode_consumer_stall_monitor.py --dry-run  # print snapshot + alerts
  python3 sycode_consumer_stall_monitor.py --self-test
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# HERMES_HOME may be either the repo root (~/.hermes) or a profile-scoped home
# (~/.hermes/profiles/<p>) depending on the invoking gateway. The cron job
# stores live at <root>/profiles/<profile>/cron/jobs.json. Resolve the repo
# root robustly so the consumer cadence is found in both layouts.
def _resolve_hermes_home() -> Path:
    """Find the Hermes repo root that actually contains ``profiles/<p>/cron``.

    HERMES_HOME may be set to:
      * the repo root itself (~/.hermes) — root-scoped gateway; or
      * a profile-scoped home (~/.hermes/profiles/<p>) — profile gateway.
    In both cases the cron stores live at <root>/profiles/<profile>/cron/jobs.json,
    so we pick the highest ancestor that has a direct ``profiles`` directory.
    """
    h = Path(os.environ.get("HERMES_HOME", "/home/frank/.hermes")).resolve()
    for cand in [h, *h.parents]:
        if (cand / "profiles").is_dir():
            return cand
    return h


HERMES_HOME = _resolve_hermes_home()
STATE_DIR = Path(os.environ.get("SYCODE_CONSUMER_STALL_STATE_DIR",
                                "/home/frank/.hermes/var/sycode_consumer_stall"))
STATE_FILE = STATE_DIR / "seen.json"
STATUS_FILE = STATE_DIR / "status.json"
STALL_MULTIPLIER = float(os.environ.get("SYCODE_CONSUMER_STALL_MULTIPLIER", "2.5"))
COOLDOWN_SECONDS = int(os.environ.get("SYCODE_CONSUMER_STALL_COOLDOWN_SECONDS", "3600"))
SCRIPT_ID = "sycode-consumer-stall-monitor:t_6f3cf9cd"

# Default target consumer jobs (discovered live from the cron stores on
# 2026-07-28). expected_minutes is the authoritative cadence used for the
# stall window; schedule_display is informational only. Override the whole
# list with SYCODE_CONSUMER_STALL_TARGETS (JSON array of objects).
DEFAULT_TARGETS: list[dict[str, Any]] = [
    {"profile": "jarvis", "id": "f05227128ac2", "name": "fusion-calibration-report",
     "expected_minutes": 360, "role": "consumer-heavyweight",
     "note": "RCA t_1267e30b: timed out after 3600s on 2026-07-28 run"},
    {"profile": "jarvis", "id": "13c1f9279025", "name": "quant-researcher-6h",
     "expected_minutes": 360, "role": "consumer"},
    {"profile": "jarvis", "id": "f8c5f45a9831", "name": "signal-fusion-engine",
     "expected_minutes": 15, "role": "producer"},
    {"profile": "jarvis", "id": "a70772892543", "name": "clean-outcome-labeler-24h-v2",
     "expected_minutes": 15, "role": "consumer"},
    {"profile": "trading-devops", "id": "1e5c8cbaa397", "name": "dlq-redrive-classifier",
     "expected_minutes": 360, "role": "consumer-audit"},
]


def load_targets() -> list[dict[str, Any]]:
    raw = os.environ.get("SYCODE_CONSUMER_STALL_TARGETS")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass
    return DEFAULT_TARGETS


def parse_ts(value: str | None) -> datetime | None:
    """Parse Hermes cron last_run_at / next_run_at ISO strings (tz-aware)."""
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        # Fallback: strip fractional/zone noise and retry
        m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", value)
        if not m:
            return None
        try:
            dt = datetime.fromisoformat(m.group(1))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_job(profile: str, job_id: str, job_name: str) -> dict[str, Any]:
    """Return the matched job dict, or a dict carrying an `_error` key on miss."""
    path = HERMES_HOME / "profiles" / profile / "cron" / "jobs.json"
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        return {"_error": f"cannot read {path}: {e}"}
    for job in data.get("jobs", []):
        if job.get("id") == job_id or job.get("name") == job_name:
            return job
    return {"_error": f"job id={job_id} name={job_name} not found in {profile}"}


def eval_target(t: dict[str, Any], now: datetime) -> dict[str, Any]:
    prof = t.get("profile", "?")
    job = load_job(prof, t.get("id", ""), t.get("name", ""))
    if "_error" in job:
        return {
            "profile": prof, "id": t.get("id"), "name": t.get("name"),
            "role": t.get("role"), "expected_minutes": t.get("expected_minutes"),
            "status": "PROBE_ERROR", "since_minutes": None,
            "last_status": None, "enabled": None,
            "detail": job["_error"], "sched": t.get("schedule_display"),
        }
    last_run = parse_ts(job.get("last_run_at"))
    last_status = (job.get("last_status") or "unknown")
    enabled = bool(job.get("enabled"))
    expected = float(t.get("expected_minutes") or 0)
    since = None
    state = "OK"
    detail = ""
    if not enabled:
        state = "DISABLED"
    elif last_run is None:
        # Never completed a run this scheduler knows about.
        state = "STALL_NEVER_RAN"
        detail = "scheduler has no last_run_at (consumer never completed)"
    else:
        since = (now - last_run).total_seconds() / 60.0
        window = expected * STALL_MULTIPLIER
        if since > window:
            state = "STALL"
            detail = (f"no completion in {since:.0f}m (window {window:.0f}m = "
                      f"{expected:.0f}m * {STALL_MULTIPLIER})")
        elif last_status == "error":
            state = "ERROR"
            detail = f"last_status=error: {(job.get('last_error') or '')[:160]}"
    return {
        "profile": prof, "id": t.get("id"), "name": t.get("name"),
        "role": t.get("role"), "expected_minutes": expected,
        "status": state, "since_minutes": round(since, 1) if since is not None else None,
        "last_status": last_status, "enabled": enabled,
        "detail": detail, "sched": job.get("schedule_display"),
        "last_run_at": job.get("last_run_at"),
    }


def build_alerts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    alerts = []
    for r in rows:
        if r["status"] in ("STALL", "STALL_NEVER_RAN", "ERROR", "PROBE_ERROR"):
            alerts.append(r)
    return alerts


# ---- state dedup ------------------------------------------------------------
def read_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def write_state(payload: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp-" + str(os.getpid()))
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, STATE_FILE)


def fingerprint_for(r: dict[str, Any]) -> str:
    return f"{r['profile']}:{r['name']}:{r['status']}"


def should_emit(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    state = read_state()
    now = time.time()
    emitted = []
    for a in alerts:
        key = a["name"]
        fp = fingerprint_for(a)
        prev = state.get(key, {})
        if prev.get("fp") != fp:
            emitted.append(a)
            state[key] = {"fp": fp, "last_sent": now}
        elif now - float(prev.get("last_sent", 0)) >= COOLDOWN_SECONDS:
            emitted.append(a)
            state[key] = {"fp": fp, "last_sent": now}
    write_state(state)
    return emitted


def format_alert(rows: list[dict[str, Any]]) -> str:
    lines = [
        "🚨 SYCODE CONSUMER-STALL ALERT (devops lane t_6f3cf9cd)",
        f"timestamp_utc: {datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')}",
        "scope: per-consumer cadence health (producer-green vs consumer-green split, RCA P1#4)",
        "safety: read-only; scheduler-state inspection only; no cron/queue/db/runtime mutation",
        "",
        "Stalled/explicit-failure consumers:",
    ]
    for r in rows:
        lines.append(
            f"  - [{r['status']}] {r['profile']}/{r['name']} (role={r['role']}, "
            f"expected={r['expected_minutes']}m, since={r['since_minutes']}m, "
            f"last_status={r['last_status']}) :: {r['detail']}"
        )
    lines += [
        "",
        "Action: inspect the named consumer job log; a STALL means it did not complete in-window "
        "(could be timeout/stall at scheduler or worker level). Triage the underlying producer/consumer "
        "path. Do NOT auto-redrive queues here — that gate is trading-devops dlq_redrive_audited.py "
        "(DLQ_APPLY_UNSAFE + Frank approval).",
        "Source: jarvis-os/t_6f3cf9cd ; RCA jarvis-os/t_1267e30b.",
    ]
    return "\n".join(lines)


def self_test() -> int:
    now = datetime.now(timezone.utc)
    mk = lambda mins: (now - timedelta(minutes=mins)).replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    # Healthy 15m producer, recent run
    rows_ok = [{
        "profile": "jarvis", "id": "x", "name": "signal-fusion-engine", "role": "producer",
        "expected_minutes": 15, "status": "OK", "since_minutes": 3.0,
        "last_status": "ok", "enabled": True, "detail": "", "sched": "every 15m",
        "last_run_at": mk(3),
    }]
    assert build_alerts(rows_ok) == [], "healthy row should not alert"
    # Stalled 6h consumer (last run 1000m ago > 360*2.5=900)
    rows_stall = [{
        "profile": "jarvis", "id": "f05227128ac2", "name": "fusion-calibration-report",
        "role": "consumer-heavyweight", "expected_minutes": 360, "status": "STALL",
        "since_minutes": 1000.0, "last_status": "ok", "enabled": True,
        "detail": "no completion in 1000m", "sched": "0 */6 * * *", "last_run_at": mk(1000),
    }]
    assert len(build_alerts(rows_stall)) == 1, "stalled row must alert"
    # error status
    rows_err = [dict(rows_stall[0], status="ERROR", detail="last_status=error: boom",
                     since_minutes=10.0, last_status="error")]
    assert len(build_alerts(rows_err)) == 1, "error row must alert"
    # never ran
    rows_never = [dict(rows_stall[0], status="STALL_NEVER_RAN", detail="no last_run_at",
                       since_minutes=None, last_status="unknown", last_run_at=None)]
    assert len(build_alerts(rows_never)) == 1, "never-ran row must alert"
    # probe error
    rows_probe = [dict(rows_stall[0], status="PROBE_ERROR", detail="cannot read store",
                       since_minutes=None, last_status=None, last_run_at=None)]
    assert len(build_alerts(rows_probe)) == 1, "probe-error row must alert"
    # disabled should not alert even if stale
    rows_dis = [dict(rows_stall[0], status="DISABLED", enabled=False, since_minutes=5000.0)]
    assert build_alerts(rows_dis) == [], "disabled must not alert"
    # fingerprint + cooldown logic
    a = rows_stall[0]
    # last_sent=0 means "long ago" -> cooldown elapsed -> SHOULD emit
    write_state({a["name"]: {"fp": fingerprint_for(a), "last_sent": 0}})
    assert should_emit([a]) == [a], "same fp but last_sent long ago -> emit"
    # re-evaluate: with last_sent set to now it suppresses
    write_state({a["name"]: {"fp": fingerprint_for(a), "last_sent": time.time()}})
    assert should_emit([a]) == [], "same fp + recent -> suppressed"
    # changed fp emits
    a2 = dict(a, status="ERROR", detail="err")
    assert should_emit([a2]) == [a2], "changed fp -> emit"
    print("SELF_TEST_PASS sycode_consumer_stall_monitor")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print snapshot + alerts, no state change")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    now = datetime.now(timezone.utc)
    targets = load_targets()
    rows = [eval_target(t, now) for t in targets]
    alerts = build_alerts(rows)
    snapshot = {
        "script_id": SCRIPT_ID,
        "timestamp_utc": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "safety": "READ_ONLY_NO_CRON_QUEUE_DB_RUNTIME_MUTATION",
        "stall_multiplier": STALL_MULTIPLIER,
        "consumers": rows,
        "alerts": alerts,
        "status": "ALERT" if alerts else "OK",
    }
    if not args.dry_run:
        try:
            STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATUS_FILE.with_suffix(".tmp-" + str(os.getpid()))
            tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
            os.replace(tmp, STATUS_FILE)
        except Exception:
            pass
        emitted = should_emit(alerts) if alerts else []
        if emitted:
            print(format_alert(emitted))
        # quiet when healthy or suppressed
    else:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
