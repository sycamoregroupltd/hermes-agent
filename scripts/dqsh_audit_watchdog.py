#!/usr/bin/env python3
"""
dqsh_audit_watchdog.py

Read-only health watchdog for the DQSH (Data-Quality Self-Healing) daemon.

Context
-------
The DQSH daemon (`/home/frank/.hermes/scripts/dqsh_daemon.py`) only writes to its
audit log (`~/.hermes/var/dq/audit_log.jsonl`) when it *acts* (RESTART_CONSUMER,
TRIGGER_REST_BACKFILL, INTERPOLATE_CANDLES, DLQ_REPLAY, SUPPRESS_ALERT). On a
healthy/quiet run it logs NOTHING -- there is no heartbeat. A naive monitor that
counts entries therefore sees "silence" and false-alarms an outage, when in fact
the daemon is alive and the pipeline is simply healthy.

This watchdog watches the audit log *file mtime* as the primary "is the daemon
alive" signal:
  - audit mtime fresh  (<= STALE_MIN)  -> daemon alive  -> content check below
  - audit mtime stale  (>  STALE_MIN)  -> daemon dead / stopped writing -> exit 2
  - audit log missing                 -> treat as dead (never wrote)     -> exit 2

SECONDARY CONTENT-AWARE CHECK (added 2026-07-10, fixes masked-incident blind spot):
A fresh mtime with only DRY_RUN/FAILED content still meant HEALTHY -- which masked
the 2026-07-10 live incident (self-healer permanently paper-mode: SUCCESS=0 across
3,200+ entries, RESTART_CONSUMER permanently cap-halted, consumer lag climbing).
We now also scan the recent audit window for real degradation signals and emit a
distinct DEGRADED verdict (exit 4) when the daemon is alive-but-not-healing, so the
#critical-alerts delivery surfaces masked incidents without changing the daemon's
paper-mode safety posture. See kanban t_4fbf61fc (incident) and t_ba2c0b5b (fix).

It is strictly READ-ONLY: no writes, no remediation, no side effects. It exits
non-zero ONLY on genuine silence (dead) or detected degradation (DEGRADED).

Env overrides (for testing / future-proofing):
  DQSH_AUDIT_LOG_PATH   absolute path to the audit log (default below)
  DQSH_STALE_MIN        staleness threshold in minutes (default 40)
  DQSH_WINDOW_MIN       lookback window in minutes for the content check (default 120)
  DQSH_DRYRUN_OK_RATIO  max all-DRY_RUN ratio tolerated before degraded (default 0.98)

Originally specified in RCA 2026-07-10 (t_46b01fae). Reconstructed 2026-07-10
after the cron `dqsh-audit-watchdog` failed with "Script not found" -- the prior
artifact was claimed done but never materialized on disk.

Delivered to: #critical-alerts (every 20m via hermes cron 0ead099b5eac).
"""

import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta

# --- Config (mirrors dqsh_daemon.py AUDIT_LOG_PATH) -------------------------
DEFAULT_AUDIT_LOG = "/home/frank/.hermes/var/dq/audit_log.jsonl"
DEFAULT_STALE_MIN = 40
DEFAULT_WINDOW_MIN = 120      # how far back to scan for degradation signals
DEFAULT_DRYRUN_OK_RATIO = 0.98  # above this all-DRY_RUN share => suspicious

EXIT_OK = 0
EXIT_DEAD = 2
EXIT_DEGRADED = 4
EXIT_ERROR = 3


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(ts):
    """Parse an audit timestamp_utc such as
    '2026-07-10T19:51:26.481582+00:00Z' (offset AND trailing Z) or
    '2026-07-10T19:51:26Z' (trailing Z, no offset)."""
    if not ts:
        return None
    s = ts
    # Normalise the trailing Z first.
    if s.endswith("Z"):
        s = s[:-1]
    # Ensure a UTC offset so fromisoformat yields an aware datetime.
    if not (("+" in s[10:]) or ("-" in s[10:])):
        s = s + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def last_action_summary(path):
    """Return a short human-readable summary of the most recent audit entry."""
    try:
        with open(path, "rb") as f:
            # read last non-empty line without slurping the whole file
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return None
            f.seek(0, os.SEEK_END)
            buf = b""
            while f.tell() > 0:
                f.seek(-2, os.SEEK_CUR)
                ch = f.read(1)
                if ch == b"\n":
                    if buf:
                        break
                else:
                    buf = ch + buf
            if not buf:
                return None
        entry = json.loads(buf.decode("utf-8", errors="replace"))
        ts = entry.get("timestamp_utc", "?")
        action = entry.get("action", "?")
        status = entry.get("status", "?")
        return f"{action} [{status}] @ {ts}"
    except Exception as e:
        return f"(unable to parse last entry: {e})"


def recent_entries(path, window_min, anchor=None):
    """Yield audit entries within `window_min` of `anchor` (default: latest
    entry timestamp in the file). Anchoring on the latest audit entry -- not
    wall-clock -- makes the check robust to host clock skew between the cron
    host and the audit-log writer."""
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln for ln in f if ln.strip()]
        parsed = []
        for line in lines:
            try:
                e = json.loads(line)
            except Exception:
                continue
            parsed.append(e)
        if anchor is None:
            # find latest timestamp among parsed entries
            latest = None
            for e in parsed:
                ts = _parse_ts(e.get("timestamp_utc", ""))
                if ts is not None and (latest is None or ts > latest):
                    latest = ts
            anchor = latest
        if anchor is None:
            return out
        cutoff = anchor - timedelta(minutes=window_min)
        for e in parsed:
            ts = _parse_ts(e.get("timestamp_utc", ""))
            if ts is None:
                continue
            if ts >= cutoff:
                out.append(e)
    except Exception:
        pass
    return out


def content_health(entries):
    """
    Inspect the recent audit window for genuine degradation signals.

    Returns (verdict, detail) where verdict is one of:
      'OK'         - no degradation detected (genuine healthy or healthy-silent)
      'DEGRADED'   - daemon alive but remediation path non-functional / stalled
    """
    if not entries:
        # Window empty but mtime fresh means healthy-silent recent cycles.
        return "OK", "no recent audit entries in window (healthy-silent)"

    total = len(entries)
    dry = sum(1 for e in entries if e.get("status") == "DRY_RUN")
    succ = sum(1 for e in entries if e.get("status") == "SUCCESS")
    failed = sum(1 for e in entries if e.get("status") == "FAILED")

    # Signal 1: RESTART_CONSUMER cap-halt. A healthy self-healer does not
    # persistently trip its own safety cap. Even one recent cap-halt means the
    # consumer-restart remediation path is non-functional.
    restart_entries = [e for e in entries if e.get("action") == "RESTART_CONSUMER"]
    cap_halted = [
        e for e in restart_entries
        if e.get("status") == "FAILED"
        and isinstance(e.get("details"), str)
        and "cap exceeded" in e.get("details", "").lower()
    ]

    # Signal 2: climbing lag. The audit stream normally does not emit lag
    # fields, but if a future version does, catch monotonic-up lag samples.
    lag_samples = []
    for e in entries:
        d = e.get("details")
        if isinstance(d, dict):
            for k in ("consumer_lag_hours", "lag_hours", "lag"):
                if k in d:
                    try:
                        lag_samples.append((_parse_ts(e.get("timestamp_utc", "")), float(d[k])))
                    except Exception:
                        pass
    climbing_lag = False
    if len(lag_samples) >= 3:
        lag_samples.sort(key=lambda x: (x[0] is None, x[0]))
        vals = [v for _, v in lag_samples if v is not None]
        if len(vals) >= 3 and all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)) and vals[-1] > 24:
            climbing_lag = True

    # Decision
    if cap_halted or climbing_lag:
        reasons = []
        if cap_halted:
            reasons.append(
                f"RESTART_CONSUMER cap-halted {len(cap_halted)}x in window "
                f"(safety cap exceeded -> restart remediation path non-functional)"
            )
        if climbing_lag:
            reasons.append("consumer lag climbing and >24h")
        reasons.append(f"SUCCESS={succ} of {total} recent entries (healer not remediating)")
        return "DEGRADED", "; ".join(reasons)

    # No hard degradation, but warn if the healer is effectively inert.
    if succ == 0 and total >= 20 and (dry / total) >= DEFAULT_DRYRUN_OK_RATIO:
        return (
            "DEGRADED",
            f"all-DRY_RUN ({dry}/{total}) with 0 SUCCESS in window -- "
            f"self-healer has performed no real remediation",
        )

    return "OK", f"recent window OK (total={total}, dry={dry}, success={succ}, failed={failed})"


def main():
    audit_log = os.environ.get("DQSH_AUDIT_LOG_PATH", DEFAULT_AUDIT_LOG)
    try:
        stale_min = int(os.environ.get("DQSH_STALE_MIN", DEFAULT_STALE_MIN))
    except ValueError:
        stale_min = DEFAULT_STALE_MIN
    try:
        window_min = int(os.environ.get("DQSH_WINDOW_MIN", DEFAULT_WINDOW_MIN))
    except ValueError:
        window_min = DEFAULT_WINDOW_MIN

    now = time.time()

    if not os.path.exists(audit_log):
        print(f"[DQSH-WATCHDOG] DEAD: audit log missing at {audit_log}")
        print("[DQSH-WATCHDOG] The DQSH daemon has never written an audit entry "
              "-- treat as daemon-not-running.")
        return EXIT_DEAD

    mtime = os.path.getmtime(audit_log)
    age_min = (now - mtime) / 60.0
    last = last_action_summary(audit_log)

    if age_min > stale_min:
        print(f"[DQSH-WATCHDOG] DEAD: audit log stale (age={age_min:.1f}m, "
              f"threshold={stale_min}m)")
        print(f"[DQSH-WATCHDOG] last write: {_iso(mtime)}")
        if last:
            print(f"[DQSH-WATCHDOG] last audit entry: {last}")
        print("[DQSH-WATCHDOG] Daemon appears stopped -- investigate DQSH "
              "self-healer daemon / pipeline.")
        return EXIT_DEAD

    # --- mtime fresh: daemon alive. Now content-check for masked degradation.
    print(f"[DQSH-WATCHDOG] HEALTHY(mtime): audit log fresh (age={age_min:.1f}m, "
          f"threshold={stale_min}m)")
    if last:
        print(f"[DQSH-WATCHDOG] last audit entry: {last}")

    entries = recent_entries(audit_log, window_min)
    verdict, detail = content_health(entries)

    if verdict == "DEGRADED":
        print(f"[DQSH-WATCHDOG] DEGRADED: {detail}")
        print("[DQSH-WATCHDOG] Daemon alive but remediation non-functional / "
              "stalled (see t_4fbf61fc). Escalating despite fresh mtime.")
        return EXIT_DEGRADED

    print(f"[DQSH-WATCHDOG] CONTENT-OK: {detail}")
    print("[DQSH-WATCHDOG] Daemon is alive and healing path functional.")
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[DQSH-WATCHDOG] ERROR: {e}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
