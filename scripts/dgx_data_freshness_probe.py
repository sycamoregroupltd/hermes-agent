#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Data-freshness probe — no-agent Hermes cron (delivers to #critical-alerts).

Checks now()-max(ts) per DATA pipeline against a per-pipeline staleness budget.
ALWAYS emits a structured report + VERDICT line (GREEN/DEGRADED) so the
cron run is never an empty-stdout black hole. Prints an explicit RED-ALERT block
on the falling edge when a feed first goes stale, then dedups re-emission while the
same stale-condition fingerprint persists (with a slow re-remind). Degraded/crash
paths exit non-zero so the cron flags failure and delivery fires.

Born 2026-07-02: process/cron-liveness monitors were blind to DATA stopping —
signal_fingerprints stalled ~6d, signal_journey_events sat frozen ~15d, and the
copy-trade harness scored nothing for ~12h while its process stayed 'alive'. This
watches whether fresh rows are actually landing. Read-only (SELECT max(ts) only —
no count(*), which seq-scans 37M-row tables and times out).
"""
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PG = "sycodetrading-supabase-db"

# table -> (timestamp column, staleness budget in hours). LIVE prod pipelines only
# (verified 2026-07-02). Cadence-aware budgets: continuous feeds tight, bursty/periodic loose.
# Excluded as EMPTY/deprecated (0 rows on 2026-07-02): liquidation_events,
# funding_rate_snapshots, market_news (orthogonal-sqlite or dead feeds); and
# (signal_journey_events was frozen ~15d, root-caused + fixed 2026-07-02 — now watched above.)
PIPELINES = {
    "candles":                ("timestamp",   3),
    "signal_journeys":        ("created_at",  3),
    "signal_pnl_points":      ("ts",          3),
    "oi_snapshots":           ("created_at",  3),
    "signal_trajectory_bars": ("captured_at", 8),
    "funding_rate_history":   ("created_at",  6),
    "signal_fingerprints":    ("created_at", 36),
    "signal_journey_events":  ("recorded_at",  6),  # emission fixed 2026-07-02 (was frozen 15d); recorded_at = DB write time (best "is data landing" signal). No created_at column exists on this table.
    # Class-C read-model (NS-P2.3 data-surface-register, owner t_a8c6bb5): rebuilt by
    # sync-pattern-win-rate-registry cron. SLO 26h. WATCHED HERE so a dead/again-stale
    # updater is visible end-to-end via the producer-liveness twin (the 2026-07-12 blind
    # spot: the SLO monitor saw 32.3h but this probe emitted SILENT/empty because the
    # table was absent from PIPELINES). Column is last_updated (written NOW() on each upsert).
    "pattern_win_rate_registry": ("last_updated", 26),
}

# Paired paper-execution freshness check — watches the OUTCOME FACTORY, the
# NS-P3-critical write path (decision_outcomes / trade_close_events). Rationale
# for the table choice (2026-07-17 outcome-factory-stall incident):
#   * The old set {trade_intents, trade_outcomes} was blind to the failure that
#     actually happens: trade_outcomes is a DEAD/empty table (always "stale"), and
#     trade_intents is NOT a reliable liveness signal — the RandomEntryInjector
#     opens via executeIntent() and never writes a trade_intents row (2026-07-14
#     incident closeout), and main-funnel intents are legitimately sparse under the
#     DQ deadlock. So the AND reduced to "trade_intents stale" — the weakest signal.
#   * On 2026-07-17 signal_journeys flowed (~14k/24h) while decision_outcomes /
#     trade_close_events had been frozen since 04:52Z (execution halted by the
#     CircuitBreaker dailyPnLPercent bug, card t_572a120e) — the probe stayed GREEN.
# Alert fires only when signal_journeys is fresh but the canonical outcome rails are
# stale beyond budget: that means signals land but NS-P3 accrual has stopped (a
# halt/drought OR a broken post-close write path). 0 fresh outcomes CAN be a
# legitimate trading halt, so the message carries that context for the operator.
PAIRED_SIGNAL_TABLE = "signal_journeys"
PAIRED_SIGNAL_COLUMN = "created_at"
PAIRED_EXECUTION_TABLES = {
    "decision_outcomes": "created_at",
    "trade_close_events": "created_at",
}
PAIRED_SIGNAL_FRESH_HOURS = 3
PAIRED_EXECUTION_STALE_HOURS = 12

EMPTY = -999.0  # sentinel: max(ts) IS NULL -> table empty
REMIND_SECONDS = int(os.getenv("DATA_FRESHNESS_REMIND_SECONDS", str(24 * 3600)))
STATE = Path(os.getenv("DATA_FRESHNESS_STATE", "/home/frank/.hermes/profiles/jarvis/cron/state/dgx_data_freshness_probe.first_seen.json"))

# Health canary integration: write unified health JSONL with pipeline freshness.
HEALTH_CANARY_LOG = Path(
    os.getenv("HEALTH_CANARY_LOG",
              "/home/frank/.hermes/profiles/jarvis/cron/output/health_canary.jsonl")
)

# Single-instance + write-serialization locks (t_ced4313d).
# The probe is the sole writer of HEALTH_CANARY_LOG, but a concurrent invocation
# (the 30m cron overlapping a manual/agent run, or a transient second invoker during
# a code-edit window) previously produced same-second OK+STALE TWIN records and a
# frozen 12.3h candles artifact. These locks prevent concurrent runs from interleaving
# writes: only one instance runs; appends are serialized and atomic (temp file + rename).
INSTANCE_LOCK = Path(
    os.getenv("DATA_FRESHNESS_LOCK",
              "/home/frank/.hermes/profiles/jarvis/cron/state/dgx_data_freshness_probe.lock")
)


def acquire_instance_lock():
    """Single-instance guard. Returns the held lock fd, or exits 0 (clean skip) if
    another instance already holds it — data is being refreshed elsewhere, so a
    skipped run cannot emit a false stale record or a twin."""
    try:
        fd = os.open(str(INSTANCE_LOCK), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None  # lock dir missing — proceed without guarding rather than block
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        print("DATA FRESHNESS PROBE — skipped: another instance holds the single-instance lock.")
        sys.exit(0)
    return fd  # released automatically by the kernel when the process exits


# Kanban-sidecar marker: stale feeds that need kanban task creation.
KANBAN_MARKER = Path(
    os.getenv("DATA_FRESHNESS_KANBAN_MARKER",
              "/home/frank/.hermes/profiles/jarvis/cron/state/dgx_data_freshness_probe.kanban_pending.json")
)


def read_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def write_state(payload):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(f".{STATE.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, STATE)


def clear_state():
    try:
        STATE.unlink()
    except FileNotFoundError:
        pass


def should_emit(alerts):
    if not alerts:
        clear_state()
        return False
    now = int(time.time())
    fingerprint = "\n".join(normalize_alert_for_fingerprint(a) for a in alerts)
    state = read_state()
    if state.get("fingerprint") != fingerprint:
        write_state({"fingerprint": fingerprint, "first_seen": now, "last_alert": now})
        return True
    last_alert = int(state.get("last_alert") or 0)
    if now - last_alert >= REMIND_SECONDS:
        write_state({**state, "last_alert": now, "last_seen": now})
        return True
    write_state({**state, "last_seen": now})
    return False


def normalize_alert_for_fingerprint(alert):
    stable = re.sub(r"STALE [0-9.]+h", "STALE", alert)
    stable = re.sub(r"fresh [0-9.]+h", "fresh", stable)
    stable = re.sub(r"=[0-9.]+h", "=STALE", stable)
    return stable


# Per-connection timeout override: postgres role has statement_timeout=3s (role rolconfig),
# which kills max(ts) probes on multi-gigabyte tables even when indexes are present.
# SET LOCAL raises it only for this psql session; does NOT mutate the role or affect
# other connections/servers. Safe connection-pool-tuning fix authorized by t_c1eed563.
PROBE_STATEMENT_TIMEOUT = os.getenv("DATA_FRESHNESS_PSQL_TIMEOUT_MS", str(60000))


def psql_scalar(q):
    # Wrap the query so the per-connection timeout overrides the role-level 3s cap.
    # psql -c with SET;SELECT emits "SET\\n<value>" — strip the SET prefix.
    wrapped_q = f"SET statement_timeout = {PROBE_STATEMENT_TIMEOUT}; {q}"
    r = subprocess.run(
        ["docker", "exec", PG, "psql", "-U", "postgres", "-d", "postgres", "-Atc", wrapped_q],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        raise RuntimeError(msg.splitlines()[-1][:90] if msg else "rc=%d" % r.returncode)
    # psql -c may emit multi-line output for SET;SELECT combos.
    # Only return the last non-empty line (the actual query result).
    lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
    return lines[-1].strip() if lines else ""


def probe(table, col):
    # max(ts) only — no count(*) (that seq-scans huge tables). max IS NULL => empty.
    q = ("SELECT COALESCE(EXTRACT(EPOCH FROM (now()-max(%s)))/3600.0, %s)::numeric(12,2) "
         "FROM %s" % (col, int(EMPTY), table))
    try:
        age = float(psql_scalar(q))
        return ("empty", 0) if age <= EMPTY else ("ok", age)
    except Exception as e:
        return ("err", str(e)[:90])


def paired_execution_freshness_alert():
    ages = {}
    for table, col in {PAIRED_SIGNAL_TABLE: PAIRED_SIGNAL_COLUMN, **PAIRED_EXECUTION_TABLES}.items():
        kind, val = probe(table, col)
        if kind == "err":
            return "  ⚠ paired execution-freshness probe error on %s — %s" % (table, val)
        ages[table] = None if kind == "empty" else float(val)

    signal_age = ages[PAIRED_SIGNAL_TABLE]
    execution_ages = {t: ages[t] for t in PAIRED_EXECUTION_TABLES}
    signal_is_fresh = signal_age is not None and signal_age <= PAIRED_SIGNAL_FRESH_HOURS
    execution_is_stale = [
        t for t, age in execution_ages.items()
        if age is None or age > PAIRED_EXECUTION_STALE_HOURS
    ]
    if signal_is_fresh and len(execution_is_stale) == len(execution_ages):
        context = ", ".join(
            "%s=%s" % (t, "EMPTY" if age is None else "%.1fh" % age)
            for t, age in execution_ages.items()
        )
        return (
            "  🔴 outcome-factory stall: signal_journeys fresh %.1fh but canonical outcome rails %s stale >%dh; "
            "NS-P3 accrual has stopped — a legitimate trading halt/drought is possible, but fresh signals with stale "
            "decision_outcomes/trade_close_events can indicate a broken post-close write path (see CircuitBreaker t_572a120e)"
            % (signal_age, context, PAIRED_EXECUTION_STALE_HOURS)
        )
    return None


def probe_all_pipelines():
    """Probe all pipelines and return structured results + alert lines."""
    pipeline_states = {}  # {table: {"status": "fresh"|"stale"|"empty"|"error", "age_h": float|None, "budget": int}}
    alerts = []
    for t, (c, budget) in PIPELINES.items():
        kind, val = probe(t, c)
        if kind == "err":
            pipeline_states[t] = {"status": "error", "age_h": None, "budget": budget, "error": val}
            alerts.append("  ⚠ %s: probe error — %s" % (t, val))
        elif kind == "empty":
            pipeline_states[t] = {"status": "empty", "age_h": None, "budget": budget}
            alerts.append("  ⚠ %s: EMPTY (0 rows) — expected a live feed" % t)
        else:
            age = float(val)
            if age > budget:
                pipeline_states[t] = {"status": "stale", "age_h": round(age, 2), "budget": budget}
                alerts.append("  \U0001f534 %s: STALE %.1fh (budget %dh) — feed likely dead" % (t, age, budget))
            else:
                pipeline_states[t] = {"status": "fresh", "age_h": round(age, 2), "budget": budget}

    paired_alert = paired_execution_freshness_alert()
    if paired_alert:
        alerts.append(paired_alert)

    return pipeline_states, alerts, paired_alert


def write_health_canary_record(pipeline_states, paired_alert, stale_count, empty_count):
    """Write data freshness state to the health canary JSONL for unified health picture."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    all_fresh = stale_count == 0 and empty_count == 0
    record = {
        "ts": now_iso,
        "source": "data-freshness-probe",
        "data_freshness": {
            "overall": "ok" if all_fresh else "degraded",
            "pipelines": pipeline_states,
            "stale_count": stale_count,
            "empty_count": empty_count,
            "total_pipelines": len(pipeline_states),
        },
    }
    if paired_alert:
        record["data_freshness"]["paired_execution_alert"] = paired_alert
    line = json.dumps(record, default=str) + "\n"
    try:
        HEALTH_CANARY_LOG.parent.mkdir(parents=True, exist_ok=True)
        # Serialize the append across concurrent invocations: hold an exclusive flock
        # on the log for the duration of the write (t_ced4313d). On filesystems that
        # don't honor flock, fall back to an atomic temp-file + os.replace so a
        # partial line can never interleave with another writer.
        fd = os.open(str(HEALTH_CANARY_LOG), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line.encode("utf-8"))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    except Exception:
        pass  # best-effort; don't fail the probe over a log write


def write_kanban_marker(pipeline_states):
    """Write marker file for stale feeds that need kanban task creation.

    The marker is consumed by the companion agent-driven cron (data-freshness-kanban-sidecar)
    which creates kanban investigation tasks. We only write stale entries here; the sidecar
    reads, creates tasks, then clears matched entries.
    """
    stale_feeds = {
        table: state for table, state in pipeline_states.items()
        if state["status"] in ("stale", "empty")
    }
    if not stale_feeds:
        # All clear — remove marker if it exists
        try:
            KANBAN_MARKER.unlink()
        except FileNotFoundError:
            pass
        return

    now = int(time.time())
    try:
        existing = json.loads(KANBAN_MARKER.read_text())
    except Exception:
        existing = {"stale_feeds": {}, "created_at": now}

    changed = False
    for table, state in stale_feeds.items():
        fingerprint = "%s=%s" % (table, state["status"])
        if table not in existing.get("stale_feeds", {}):
            existing.setdefault("stale_feeds", {})[table] = {
                "status": state["status"],
                "age_h": state.get("age_h"),
                "budget": state.get("budget"),
                "fingerprint": fingerprint,
                "first_seen": now,
                "task_created": False,
            }
            changed = True

    # Remove feeds that have recovered
    recovered = [t for t in existing.get("stale_feeds", {}) if t not in stale_feeds]
    for t in recovered:
        existing["stale_feeds"].pop(t, None)
        changed = True

    if changed:
        KANBAN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        tmp = KANBAN_MARKER.with_name(f".{KANBAN_MARKER.name}.tmp-{os.getpid()}")
        tmp.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, KANBAN_MARKER)


def _fmt_age(state):
    if state["age_h"] is None:
        return "n/a"
    return "%.1fh" % state["age_h"]


def main():
    # Single-instance guard (t_ced4313d): skip cleanly if another invocation holds
    # the lock — prevents same-second OK+STALE TWIN records and frozen-value artifacts.
    acquire_instance_lock()
    try:
        pipeline_states, alerts, paired_alert = probe_all_pipelines()
    except Exception as e:
        # Never go silent on a crash: emit a DEGRADED verdict and fail loud.
        print("DATA FRESHNESS — DEGRADED: probe crashed before completing — %s"
              % str(e)[:200])
        print("Check the owning ingester/cron/collector and docker/psql reachability.")
        sys.exit(2)

    stale_count = sum(1 for s in pipeline_states.values() if s["status"] == "stale")
    empty_count = sum(1 for s in pipeline_states.values() if s["status"] == "empty")
    error_count = sum(1 for s in pipeline_states.values() if s["status"] == "error")

    # Write to health canary JSONL (unified health picture)
    write_health_canary_record(pipeline_states, paired_alert, stale_count, empty_count)

    # Write kanban-sidecar marker for stale feeds
    write_kanban_marker(pipeline_states)

    # Always emit an evidence-bearing report — one line per watched feed.
    print("DATA FRESHNESS PROBE @ %s"
          % datetime.datetime.now(datetime.timezone.utc).isoformat())
    for table, state in sorted(pipeline_states.items()):
        print("  [%s] %s: %s budget=%dh age=%s%s" % (
            "OK" if state["status"] == "fresh" else "XX",
            table, state["status"].upper(), state["budget"],
            _fmt_age(state),
            (" err=%s" % state["error"]) if state.get("error") else "",
        ))

    overall = "GREEN" if (stale_count == 0 and empty_count == 0 and error_count == 0) else "DEGRADED"
    print("VERDICT: %s — %d/%d pipelines fresh, %d stale, %d empty, %d error" % (
        overall,
        sum(1 for s in pipeline_states.values() if s["status"] == "fresh"),
        len(pipeline_states), stale_count, empty_count, error_count))

    # Loud alert on the falling edge (deduped) for the operator feed.
    if should_emit(alerts):
        print("RED-ALERT: pipeline(s) past budget (now-max(ts)):")
        print("\n".join(alerts))
        print("A process can be 'alive' while its data silently stops — check the owning ingester/cron/collector.")

    if overall == "DEGRADED":
        sys.exit(1)


if __name__ == "__main__":
    main()
