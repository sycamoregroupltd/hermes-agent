#!/usr/bin/env python3
"""
sycode_fingerprint_lag_monitor.py - idempotent fingerprint lag monitoring for G-F1.

Uses idempotency key: gqt-integrity-2026-08-06-I3

WHAT THIS DOES
--------------
Read-only measurement of signal_fingerprints producer lag against the G-F1 / I-3
acceptance criteria (WS01 I-3, ticket t_eddbfd72), persisted to a single JSONL
state file keyed by the idempotency key above. Safe to re-run: identical
measurements for the same UTC day are deduped; new measurements append a record.
No DB mutation (INSERT/UPDATE/DELETE/DDL) -- SELECT only.

The JSONL file is the queryable store AND the anomaly log: any record whose
`alerts` list is non-empty is an anomaly (criterion breached). A vault note is
generated from the latest record (see --emit-vault).

t_bce90116 (2026-09-05): every fingerprint SQL is a nested-loop join from
signal_journeys.triggered_at (indexed) to signal_fingerprints.correlation_id.
Unbounded count(*)/max(created_at) on signal_fingerprints is a parallel seq
scan (5.4M rows, no created_at index) and is forbidden. statement_timeout
default 20s, per-query psql backstop 25s, process probe budget 55s, shim wall
60s (still << 60m cadence). Shared flock with the DB-latency collector.
Stmt/wall timeout is a watchdog exit-3 probe failure, never silent healthy.

THRESHOLDS (from WS01 I-3 / F1.4 / root-cause doc 2026-08-06)
-------------------------------------------------------------
  I-3 A  producer liveness lag_h < 1h          (alert if lag >1h per F1.4)
  I-3 B  p95 lag (fp created_at - fp triggered_at) < 30 min   (I-3 acceptance)
  I-3 C  7d journey:fingerprint ratio >= 0.98  (I-3 acceptance)
  Escalation: lag_h >= 2h or p95 >= 60m or ratio < 0.90 -> level CRITICAL
  (F1.5 steady-state acceptance is lag_h < 2h; ratio floor 0.98 per I-3.)

USAGE
    python3 ~/.hermes/scripts/sycode_fingerprint_lag_monitor.py
    python3 ~/.hermes/scripts/sycode_fingerprint_lag_monitor.py --json
    python3 ~/.hermes/scripts/sycode_fingerprint_lag_monitor.py --emit-vault
    python3 ~/.hermes/scripts/sycode_fingerprint_lag_monitor.py --watchdog
        (no-agent cron mode: SILENT on healthy, prints ALERT block + best-effort
         Telegram + idempotent standing kanban card on breach; exit 0 unless the
         probe itself fails, which exits 3 so the cron flags monitor failure.)

EXIT CODES
    0  healthy (or watchdog silent / watchdog breach already alerted)
    1  a tracked criterion is failing (default mode only)
    3  harness error (DB unreachable, query error)
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

KEY = "gqt-integrity-2026-08-06-I3"
TICKET = "t_b0f2fd14"
GAP = "G-F1"
METRIC = "signal_fingerprints.created_at (fingerprint producer lag)"

STATE_DIR = "/home/frank/.hermes/state"
STATE = os.path.join(STATE_DIR, "gqt-fingerprint-lag-g-f1.jsonl")
ALERT_STATE = os.path.join(STATE_DIR, "gqt-fingerprint-lag-g-f1.alert.json")
VAULT_NOTE = ("/home/frank/obsidian/grok-quant-trader/workflows/"
              "gqt-integrity-2026-08-06-I3-fingerprint-lag.md")

# ---- thresholds (env-overridable) ------------------------------------------
LAG_ALERT_H = float(os.getenv("GQT_FP_LAG_ALERT_H", "1.0"))      # I-3 A
P95_ALERT_MIN = float(os.getenv("GQT_FP_P95_ALERT_MIN", "30.0"))  # I-3 B
RATIO_FLOOR = float(os.getenv("GQT_FP_RATIO_FLOOR", "0.98"))      # I-3 C
CRIT_LAG_H = float(os.getenv("GQT_FP_CRIT_LAG_H", "2.0"))         # escalation
CRIT_P95_MIN = float(os.getenv("GQT_FP_CRIT_P95_MIN", "60.0"))    # escalation
CRIT_RATIO = float(os.getenv("GQT_FP_CRIT_RATIO", "0.90"))        # escalation
REMIND_SECONDS = int(os.getenv("GQT_FP_REMIND_SECONDS", str(6 * 3600)))

KANBAN_BOARD = os.getenv("GQT_FP_KANBAN_BOARD", "jarvis-os")
KANBAN_ASSIGNEE = os.getenv("GQT_FP_KANBAN_ASSIGNEE", "sycode-trading-pm")
TELEGRAM_TARGET = os.getenv("GQT_FP_TELEGRAM_TARGET", "telegram")
STANDING_CARD_IDEM_KEY = "gqt-fingerprint-lag-g-f1-standing"

# Kanban runtime env vars the CLI inherits from workers; scrub them from the
# kanban-create sub-process so it always hits the default board.
KANBAN_ENV_OVERRIDES = ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                        "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE")

PSQL = ["psql", "-h", "127.0.0.1", "-p", "5432", "-U", "postgres", "-d", "postgres",
        "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1"]
# t_bce90116: 5.4M-row signal_fingerprints has NO created_at index; unbounded
# count(*)/max(created_at) was a parallel seq scan (EXPLAIN, 2026-09-05). Bound
# every probe through signal_journeys.triggered_at (idx_signal_journeys_triggered_corr)
# nested-loop join to idx_signal_fingerprints_correlation_id. 20s statement
# timeout is the SQL bound; 25s psql backstop; 55s remaining-time budget so
# four serial queries cannot overrun the 60s shim wall (still << 60m cadence).
STMT_TIMEOUT = os.getenv("GQT_FP_STMT_TIMEOUT", "20s")
PSQL_TIMEOUT_S = int(os.getenv("GQT_FP_PSQL_TIMEOUT_S", "25"))
PROBE_BUDGET_S = float(os.getenv("GQT_FP_PROBE_BUDGET_S", "55"))
LOCK_PATH = os.getenv(
    "GQT_FP_LOCK",
    "/home/frank/.hermes/profiles/trading-devops/cron/state/sycode-oltp-probe.lock",
)

# Precise read-only guard: match whole SQL statement keywords with surrounding spaces.
_FORBIDDEN = (" INSERT ", " UPDATE ", " DELETE ", " DROP ", " ALTER ",
              " CREATE ", " TRUNCATE ", " VACUUM ", " REINDEX ", " GRANT ",
              " REVOKE ", " SET ", " SET SCHEMA", " COPY ", " ATTACH ",
              " DETACH ", " REFRESH ", " NOTIFY ", " LISTEN ")

MUTATION_LOCK = "/home/frank/.hermes/locks/sycode-data-mutation.lock"


def read_mutation_lock():
    try:
        with open(MUTATION_LOCK) as fh:
            raw = fh.read().strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw[:300]}
    except FileNotFoundError:
        return None
    except OSError as e:
        return {"raw": f"lock unreadable: {e}"}


def acquire_oltp_lock():
    """Non-blocking exclusive flock shared with the DB-latency collector shim.

    Returns an open fd that MUST stay open for the process lifetime, or None
    if another OLTP probe already holds the lock (caller should skip).
    """
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def run_psql(sql, timeout=None):
    """Host-local psql (SOUL convention). Returns (stdout_lines, error). Never raises."""
    if timeout is None:
        timeout = PSQL_TIMEOUT_S
    up = sql.upper()
    for w in _FORBIDDEN:
        if w in up:
            return None, f"refused: statement is not read-only ({w.strip()})"
    cmd = PSQL + ["-c", f"SET statement_timeout='{STMT_TIMEOUT}'; {sql}"]
    env = os.environ.copy()
    env.setdefault("PGPASSWORD", "postgres")
    for k in KANBAN_ENV_OVERRIDES:
        env.pop(k, None)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, f"harness timeout after {timeout}s"
    if r.returncode != 0:
        return None, (r.stderr or r.stdout or "").strip()[:400]
    lines = [l.strip() for l in r.stdout.strip().splitlines()
             if l.strip() and l.strip() != "SET"]
    return lines, None


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_timeout_err(err: str | None) -> bool:
    if not err:
        return False
    u = err.lower()
    return any(s in u for s in (
        "harness timeout",
        "probe budget exhausted",
        "canceling statement",
        "statement timeout",
        "query_canceled",
    ))


def measure():
    """Returns a dict of the G-F1 fingerprint lag measurement (or error dict)."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    deadline = time.monotonic() + PROBE_BUDGET_S

    def run_budgeted(sql):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            return None, "harness timeout: probe budget exhausted"
        timeout = max(1, min(PSQL_TIMEOUT_S, int(remaining)))
        return run_psql(sql, timeout=timeout)

    # Core: producer liveness lag + 24h inserts. Bound through journeys.triggered_at
    # (indexed) + fingerprint correlation_id join. NEVER scan signal_fingerprints
    # without a triggered_at window — there is no created_at index.
    raw, err = run_budgeted("""
    SELECT count(*) AS inserts_24h,
           max(fp.created_at) AS max_created,
           round(extract(epoch FROM (now()-max(fp.created_at)))/3600.0, 3) AS lag_h
    FROM signal_journeys sj
    JOIN signal_fingerprints fp ON fp.correlation_id = sj.correlation_id
    WHERE sj.triggered_at >= now()-interval '24 hours';""")
    if err:
        return {"error": err, "idempotency_key": KEY, "measured_at": stamp}
    parts = (raw[0] if raw else "").split("|")
    inserts_24h = parts[0] if len(parts) > 0 else "0"
    max_created = parts[1] if len(parts) > 1 else None
    lag_h_raw = parts[2] if len(parts) > 2 else None
    # Unbounded COUNT(*) on signal_fingerprints was the 5.4M seq-scan; drop it.
    total = "bounded_24h"
    if not max_created or max_created in ("", "None"):
        max_created = None
    lag_h = num(lag_h_raw) if lag_h_raw not in (None, "", "None") else None

    # Per-day insert buckets (last 7d) - liveness history, journeys-windowed.
    raw2, err2 = run_budgeted("""
    SELECT to_char(date_trunc('day', fp.created_at), 'YYYY-MM-DD') AS d, count(*)
    FROM signal_journeys sj
    JOIN signal_fingerprints fp ON fp.correlation_id = sj.correlation_id
    WHERE sj.triggered_at >= now()-interval '7 days'
    GROUP BY 1 ORDER BY 1;""")
    if _is_timeout_err(err2):
        return {"error": err2, "idempotency_key": KEY, "measured_at": stamp}
    daily = {}
    if err2:
        daily = {"error": err2}
    else:
        for line in raw2 or []:
            f = line.split("|")
            if len(f) == 2:
                daily[f[0]] = f[1]

    # Lag distribution: fingerprint write time minus journey trigger time (minutes)
    # over the trailing 7d of journeys (indexed), not a 7d seq scan of fingerprints.
    raw3, err3 = run_budgeted("""
    WITH fp AS (
      SELECT extract(epoch FROM (fp.created_at - sj.triggered_at))/60.0 AS lag_min
      FROM signal_journeys sj
      JOIN signal_fingerprints fp ON fp.correlation_id = sj.correlation_id
      WHERE sj.triggered_at >= now()-interval '7 days'
        AND fp.created_at IS NOT NULL
        AND sj.triggered_at IS NOT NULL
    )
    SELECT round(percentile_cont(0.50) WITHIN GROUP (ORDER BY lag_min)::numeric, 2),
           round(percentile_cont(0.95) WITHIN GROUP (ORDER BY lag_min)::numeric, 2),
           round(percentile_cont(0.99) WITHIN GROUP (ORDER BY lag_min)::numeric, 2),
           round(max(lag_min)::numeric, 2),
           count(*)
    FROM fp;""")
    if _is_timeout_err(err3):
        return {"error": err3, "idempotency_key": KEY, "measured_at": stamp}
    if err3:
        lag_dist = {"error": err3}
    else:
        f = (raw3[0] if raw3 else "").split("|")
        lag_dist = {
            "p50_min": f[0] if len(f) > 0 else None,
            "p95_min": f[1] if len(f) > 1 else None,
            "p99_min": f[2] if len(f) > 2 else None,
            "max_min": f[3] if len(f) > 3 else None,
            "n": f[4] if len(f) > 4 else "0",
        }

    # 7d journey:fingerprint ratio. Journeys via triggered_at index; fingerprints
    # via the same nested-loop join. Never count(*) the whole fingerprints table.
    raw4, err4 = run_budgeted("""
    SELECT (SELECT count(*)
            FROM signal_journeys sj
            JOIN signal_fingerprints fp ON fp.correlation_id = sj.correlation_id
            WHERE sj.triggered_at >= now()-interval '7 days') AS n_fp_7d,
           (SELECT count(*) FROM signal_journeys
            WHERE triggered_at >= now()-interval '7 days') AS n_journeys_7d;""")
    if _is_timeout_err(err4):
        return {"error": err4, "idempotency_key": KEY, "measured_at": stamp}
    if err4:
        ratio7 = {"error": err4}
    else:
        f = (raw4[0] if raw4 else "").split("|")
        n_fp_7d = num(f[0]) if len(f) > 0 else None
        n_journeys_7d = num(f[1]) if len(f) > 1 else None
        ratio = (round(n_fp_7d / n_journeys_7d, 4)
                 if n_fp_7d is not None and n_journeys_7d and n_journeys_7d > 0 else None)
        ratio7 = {"n_fp_7d": f[0] if len(f) > 0 else None,
                  "n_journeys_7d": f[1] if len(f) > 1 else None,
                  "ratio": ratio}

    # Verdicts (deterministic on the snapshot)
    p95 = num(lag_dist.get("p95_min")) if isinstance(lag_dist, dict) else None
    ratio_v = num(ratio7.get("ratio")) if isinstance(ratio7, dict) else None

    ok_a = lag_h is not None and lag_h < LAG_ALERT_H
    ok_b = p95 is not None and p95 < P95_ALERT_MIN
    ok_c = ratio_v is not None and ratio_v >= RATIO_FLOOR

    verdicts = {
        "I-3 A producer liveness": {
            "target": f"lag_h < {LAG_ALERT_H:g}h",
            "measured": (f"{lag_h}h (max created_at {max_created})"
                         if lag_h is not None else f"NULL (max_created_at={max_created})"),
            "ok": ok_a if lag_h is not None else None},
        "I-3 B p95 lag": {
            "target": f"p95 < {P95_ALERT_MIN:g} min",
            "measured": (f"{p95} min (n={lag_dist.get('n','?')})"
                         if p95 is not None else "NULL"),
            "ok": ok_b if p95 is not None else None},
        "I-3 C 7d ratio": {
            "target": f">= {RATIO_FLOOR}",
            "measured": (f"{ratio_v} (fp={ratio7.get('n_fp_7d')} journeys="
                         f"{ratio7.get('n_journeys_7d')})"
                         if ratio_v is not None else "NULL"),
            "ok": ok_c if ratio_v is not None else None},
    }

    # Anomaly log entries: one per failing criterion, with escalation level.
    alerts = []
    if lag_h is not None and not ok_a:
        lvl = "CRITICAL" if lag_h >= CRIT_LAG_H else "WARN"
        alerts.append({"criterion": "I-3 A producer liveness",
                       "level": lvl, "target": f"lag_h < {LAG_ALERT_H:g}h",
                       "measured": f"{lag_h}h"})
    if p95 is not None and not ok_b:
        lvl = "CRITICAL" if p95 >= CRIT_P95_MIN else "WARN"
        alerts.append({"criterion": "I-3 B p95 lag",
                       "level": lvl, "target": f"p95 < {P95_ALERT_MIN:g} min",
                       "measured": f"{p95} min"})
    if ratio_v is not None and not ok_c:
        lvl = "CRITICAL" if ratio_v < CRIT_RATIO else "WARN"
        alerts.append({"criterion": "I-3 C 7d ratio",
                       "level": lvl, "target": f">= {RATIO_FLOOR}",
                       "measured": f"{ratio_v}"})
    if lag_h is None:
        alerts.append({"criterion": "I-3 A producer liveness",
                       "level": "CRITICAL",
                       "target": f"lag_h < {LAG_ALERT_H:g}h",
                       "measured": "UNKNOWN (no max created_at / empty table)"})

    return {
        "idempotency_key": KEY,
        "ticket": TICKET,
        "gap": GAP,
        "metric": METRIC,
        "measured_at": stamp,
        "core": {"total": total, "max_created": max_created, "lag_h": lag_h,
                 "inserts_24h": inserts_24h},
        "daily_inserts_7d": daily,
        "lag_distribution": lag_dist,
        "ratio_7d": ratio7,
        "verdicts": verdicts,
        "alerts": alerts,
        "alert_level": (max((a["level"] for a in alerts), default="OK")
                        if alerts else "OK"),
        "mutation_lock_active": read_mutation_lock() is not None,
    }


def record(m):
    """Idempotent append: dedupe by (idempotency_key, utc_day, measurement_hash)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    day = m["measured_at"][:10]
    if "error" in m:
        sig = json.dumps({"idempotency_key": KEY, "error": m["error"]}, sort_keys=True)
    else:
        # Stable facts only: verdict booleans + alert levels + lag rounded to
        # whole hours + p95 rounded to whole minutes + ratio rounded 2dp. Row
        # counts drift every second; the verdict does not change on that drift.
        p95_raw = m.get("lag_distribution", {}).get("p95_min") \
            if isinstance(m.get("lag_distribution"), dict) else None
        ratio_raw = m.get("ratio_7d", {}).get("ratio") \
            if isinstance(m.get("ratio_7d"), dict) else None
        stable = {
            "idempotency_key": KEY,
            "verdict_ok": {k: v.get("ok") for k, v in (m.get("verdicts") or {}).items()},
            "alert_level": m.get("alert_level"),
            "lag_h_rounded": (round(m.get("core", {}).get("lag_h"))
                              if isinstance(m.get("core", {}).get("lag_h"), (int, float))
                              else None),
            "p95_rounded": (round(num(p95_raw)) if p95_raw is not None else None),
            "ratio_rounded": (round(num(ratio_raw), 2) if num(ratio_raw) is not None
                              else None),
        }
        sig = json.dumps(stable, sort_keys=True)
    mhash = hashlib.sha1(sig.encode()).hexdigest()[:12]

    existing = []
    if os.path.exists(STATE):
        with open(STATE) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        existing.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

    dup = [r for r in existing
           if r.get("idempotency_key") == KEY
           and r.get("utc_day") == day
           and r.get("measurement_hash") == mhash]
    if dup:
        m["utc_day"] = day
        m["measurement_hash"] = mhash
        m["idempotent_noop"] = True
        m["appended"] = False
        return m, existing

    m["utc_day"] = day
    m["measurement_hash"] = mhash
    m["idempotent_noop"] = False
    m["appended"] = True
    existing.append(m)
    with open(STATE, "w") as fh:
        for r in existing:
            fh.write(json.dumps(r) + "\n")
    return m, existing


# ---- watchdog alert dedup (no-agent cron) ----------------------------------
def read_alert_state():
    try:
        return json.loads(Path(ALERT_STATE).read_text())
    except Exception:
        return {}


def write_alert_state(payload):
    Path(ALERT_STATE).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{ALERT_STATE}.tmp-{os.getpid()}"
    Path(tmp).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, ALERT_STATE)


def alert_fingerprint(m):
    """Stable signature of the breach: failing criteria + alert level + lag bucket."""
    fail = sorted(k for k, v in (m.get("verdicts") or {}).items()
                  if v.get("ok") is False)
    lag_bucket = round(m.get("core", {}).get("lag_h") or 0) \
        if m.get("core", {}).get("lag_h") is not None else None
    return hashlib.sha1(json.dumps(
        {"fail": fail, "level": m.get("alert_level"), "lag_bucket": lag_bucket},
        sort_keys=True).encode()).hexdigest()[:12]


def should_alert(m) -> bool:
    """Emit only on changed breach fingerprint, then once per REMIND_SECONDS."""
    import time
    now = int(time.time())
    fp = alert_fingerprint(m)
    state = read_alert_state()
    if state.get("fingerprint") != fp:
        write_alert_state({"fingerprint": fp, "first_seen": now, "last_alert": now})
        return True
    last = int(state.get("last_alert", 0))
    if now - last >= REMIND_SECONDS:
        write_alert_state({**state, "last_alert": now})
        return True
    write_alert_state({**state, "last_seen": now})
    return False


def clear_alert_state():
    try:
        Path(ALERT_STATE).unlink()
    except FileNotFoundError:
        pass


# ---- notifiers (best-effort, never fatal) ----------------------------------
def send_telegram(msg: str):
    try:
        subprocess.run(["hermes", "send", "-t", TELEGRAM_TARGET, "-m", msg],
                       timeout=30, capture_output=True, text=True)
        return True
    except Exception as e:
        print(f"(telegram alert failed: {e})", file=sys.stderr)
        return False


def create_kanban_card(title: str, body: str, idem_key: str):
    env = os.environ.copy()
    for k in KANBAN_ENV_OVERRIDES:
        env.pop(k, None)
    try:
        subprocess.run(
            ["hermes", "kanban", "--board", KANBAN_BOARD, "create", title,
             "--assignee", KANBAN_ASSIGNEE, "--priority", "2",
             "--idempotency-key", idem_key, "--body", body],
            timeout=30, capture_output=True, text=True, env=env)
        return True
    except Exception as e:
        print(f"(kanban card creation failed: {e})", file=sys.stderr)
        return False


def append_standing_comment(body: str) -> bool:
    env = os.environ.copy()
    for k in KANBAN_ENV_OVERRIDES:
        env.pop(k, None)
    try:
        r = subprocess.run(
            ["hermes", "kanban", "--board", KANBAN_BOARD, "comment",
             STANDING_CARD_IDEM_KEY, body[:4000], "--author", "gqt-fingerprint-lag-monitor"],
            timeout=30, capture_output=True, text=True, env=env)
        return r.returncode == 0
    except Exception as e:
        print(f"(standing card comment exception: {e})", file=sys.stderr)
        return False


def upsert_standing_card(title: str, body: str) -> bool:
    created = create_kanban_card(title, body, STANDING_CARD_IDEM_KEY)
    if created:
        return True
    print("(standing card create failed, attempting comment fallback)", file=sys.stderr)
    return append_standing_comment(body)


# ---- vault note -------------------------------------------------------------
def emit_vault(rec, history, appended):
    os.makedirs(os.path.dirname(VAULT_NOTE), exist_ok=True)
    latest = history[-1] if history else rec
    day = latest.get("utc_day", latest["measured_at"][:10])

    rows = []
    for k, v in latest.get("verdicts", {}).items():
        if v.get("ok") is True:
            badge = "PASS"
        elif v.get("ok") is False:
            badge = "**FAIL**"
        else:
            badge = "_UNKNOWN_"
        rows.append(f"| {k} | {v['target']} | {v['measured']} | {badge} |")
    verdict_table = "\n".join(rows)

    npass = sum(1 for v in latest.get("verdicts", {}).values() if v.get("ok") is True)
    nfail = sum(1 for v in latest.get("verdicts", {}).values() if v.get("ok") is False)
    nunk = sum(1 for v in latest.get("verdicts", {}).values() if v.get("ok") is None)

    daily = latest.get("daily_inserts_7d", {})
    daily_rows = ""
    if isinstance(daily, dict) and "error" not in daily:
        for d in sorted(daily):
            daily_rows += f"| {d} | {daily[d]} |\n"
    else:
        daily_rows = f"| _error: {daily.get('error','?')}_ |\n"

    ld = latest.get("lag_distribution", {})
    ld_row = (f"| p50 | {ld.get('p50_min','?')} min |\n"
              f"| p95 | {ld.get('p95_min','?')} min |\n"
              f"| p99 | {ld.get('p99_min','?')} min |\n"
              f"| max | {ld.get('max_min','?')} min |\n"
              f"| n (7d) | {ld.get('n','?')} |\n")

    hist_rows = []
    for h in history[-15:]:
        c = h.get("core", {})
        ld_h = h.get("lag_distribution", {})
        r7 = h.get("ratio_7d", {})
        hist_rows.append(
            f"| {h['measured_at']} | {c.get('lag_h','?')}h | "
            f"{c.get('inserts_24h','?')} | "
            f"{ld_h.get('p95_min','?') if isinstance(ld_h,dict) else '?'} min | "
            f"{r7.get('ratio','?') if isinstance(r7,dict) else '?'} | "
            f"{h.get('alert_level','OK')} | "
            f"`{h.get('measurement_hash','?')}` |"
        )
    hist_table = "\n".join(hist_rows) if hist_rows else "| _no prior history_ | - |"

    noop_note = ""
    if latest.get("idempotent_noop"):
        noop_note = (f"\n> _idempotent no-op: this run produced a measurement identical "
                     f"to one already recorded today (hash `{latest.get('measurement_hash','?')}`). "
                     f"No duplicate appended._\n")

    fail_block = ""
    if nfail:
        fail_block = (f"\n> [!caution] **I-3 criterion failing** as of "
                      f"{latest['measured_at']} UTC. Alert level "
                      f"**{latest.get('alert_level','?')}**. See verdict table below.\n")

    alerts_block = "_none_"
    if latest.get("alerts"):
        alerts_block = "\n".join(
            f"- **{a.get('criterion')}** [{a.get('level')}] target={a.get('target')} "
            f"measured={a.get('measured')}"
            for a in latest["alerts"])

    body = f"""---
title: "Fingerprint lag monitoring — G-F1 ({KEY})"
type: task-evidence
status: {'active' if nfail else 'complete'}
created: 2026-08-06
updated: {day}
confidence: high
tags:
  - fingerprint
  - G-F1
  - integrity
  - monitoring
  - sycode-trading
  - idempotency
sources:
  - "live:sycodetrading-supabase-db@signal_fingerprints (read-only SELECT)"
  - "live:sycodetrading-supabase-db@signal_journeys (read-only SELECT)"
  - kanban:t_b0f2fd14
  - kanban:t_eddbfd72
  - kanban:t_7d5ca285
knowledge_tier: verified-measurement
---

Idempotent fingerprint lag monitoring for **G-F1** (`signal_fingerprints.created_at`
producer lag vs `triggered_at`). Idempotency key: `{KEY}`. State/anomaly store:
`{STATE}`.

## Latest measurement — {latest['measured_at']} UTC{noop_note}{fail_block}

### Core

| field | value |
|---|---|
| total fingerprints | {latest['core'].get('total','?')} |
| max created_at | {latest['core'].get('max_created','?')} |
| producer lag (now - max created_at) | {latest['core'].get('lag_h','?')}h |
| inserts last 24h | {latest['core'].get('inserts_24h','?')} |
| alert level | **{latest.get('alert_level','?')}** |
| mutation_lock_active | {latest.get('mutation_lock_active', False)} |

### Daily inserts (last 7d)

| day (UTC) | inserts |
|---|---|
{daily_rows}

### Lag distribution (fp created_at - fp triggered_at, trailing 7d)

{ld_row}

### 7d journey:fingerprint ratio

| n_fp_7d | n_journeys_7d | ratio |
|---|---|---|
| {latest.get('ratio_7d',{}).get('n_fp_7d','?') if isinstance(latest.get('ratio_7d'),dict) else '?'} | {latest.get('ratio_7d',{}).get('n_journeys_7d','?') if isinstance(latest.get('ratio_7d'),dict) else '?'} | {latest.get('ratio_7d',{}).get('ratio','?') if isinstance(latest.get('ratio_7d'),dict) else '?'} |

### Acceptance verdicts — I-3 A–C

> {npass} PASS · **{nfail} FAIL** · {nunk} UNKNOWN (UNKNOWN is never green)

| criterion | target | measured | verdict |
|---|---|---|---|
{verdict_table}

### Anomalies this measurement

{alerts_block}

## Measurement history (last 15 records)

| measured_at | lag_h | inserts_24h | p95 (min) | ratio_7d | level | hash |
|---|---|---|---|---|---|---|
{hist_table}

## Acceptance criteria (WS01 I-3 / ticket t_eddbfd72)

- [ ] I-3 A: producer liveness lag < 1h (alert on exceedance; steady-state target < 2h per F1.5)
- [ ] I-3 B: p95 lag (`fingerprint_ts - journey_ts`) < 30 minutes (or documented batch SLO + catch-up SLA)
- [ ] I-3 C: 7d journey:fingerprint ratio >= 0.98 for eligible rows

## Idempotent store + anomaly log

The canonical queryable store is the JSONL at `{STATE}`. Each line is a measurement
record; records are deduped by `(idempotency_key, utc_day, measurement_hash)` so
re-runs within the same measurement window are no-ops, not duplicates. Any record
with a non-empty `alerts` list is an **anomaly** (criterion breached); the current
alert level is in `alert_level`.

**Vault query:** filter notes tagged `G-F1` and `monitoring`, or open this note directly.
**Script query:** `python3 ~/.hermes/scripts/sycode_fingerprint_lag_monitor.py --json`
returns the latest measurement + full history.
**Cron:** trading-devops `gqt-fingerprint-lag-monitor` (no-agent watchdog, every 15m)
runs `--watchdog`; on breach it prints an ALERT block, sends Telegram, and upserts a
standing kanban card (idem key `{STANDING_CARD_IDEM_KEY}`).

## Safety

Read-only. SELECT against `signal_fingerprints` / `signal_journeys` only. No DB
writes, no live trading, no secrets. Paper-only per the Grok quant seat policy.
Fingerprint producer restore (jobs.json + script) is a separate ticket
(`t_eddbfd72` / root-cause doc) and is NOT performed by this monitor.
"""
    with open(VAULT_NOTE, "w") as fh:
        fh.write(body)
    return VAULT_NOTE


def build_alert_block(m):
    alerts = m.get("alerts", [])
    c = m.get("core", {})
    ld = m.get("lag_distribution", {})
    r7 = m.get("ratio_7d", {})
    lines = [f"🔴 P1 G-F1 FINGERPRINT LAG ALERT — {m.get('alert_level','ALERT')}",
             f"   lag_h={c.get('lag_h')}h  max_created_at={c.get('max_created')}  "
             f"inserts_24h={c.get('inserts_24h')}",
             f"   p95={ld.get('p95_min')}m  p99={ld.get('p99_min')}m  max={ld.get('max_min')}m "
             f"(n={ld.get('n')})" if isinstance(ld, dict) else "   p95: N/A",
             f"   7d ratio={r7.get('ratio')} (fp={r7.get('n_fp_7d')} "
             f"journeys={r7.get('n_journeys_7d')})" if isinstance(r7, dict) else "   7d ratio: N/A"]
    for a in alerts:
        lines.append(f"   - {a['criterion']} [{a['level']}] target={a['target']} "
                     f"measured={a['measured']}")
    lines.append("   Read-only monitor (idem key gqt-integrity-2026-08-06-I3); producer "
                 "restore tracked on t_eddbfd72. Idempotency proof: "
                 f"hash={m.get('measurement_hash','?')} appended={m.get('appended')}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--emit-vault", action="store_true")
    ap.add_argument("--watchdog", action="store_true",
                    help="no-agent cron mode: silent when healthy, alert on breach")
    ap.add_argument("--dry-run", action="store_true",
                    help="watchdog: print alert but do NOT notify")
    args = ap.parse_args()

    skip_lock = os.getenv("GQT_FP_SKIP_LOCK", "") in ("1", "true", "yes")
    lock_fd = None
    if not skip_lock:
        lock_fd = acquire_oltp_lock()
        if lock_fd is None:
            if args.watchdog:
                return 0  # overlapping tick: silent skip, do not stack scans
            print("OLTP_PROBE_SKIP locked", file=sys.stderr)
            return 0

    rec = measure()
    if "error" in rec:
        msg = f"# G-F1 fingerprint lag monitor ({KEY}) - ERROR\n{rec['error']}"
        if args.json:
            print(json.dumps({"idempotency_key": KEY, "latest": rec}, indent=1))
        else:
            print(msg, file=sys.stderr)
        if args.watchdog and not args.dry_run:
            print(f"🔴 G-F1 FP MONITOR PROBE FAILURE — cannot read signal_fingerprints.\n"
                  f"   error: {rec['error']}\n"
                  f"   DB may be down → fingerprint lag is INVISIBLE. Escalate.")
            send_telegram(f"🔴 G-F1 FP MONITOR PROBE FAILURE — {rec['error'][:160]}")
        return 3

    rec, history = record(rec)
    noop = rec.get("idempotent_noop", False)

    if args.emit_vault:
        emit_vault(rec, history, rec.get("appended", False))

    if args.json:
        out = {"idempotency_key": KEY, "recorded": rec.get("appended", False),
               "idempotent_noop": noop, "latest": rec, "history_len": len(history),
               "history_last": history[-1] if history else None}
        print(json.dumps(out, indent=1))
        return 0

    alerts = rec.get("alerts", [])

    if args.watchdog:
        if not alerts:
            # healthy: clear stale alert state, stay silent
            clear_alert_state()
            return 0
        alert_text = build_alert_block(rec)
        if args.dry_run:
            print(alert_text)
            return 0
        if should_alert(rec):
            print(alert_text)
            send_telegram(alert_text)
            title = (f"P1 G-F1 FINGERPRINT LAG: {rec.get('alert_level')} "
                     f"lag_h={rec.get('core',{}).get('lag_h')}h")
            upsert_standing_card(title, alert_text + "\n\n"
                                 "[STANDING CARD] updated in-place on each alert tick "
                                 "(idem key gqt-fingerprint-lag-g-f1-standing).")
        return 0

    # default (interactive) mode
    if alerts:
        for a in alerts:
            print(f"ALERT {a['criterion']} [{a['level']}] target={a['target']} "
                  f"measured={a['measured']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
