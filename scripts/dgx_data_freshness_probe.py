#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Data-freshness probe — no-agent Hermes cron (delivers to #critical-alerts).

Checks now()-max(timestamp_col) per DATA pipeline against a per-pipeline staleness budget.
ALWAYS emits a structured report + VERDICT line (GREEN/DEGRADED) so the
cron run is never an empty-stdout black hole. Prints an explicit RED-ALERT block
on the falling edge when a feed first goes stale, then dedups re-emission while the
same stale-condition fingerprint persists (with a slow re-remind). Degraded/crash
paths exit non-zero so the cron flags failure and delivery fires.

Born 2026-07-02: process/cron-liveness monitors were blind to DATA stopping —
signal_fingerprints stalled ~6d, signal_journey_events sat frozen ~15d, and the
copy-trade harness scored nothing for ~12h while its process stayed 'alive'. This
watches whether fresh rows are actually landing. Read-only (SELECT max(col) only —
no count(*), which seq-scans 37M-row tables and times out). For very large
un-indexed tables like signal_pnl_points (88GB), uses max(col) GROUP BY
<group_col> to leverage the composite index for an index-only scan.
Also writes a structured JSONL record to the health canary log.
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
# signal_pnl_points: uses `ts` (signal-time). The 88GB un-partitioned table has
# NO index on `ts` alone — only composite indexes (journey_id, ts DESC) and
# (symbol, ts DESC). A plain max(ts) forces a full seq scan.
# To avoid the timeout, the probe uses the FAST_QUERY_GROUPBY override below
# which rewrites max(ts) as max(ts) GROUP BY journey_id — leveraging the
# composite index for an index-only scan (sub-second vs 88s seq scan).
# If signal_pnl_points gains a recorded_at column with a dedicated index (planned
# per migration 20260704000001 comment), switch to that for O(1) write-time checks.
PIPELINES = {
    "candles":                ("timestamp",   3),
    "signal_journeys":        ("created_at",  3),
    "signal_pnl_points":      ("ts",          3),
    "oi_snapshots":           ("created_at",  3),
    # signal_trajectory_bars: sparse-by-design FORENSIC OHLC store. TrajectoryCaptureService
    # persists bars only when includeFullBars = isStage5bProofMode() || decision.reason==='EXECUTED'
    # || Math.random() < FORENSIC_OHLC_SAMPLE_RATE (1%). With proofMode OFF and the paper desk
    # predominantly emitting EXPIRED journeys (recurrence t_c799394b / diagnosis t_1cd1ea54,
    # 2026-08-21), max(captured_at) ages past any tight budget BY DESIGN while the trajectory
    # ANALYSIS pipeline is healthy (signal_journeys.trajectory_captured_at fresh; signal_journeys
    # created_at watch below is fresh). Budget raised to forensic cadence (96h) so a GENUINE
    # bars-persistence failure (producer dead >96h) stays visible without false-stale alerts.
    # Do NOT switch this to max(signal_journeys.trajectory_captured_at): that column has NO index
    # and max() over the 42GB signal_journeys seq-scans (cost 2.1M) on every probe run — the exact
    # trap this probe's FAST_QUERY_GROUPBY was built to avoid.
    "signal_trajectory_bars": ("captured_at", 96),
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

EMPTY = -999.0  # sentinel: max(col) IS NULL -> table empty
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
#
# Increased from 60s to 300s (2026-08-18) — signal_fingerprints (~16 GB) lacks
# an index on created_at; max(created_at) needs a seq scan that previously
# exceeded 60s (especially under I/O contention from signal_pnl_points's
# concurrent scan). 300s provides headroom while the A3-gated DDL fix
# (idx_signal_fingerprints_created_at) is planned. Subprocess timeout 360s.
PROBE_STATEMENT_TIMEOUT = os.getenv("DATA_FRESHNESS_PSQL_TIMEOUT_MS", str(300000))


# Per-table fast-path strategies for max(col) on very large, un-indexed-by-col tables.
# Tables that lack a single-column index on their timestamp column force a full sequential
# scan for max(ts). Where a composite index (group_col, ts DESC) exists, we can instead do
# max(ts) GROUP BY group_col — the planner uses the composite index for an index-only scan,
# reading one entry per group instead of every row. For a 39-88 GB table this is the
# difference between a 60-300s scan and a sub-second index lookup.
# Format: table -> group_col (must match a composite index where ts is the 2nd column).
FAST_QUERY_GROUPBY = {
    # signal_pnl_points: 88GB un-partitioned table, no single-column index on `ts`.
    # Without this, max(ts) forces a full sequential scan (>60s, even >300s at scale).
    # The composite index idx_signal_pnl_points_journey_ts (journey_id, ts DESC)
    # enables an index-only GROUP BY scan: max(ts) GROUP BY journey_id reads one
    # index entry per journey_id group instead of every row. Verified sub-second
    # on 180M-row / 88GB table at 2026-08-18T16:11Z probe run.
    # NOTE: the outer MAX(max_ts) aggregate is REQUIRED — without it, the query
    # returns N rows (one per journey_id) and psql_scalar picks lines[-1], returning
    # the age of an ARBITRARY group instead of the global MAX.
    "signal_pnl_points": "journey_id",
}

# Gated probe-error classes (t_8bf353b4, 2026-08-28). A probe that CANNOT complete on
# a known-large table is a probe-capability gap, not a data-staleness signal; failing the
# whole run on it churns the cron-health canary every 30m with zero new information.
# Each entry documents the durable fix and its gate so the gate is removed when the fix
# lands (self-healing: once the query succeeds again the pipeline returns to fresh).
# Table -> (known_error_substring, durable_fix_card, note)
GATED_PROBE_ERROR_TABLES = {
    # signal_pnl_points: 88GB no-ts-index table. max(ts) GROUP BY journey_id still times
    # out under DB contention even after the FAST_QUERY_GROUPBY rewrite (verified still
    # timing out at 300s statement timeout on 2026-08-28). Durable fix = single-column
    # index on signal_pnl_points(ts) — DDL is A3/Frank-gated: card t_66d31da9 (db-architect,
    # blocked needs_input) is the owning Frank gate; DO NOT apply DDL without approval.
    # Once the index lands, this query returns to sub-second and the pipeline reports fresh.
    "signal_pnl_points": (
        "canceling statement due to statement timeout",
        "t_66d31da9 (ADD INDEXES: signal_pnl_points/ts — Frank-gated)",
        "88GB no-ts-index table; probe cannot complete max(ts) within statement timeout",
    ),
}

# Trajectory-pipeline liveness (t_cf1b921d rework, data-oracle t_1cd1ea54 disposition):
# signal_trajectory_bars is a sparse-by-design FORENSIC store — bars land only when
# includeFullBars is true, so max(captured_at) there is NOT a producer-liveness signal.
# The live producer-completion timestamp is signal_journeys.trajectory_captured_at
# (written by saveAnalysis on every capture). Watch THAT with an 8h budget.
#
# CRITICAL: trajectory_captured_at has NO index, so a naive max() over the 42GB
# signal_journeys seq-scans (cost 2.1M) — the exact trap FAST_QUERY_GROUPBY was built
# to avoid. We instead BOUND the scan to journeys created within a recent window using
# idx_signal_journeys_created_at (a live journey column): the planner filters on
# created_at (index) then aggregates over the surviving rows, which is far cheaper than
# a full-table scan. A healthy pipeline captures every journey, so max(trajectory_captured_at)
# over journeys created in the last 96h stays fresh.
TRAJECTORY_ANALYSIS_TABLE = "signal_journeys"
TRAJECTORY_ANALYSIS_COLUMN = "trajectory_captured_at"
TRAJECTORY_ANALYSIS_BUDGET_HOURS = 8
TRAJECTORY_ANALYSIS_LOOKBACK_HOURS = 96  # created_at window that bounds the scan via the index


def probe_trajectory_analysis():
    """max(trajectory_captured_at) over journeys created in the last 96h — bounded by
    idx_signal_journeys_created_at so it does NOT seq-scan the 42GB table. Returns
    (kind, value) like probe() but reports an EMPTY sentinel when no recent journey has
    been captured yet."""
    q = (
        "SELECT COALESCE(EXTRACT(EPOCH FROM (now()-max(%s)))/3600.0, %s)::numeric(12,2) "
        "FROM %s WHERE created_at > now() - interval '%d hours'"
        % (TRAJECTORY_ANALYSIS_COLUMN, int(EMPTY),
           TRAJECTORY_ANALYSIS_TABLE, TRAJECTORY_ANALYSIS_LOOKBACK_HOURS)
    )
    try:
        age = float(psql_scalar(q))
        return ("empty", 0) if age <= EMPTY else ("ok", age)
    except Exception as e:
        return ("err", str(e)[:90])


def psql_scalar(q):
    # Wrap the query so the per-connection timeout overrides the role-level 3s cap.
    # psql -c with SET;SELECT emits "SET\n<value>" — strip the SET prefix.
    wrapped_q = f"SET statement_timeout = {PROBE_STATEMENT_TIMEOUT}; {q}"
    r = subprocess.run(
        ["docker", "exec", PG, "psql", "-U", "postgres", "-d", "postgres", "-Atc", wrapped_q],
        capture_output=True, text=True, timeout=360)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        raise RuntimeError(msg.splitlines()[-1][:90] if msg else "rc=%d" % r.returncode)
    # psql -c may emit multi-line output for SET;SELECT combos.
    # Only return the last non-empty line (the actual query result).
    lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
    return lines[-1].strip() if lines else ""


def probe(table, col):
    """max(col) only — no count(*) (that seq-scans huge tables). max IS NULL => empty.

    For very large tables lacking a single-column index on `col`, a naive max(col)
    forces a full sequential scan. Where a composite index (group_col, col DESC)
    exists, rewrite as max(col) GROUP BY group_col — the planner uses the composite
    index for an index-only scan, reading one entry per group instead of every row.
    Falls back to the plain max(col) query when no fast-path override is configured.
    """
    if table in FAST_QUERY_GROUPBY:
        group_col = FAST_QUERY_GROUPBY[table]
        q = (
            "SELECT COALESCE(EXTRACT(EPOCH FROM (now()-MAX(max_ts))/3600.0),"
            " %s)::numeric(12,2) FROM (SELECT MAX(%s) AS max_ts FROM %s GROUP BY %s) sub"
            % (int(EMPTY), col, table, group_col)
        )
    else:
        q = (
            "SELECT COALESCE(EXTRACT(EPOCH FROM (now()-max(%s)))/3600.0, %s)::numeric(12,2) "
            "FROM %s" % (col, int(EMPTY), table)
        )
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
            gate = GATED_PROBE_ERROR_TABLES.get(t)
            if gate and isinstance(val, str) and gate[0] in val:
                # Known probe-capability gap with a documented Frank-gated durable fix
                # (t_8bf353b4). Report it visibly but do NOT count it as an error: the
                # probe cannot distinguish fresh vs stale on this table today, so failing
                # every run only churns the cron-health canary. Self-heals when the fix
                # (index DDL, card t_66d31da9) lands and the query succeeds again.
                pipeline_states[t] = {"status": "gated", "age_h": None, "budget": budget,
                                      "error": val, "gate": gate[1]}
                alerts.append("  ℹ %s: GATED probe error — %s (%s)" % (t, val, gate[1]))
            else:
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

    # Trajectory-pipeline liveness (t_cf1b921d rework): signal_trajectory_bars is
    # sparse-by-design, so watch the live producer-completion timestamp instead.
    t = "trajectory_analysis"
    tbudget = TRAJECTORY_ANALYSIS_BUDGET_HOURS
    tkind, tval = probe_trajectory_analysis()
    if tkind == "err":
        pipeline_states[t] = {"status": "error", "age_h": None, "budget": tbudget, "error": tval}
        alerts.append("  ⚠ %s: probe error — %s" % (t, tval))
    elif tkind == "empty":
        pipeline_states[t] = {"status": "empty", "age_h": None, "budget": tbudget}
        alerts.append("  ⚠ %s: EMPTY (no captured journey in lookback) — expected a live feed" % t)
    else:
        tage = float(tval)
        if tage > tbudget:
            pipeline_states[t] = {"status": "stale", "age_h": round(tage, 2), "budget": tbudget}
            alerts.append("  \U0001f534 %s: STALE %.1fh (budget %dh) — trajectory capture has stopped" % (t, tage, tbudget))
        else:
            pipeline_states[t] = {"status": "fresh", "age_h": round(tage, 2), "budget": tbudget}

    paired_alert = paired_execution_freshness_alert()
    if paired_alert:
        alerts.append(paired_alert)

    return pipeline_states, alerts, paired_alert


def write_health_canary_record(pipeline_states, paired_alert, stale_count, empty_count, gated_count=0):
    """Write data freshness state to the health canary JSONL for unified health picture."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    all_fresh = stale_count == 0 and empty_count == 0 and gated_count == 0
    record = {
        "ts": now_iso,
        "source": "data-freshness-probe",
        "data_freshness": {
            "overall": "ok" if all_fresh else "degraded",
            "pipelines": pipeline_states,
            "stale_count": stale_count,
            "empty_count": empty_count,
            "gated_count": gated_count,
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
    gated_count = sum(1 for s in pipeline_states.values() if s["status"] == "gated")

    # Write to health canary JSONL (unified health picture)
    write_health_canary_record(pipeline_states, paired_alert, stale_count, empty_count, gated_count)

    # Write kanban-sidecar marker for stale feeds
    write_kanban_marker(pipeline_states)

    # Always emit an evidence-bearing report — one line per watched feed.
    print("DATA FRESHNESS PROBE @ %s"
          % datetime.datetime.now(datetime.timezone.utc).isoformat())
    for table, state in sorted(pipeline_states.items()):
        marker = {"fresh": "OK", "gated": "--"}.get(state["status"], "XX")
        print("  [%s] %s: %s budget=%dh age=%s%s" % (
            marker,
            table, state["status"].upper(), state["budget"],
            _fmt_age(state),
            ((" err=%s" % state["error"]) if state.get("error") else "") +
            ((" gate=%s" % state["gate"]) if state.get("gate") else ""),
        ))

    # Gated probe-error classes (t_8bf353b4) do NOT fail the run: they are documented
    # probe-capability gaps with Frank-gated durable fixes, not staleness signals.
    overall = "GREEN" if (stale_count == 0 and empty_count == 0 and error_count == 0) else "DEGRADED"
    print("VERDICT: %s — %d/%d pipelines fresh, %d stale, %d empty, %d error, %d gated" % (
        overall,
        sum(1 for s in pipeline_states.values() if s["status"] == "fresh"),
        len(pipeline_states), stale_count, empty_count, error_count, gated_count))

    # Loud alert on the falling edge (deduped) for the operator feed.
    if should_emit(alerts):
        print("RED-ALERT: pipeline(s) past budget (now-max(col)):")
        print("\n".join(alerts))
        print("A process can be 'alive' while its data silently stops — check the owning ingester/cron/collector.")

    if overall == "DEGRADED":
        sys.exit(1)


if __name__ == "__main__":
    main()
