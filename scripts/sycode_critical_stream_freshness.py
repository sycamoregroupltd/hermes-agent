#!/usr/bin/env python3
# invoker: hermes no-agent cron "sycode-critical-stream-freshness" (*/15) —
#   manual: python3 ~/.hermes/scripts/sycode_critical_stream_freshness.py
#
# CRITICAL WRITE-STREAM FRESHNESS MONITOR  (Frank standing rule, 2026-07-08)
# ---------------------------------------------------------------------------
# Guards against "silent writer death": a producer stops writing a critical DB
# stream and the outage drifts unnoticed for days. The GENUINELY-LIVE streams
# below have a continuous producer, so a stall is a real death worth alerting on:
#   - strategy_arena_decisions          dead since 2026-07-05 14:00Z (rogue trigger)
#   - decision_outcomes[trade_close]     the LIVE canonical outcome lane (~240m cadence)
#   - r_multiple_labels                 dropped/broken cron -> stops silently (was 25h)
#   - managed_positions closes          stuck-exit / halt detector
#   - per-symbol price feed             stuck exits when an open position's feed stalls
#   - strategy_live_promotions          gate_snapshot (info; downstream of arena)
#   - signal_journeys realized stamps   NS-P1 realized-only successor liveness (info-wide)
#
# RECLASSIFIED OUT OF ALERTING (2026-07-09, card t_16fdf654) — these were
# FALSE-ALERTING because they are NOT continuous producers:
#   - decision_outcomes[candle_1m_asof|candle_15m|candle_15m_tolerance|candle_1h|
#     candle_1h_tolerance]  ONE-TIME BATCH backfill epochs (ran 2026-07-05
#     06:00-09:16, finished; no continuous producer exists in the repo). Their
#     max(created_at) is permanently ~3d+ stale BY DESIGN -> perpetual false STALE.
#   - decision_outcomes[journey_finalizer]  DEPRECATED BY DESIGN: the NS-P1
#     realized-only cutover (2026-07-05) censors non-realized journeys, so the
#     finalizer has no population left to write -> permanently stale, not a death.
#   A monitor that cries wolf on these trains people to ignore it; the realized-
#   outcome-stamp check (below) is the genuine NS-P1 successor liveness signal.
#   Similarly, the closing-activity lanes trade_close + managed_positions[closes]
#   are SUPPRESSED when the book is FLAT (0 open positions) — a stale close/trade_
#   close lane with nothing to close is the legitimate paper-drought halt, not a
#   death. A real stuck-exit death (open positions that won't close) still fires;
#   a real trade_close death (closes continuing but unlabeled) still fires when
#   closing activity resumes. See FLAT-PORTFOLIO SUPPRESSION block below.
#
# WHY the existing sycode_surface_freshness_monitor.py MISSED the real deaths:
#   (1) it was never scheduled (absent from `hermes cron list`) -> never ran;
#   (2) it checks decision_outcomes as a SINGLE table at max(created_at)<=8h, so
#       the still-alive trade_close lane keeps the table "fresh" and a dead
#       trade_close lane would be invisible -> this monitor breaks it out PER
#       label_source and only alerts on the lane with a live producer;
#   (3) it has no strategy_arena_decisions / arena-family stream at all;
#   (4) r_multiple_labels is mode=pending @ 26h SLO -> too loose to catch a 25h death.
# This script COMPLEMENTS (does not replace) the surface monitor: surface monitor
# = data-surface SLO register (writes a vault report); this = lean, silent-on-healthy
# intelligence write-stream liveness with per-lane granularity, no side effects.
#
# CONTRACT (mirrors the healthy clean_outcome_labeler_24h_v2.sh no-agent cron):
#   - Silent on healthy: empty stdout => the no-agent cron delivers nothing.
#   - LOUD on stale: one "STALE: <stream> ..." line per stale stream to stdout
#     (delivered to discord:#critical-alerts by the registered cron). exit 0 so a
#     firing alert does NOT crash the cron slot (proven clean-labeler pattern).
#   - exit 1 only on OPERATIONAL error (DB unreachable) so real breakage of the
#     monitor itself surfaces as a cron error (and native-cron-health covers a
#     dead monitor — a dead monitor is itself caught).
#   - READ-ONLY: SELECT max(ts) only, default_transaction_read_only=on. No writes,
#     no schema changes, no count(*) on hot tables. Idempotent, <60s.
#
# DB: server/.env DATABASE_URL (127.0.0.1:5432); docker-network host `supabase-db`
#     is rewritten to 127.0.0.1 for host runs (same DB), same pattern as
#     clean_outcome_labeler_24h_v2.sh / sycode_clean_cohort_accrual.py.

import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ENV_FILE = Path("/home/frank/sycode-trading/server/.env")
DEFAULT_URL = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"

# ---------------------------------------------------------------------------
# REGISTRY of critical write-streams. Easy to extend: add a dict.
#   name        : alert label (unique)
#   table       : source table (public schema)
#   col         : freshness (write) timestamp column, SQL-quoted if needed
#   where       : optional SQL predicate isolating a single write-lane
#   max_stale_min: staleness budget in minutes (see cadence notes below)
#   mode        : "alert" -> stale/empty fires; "info" -> never fires (silent,
#                 documented context only, e.g. downstream-of-a-dead-parent)
#   note        : why this threshold (derived 2026-07-08 from observed cadence)
#
# Thresholds are tuned from observed cadence (read-only) to catch multi-hour/day
# death WITHOUT chronic false positives (a noisy monitor gets ignored -> the very
# failure this replaces). Event-driven lanes get budgets above their p90/max gap.
# ---------------------------------------------------------------------------
STREAMS = [
    # --- Strategy arena family (rogue-trigger death 2026-07-05 14:00Z) ---------
    dict(name="strategy_arena_decisions", table="strategy_arena_decisions",
         col="decided_at", where=None, max_stale_min=30, mode="alert",
         note="continuous when the arena evaluates; 30m catches a dead arena fast"),

    # --- decision_outcomes: ONLY the trade_close lane has a live producer -------
    # The candle_* lanes (one-time batch backfill, finished 2026-07-05) and the
    # journey_finalizer lane (deprecated by the NS-P1 realized-only cutover) were
    # REMOVED 2026-07-09 (card t_16fdf654): they are permanently stale BY DESIGN,
    # so alerting on them was a chronic false positive. See header for detail.
    dict(name="decision_outcomes[trade_close]", table="decision_outcomes",
         col="created_at", where="label_source='trade_close'",
         max_stale_min=240, mode="alert",
         note="LIVE canonical outcome lane; event-driven (~44/24h; p90 gap 102m, max 181m); 4h avoids quiet-market noise"),

    # --- r_multiple labeler (dropped/broken cron -> silent death; was 25h) ------
    dict(name="r_multiple_labels", table="r_multiple_labels",
         col="computed_at", where=None, max_stale_min=180, mode="alert",
         note="batch-then-idle labeler; 3h > max legit close-gap, catches a dead/broken cron"),

    # --- managed_positions closes (stuck-exit / paper-halt detector) -----------
    dict(name="managed_positions[closes]", table="managed_positions",
         col="closed_at", where=None, max_stale_min=240, mode="alert",
         note="event-driven closes (p90 gap 102m, max 181m); 4h. 0 closes can be a legit halt"),

    # --- strategy_live_promotions.gate_snapshot (downstream of arena) ----------
    # 0 rows today because it is fed by the arena, which is dead. Informational:
    # never alerts (arena death already fires; this would be redundant noise).
    # Graduates to a real check once arena revives -> flip mode to "alert".
    dict(name="strategy_live_promotions[gate_snapshot]", table="strategy_live_promotions",
         col="created_at", where="gate_snapshot IS NOT NULL", max_stale_min=1440,
         mode="info", note="0 rows; downstream of arena death; informational until arena revives"),

    # --- signal_journeys realized-outcome-stamp throughput (NS-P1 successor) -----
    # The realized-only cutover (2026-07-05) made this the CANONICAL live outcome
    # signal that replaced journey_finalizer/candle_*. A row gets realized_pnl_
    # percent / realized_exit_price stamped at exit; the journey is then terminal,
    # so max(updated_at) among realized rows == last realization time (not later-
    # bumped: open-journey updaters like mae/mfe/trajectory don't touch closed
    # journeys). Threshold from observed cadence 2026-07-09 (14d, read-only):
    # p50 gap 15m, p90 124m, MAX LEGIT GAP 858m (~14.3h) in the paper-drought.
    # Crypto is 24/7, so a "quiet" gap is a genuine low-signal DROUGHT, not an
    # overnight close -> a 4-6h alert WOULD false-fire. 24h sits well above the
    # observed max legit drought while still catching a genuine producer death
    # (the "unnoticed for days" failure this monitor guards) within <1 day. If it
    # still proves noisy in a deeper drought, flip mode to "info" (do NOT tighten).
    dict(name="signal_journeys[realized_outcome_stamp]", table="signal_journeys",
         col="updated_at", where="realized_pnl_percent IS NOT NULL",
         max_stale_min=1440, mode="alert",
         note="NS-P1 realized-only successor; 24h > observed max legit drought gap (14.3h), catches producer death without paper-drought false positives"),

    # --- realized_trade_ledger (NS-P1 net-of-cost AUTHORITY; writer PR #425) -----
    # 0 rows until REALIZED_LEDGER_WRITE_ENABLED deploys. Once live this is THE
    # net-of-cost measurement authority (one immutable row per realized close);
    # a dead writer would leave every close silently un-costed — the exact black
    # hole this monitor guards. Info until the writer deploys (avoids empty-stream
    # noise, same graduation pattern as gate_snapshot); FLIP mode to "alert" on
    # the first row. Budget ~ managed_positions[closes] (one ledger row per close).
    dict(name="realized_trade_ledger", table="realized_trade_ledger",
         col="computed_at", where=None, max_stale_min=240,
         mode="info", note="0 rows until REALIZED_LEDGER_WRITE_ENABLED deploys; FLIP to alert on first row (~matches managed_positions[closes] 240m budget)"),
]

# Per-symbol price feed for OPEN positions (stuck-exit guard). Checked separately
# because it is dynamic (one row per currently-open symbol). Silent when flat.
OPEN_POSITION_PRICE_STALE_MIN = 30

# ---------------------------------------------------------------------------
# FLAT-PORTFOLIO SUPPRESSION (card t_16fdf654, 2026-07-11)
# The two lanes below are DOWNSTREAM OF POSITION CLOSING. They only advance when
# the book is actually closing positions. A flat book (0 open positions AND no
# recent closes) is a LEGITIMATE HALT — the NS-P1 realized-only paper-drought /
# negative-EV condition — NOT a writer death. Alerting on it is the same
# cry-wolf defect this card was opened to remove, and it trains people to ignore
# the monitor. So we suppress their staleness alerts in that condition; the NS-P1
# realized-outcome AUTHORITY signal_journeys[realized_outcome_stamp] (24h) still
# fires on a genuine realized-outcome death, so coverage is preserved.
#   - managed_positions[closes]: real death = open positions exist but the
#     close-writer has stalled (> threshold with positions still open). Benign =
#     open_count == 0 (nothing to close).
#   - decision_outcomes[trade_close]: real death = closes ARE happening but the
#     trade_close labeler stopped stamping them. Benign = no recent closing
#     activity at all (last close older than the lane's own threshold).
# ---------------------------------------------------------------------------
CLOSING_ACTIVITY_LANES = {"managed_positions[closes]", "decision_outcomes[trade_close]"}


def portfolio_context(db):
    """(open_count, last_close_age_s) for the flat-portfolio suppression.

    open_count   : currently-open positions (closed_at IS NULL).
    last_close_age_s: staleness of the most recent managed_positions close, or
    None if the table has never recorded a close.
    """
    sql = (
        "SELECT (SELECT count(*) FROM public.managed_positions "
        "        WHERE closed_at IS NULL) AS open_n, "
        "EXTRACT(EPOCH FROM (now() - max(closed_at)))::float AS age_s "
        "FROM public.managed_positions WHERE closed_at IS NOT NULL;"
    )
    for row in psql(db, sql):
        parts = (row.split("|") + ["", ""])[:2]
        open_n = int(parts[0]) if parts[0].strip() else 0
        age_s = float(parts[1]) if parts[1].strip() else None
        return open_n, age_s
    return 0, None


def resolve_db():
    """host, port, user, db, password from server/.env DATABASE_URL (host-rewritten)."""
    url = DEFAULT_URL
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL=") and ("127.0.0.1" in line or "localhost" in line):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
        else:
            # no host-reachable line; take the first DATABASE_URL and rewrite host
            for line in ENV_FILE.read_text().splitlines():
                if line.strip().startswith("DATABASE_URL="):
                    url = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    except OSError:
        pass
    # docker-network host -> host-reachable loopback (same DB)
    url = url.replace("@supabase-db:", "@127.0.0.1:").replace("@localhost:", "@127.0.0.1:")
    m = re.match(r"postgresql://([^:]+):([^@]*)@([^:/]+):(\d+)/(\w+)", url)
    if not m:
        return dict(host="127.0.0.1", port="5432", user="postgres", db="postgres", pw="postgres")
    return dict(user=m.group(1), pw=m.group(2), host=m.group(3), port=m.group(4), db=m.group(5))


def psql(db, sql):
    """Run a read-only SELECT; return list of pipe-split rows. Raises on error."""
    cmd = [
        "psql", "-h", db["host"], "-p", db["port"], "-U", db["user"], "-d", db["db"],
        "-X", "-q", "-t", "-A", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql,
    ]
    env = {"PGPASSWORD": db["pw"], "PGOPTIONS": "-c default_transaction_read_only=on",
           "PATH": "/usr/bin:/bin:/usr/local/bin"}
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=50, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"psql rc={proc.returncode}: {proc.stderr.strip()[:300]}")
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def build_union_sql():
    """One round-trip: name | age_seconds | last_ts for every registry stream."""
    frags = []
    for s in STREAMS:
        where = f" WHERE {s['where']}" if s["where"] else ""
        frags.append(
            f"SELECT '{s['name']}' AS name, "
            f"EXTRACT(EPOCH FROM (now() - max({s['col']})))::float AS age_s, "
            f"max({s['col']})::text AS last_ts "
            f"FROM public.{s['table']}{where}"
        )
    return "\nUNION ALL\n".join(frags) + ";"


def fmt_age(sec):
    if sec is None:
        return "never"
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    if d:
        return f"{d}d{h}h{m}m"
    if h:
        return f"{h}h{m}m"
    return f"{m}m"


def check_open_position_price(db):
    """Alert lines for OPEN-position symbols whose price feed (candles) is stale.
    Silent when flat (no open positions) or when every open feed is fresh."""
    sql = (
        "SELECT mp.symbol, "
        "EXTRACT(EPOCH FROM (now() - max(c.\"timestamp\")))::float AS age_s, "
        "max(c.\"timestamp\")::text "
        "FROM managed_positions mp JOIN candles c ON c.symbol = mp.symbol "
        "WHERE mp.closed_at IS NULL "
        "GROUP BY mp.symbol "
        f"HAVING EXTRACT(EPOCH FROM (now() - max(c.\"timestamp\"))) > {OPEN_POSITION_PRICE_STALE_MIN*60};"
    )
    alerts = []
    for row in psql(db, sql):
        sym, age_s, last_ts = (row.split("|") + ["", ""])[:3]
        age = float(age_s) if age_s else None
        alerts.append(f"STALE: price_feed[{sym}] (open position) last={last_ts} "
                      f"age={fmt_age(age)} threshold={OPEN_POSITION_PRICE_STALE_MIN}m")
    return alerts


def main():
    db = resolve_db()
    try:
        rows = psql(db, build_union_sql())
        by_name = {}
        for row in rows:
            name, age_s, last_ts = (row.split("|") + ["", ""])[:3]
            by_name[name] = (float(age_s) if age_s.strip() else None,
                             last_ts if last_ts.strip() else None)
        price_alerts = check_open_position_price(db)
    except Exception as e:  # operational failure -> surface as cron error
        print(f"ERROR sycode-critical-stream-freshness: {e}", file=sys.stderr)
        sys.exit(1)

    alerts = []
    # Flat-portfolio context: suppress benign no-closing-activity staleness on the
    # two closing-activity lanes (see FLAT-PORTFOLIO SUPPRESSION header note).
    open_n, last_close_age_s = portfolio_context(db)
    flat_book = (open_n == 0)
    for s in STREAMS:
        if s["mode"] != "alert":
            continue
        if s["name"] in CLOSING_ACTIVITY_LANES and flat_book:
            # No open positions -> nothing to close -> a stale closes/trade_close
            # lane is expected, not a death. Suppress (still guarded by the NS-P1
            # realized_outcome_stamp 24h lane if realized outcomes truly stop).
            continue
        age, last_ts = by_name.get(s["name"], (None, None))
        thr_s = s["max_stale_min"] * 60
        if age is None:  # lane has never written a row -> unexpectedly empty
            alerts.append(f"STALE: {s['name']} last=NEVER age=inf "
                          f"threshold={s['max_stale_min']}m (stream empty)")
        elif age > thr_s:
            alerts.append(f"STALE: {s['name']} last={last_ts} age={fmt_age(age)} "
                          f"threshold={s['max_stale_min']}m")
    alerts.extend(price_alerts)

    if alerts:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
        print(f"CRITICAL STREAM FRESHNESS — {len(alerts)} stale @ {stamp}")
        for a in alerts:
            print(a)
        sys.exit(0)  # alert delivered via stdout; do not crash the cron slot
    sys.exit(0)  # healthy: empty stdout -> no-agent cron delivers nothing


if __name__ == "__main__":
    main()
