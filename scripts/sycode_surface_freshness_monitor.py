#!/usr/bin/env python3
# invoker: hermes cron job (to be registered by devops card, hourly) — manual: python3 ~/.hermes/scripts/sycode_surface_freshness_monitor.py
#
# NS-P2.3 (sycode-trading/t_a8c6bbf5): Surface freshness monitor for the
# data-surface certification register.
#
# Companion to (not a replacement for) two existing monitors:
#   - dgx_data_freshness_probe.py (jarvis cron "data-freshness-probe", */30m):
#     producer-liveness on 8 hot pipelines, falling-edge alerting to discord.
#   - sycode_feature_density_monitor.py (NS-P2.5): consuming-field FILL RATE in
#     signal_journeys (catches producer-fresh-but-consumer-NULL outages).
# THIS script enforces the REGISTER's SLOs: every certified surface in
# analytics/data-surface-register.md has a freshness SLO here, including the
# surfaces the probe never covered (orderbook, onchain, stablecoin hourly,
# trade_close_events, decision_outcomes, r_multiple_labels, pattern registry).
# Overlap with the probe on hot feeds is intentional defense-in-depth; this
# monitor's budgets are the certified SLOs (tighter on machine feeds).
#
# Behavior:
#   - SELECT max(ts) only against the sycodetrading-supabase-db container
#     (read-only enforced via PGOPTIONS default_transaction_read_only=on).
#     NEVER count(*) — seq-scanning 37M-row tables times out (dgx probe lesson).
#   - Writes a markdown report to
#     /home/frank/obsidian/sycode-trading/analytics/surface-freshness/YYYY-MM-DD.md
#     (the ONLY thing this script ever writes; same-day reruns overwrite).
#   - Exits 2 and prints ALERT line(s) when any surface with mode=alert exceeds
#     its SLO (or is unexpectedly EMPTY). Exits 1 on operational error. Exits 0
#     when every alertable surface is within SLO.
#   - mode=pending surfaces (born-empty tables awaiting first production batch,
#     e.g. r_multiple_labels) report PENDING while empty and graduate to the SLO
#     automatically once rows exist. mode=gap surfaces (capture never wired,
#     e.g. liquidation_events) are reported as GAP but never alert — the GAP is
#     the register's finding; alerting on it forever would be noise.
#   - --self-test: runs the evaluation logic on synthetic ages (no DB, no report
#     write), proving stale/empty alerts fire and pending/gap stay silent.
#
# Consumers: NS-P2.3 data-surface certification register (SLO source of truth);
# daily north-star sweep; the registering cron routes non-zero exit to discord
# #critical-alerts.

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from second_brain_writer import write_markdown_atomic

DB_CONTAINER = "sycodetrading-supabase-db"
REPORT_DIR = Path("/home/frank/obsidian/sycode-trading/analytics/surface-freshness")
REGISTER = "analytics/data-surface-register.md"

# ----------------------------------------------------------------------------
# CERTIFIED SURFACES — keep in lockstep with the register. One row per surface:
#   surface, table, ts column, SLO hours, mode, note
# mode: "alert"   = stale or empty ⇒ ALERT (exit 2)
#       "pending" = empty ⇒ PENDING (no alert); non-empty ⇒ behaves like alert
#       "gap"     = capture not wired; report only, never alert
# SLOs derived 2026-07-06 from observed cadence (see register for the numbers).
# ----------------------------------------------------------------------------
SURFACES = [
    ("candles",                "candles",                  "\"timestamp\"", 1.0,  "alert",
     "1m feed lands every minute (~800 rows/h); 1h SLO catches a dead collector within the cron hour"),
    ("funding_rate_history",   "funding_rate_history",     "\"timestamp\"", 1.0,  "alert",
     "continuous (~50k rows/h); the surface whose silent outage class motivated this card"),
    ("oi_snapshots",           "oi_snapshots",             "\"timestamp\"", 1.0,  "alert",
     "continuous (~4.5k rows/h)"),
    ("orderbook_snapshots",    "orderbook_snapshots",      "captured_at",   1.0,  "alert",
     "continuous (~500 rows/h); NOT covered by dgx probe"),
    ("signal_journeys",        "signal_journeys",          "created_at",    3.0,  "alert",
     "signal-gated (~150-550/h observed); 3h budget tolerates quiet market stretches"),
    ("signal_journey_events",  "signal_journey_events",    "occurred_at",   3.0,  "alert",
     "event stream for journeys; was frozen 15d once (2026-07-02 lesson)"),
    ("pro_trader_fills",       "pro_trader_fills",         "ingested_at",   6.0,  "alert",
     "wallet-shadow cohort fill feed (continuous WS+REST; PR #814 harness); 6h SLO matches WalletShadowFillsFeedStale"),
    ("wallet_shadow_journeys", "signal_journeys",          "created_at",    3.0,  "alert",
     "WALLET_SHADOW journey emission (correlation_id LIKE 'wallet-shadow-%'); signal-gated, matches base signal_journeys 3h budget"),
    ("onchain_snapshots",      "onchain_snapshots",        "created_at",    27.0, "alert",
     "daily batch ~00:00-01:10Z; 27h budget = daily cadence + margin"),
    ("stablecoin_flow_hourly", "stablecoin_flow_hourly_v1", "hour_utc",     4.0,  "alert",
     "hourly builder; 4h budget tolerates builder lag"),
    ("trade_close_events",     "trade_close_events",       "created_at",    12.0, "alert",
     "event-driven; 0 closes can be legitimate under strategy quarantine/paper halt "
     "(dgx probe paired-check lesson) — treat ALERT as 'confirm halt is intentional'"),
    ("decision_outcomes",      "decision_outcomes",        "created_at",    8.0,  "alert",
     "journey_finalizer + trade_close labelers write continuously when up; "
     "8h budget catches a dead outcome factory within the shift"),
    ("r_multiple_labels",      "r_multiple_labels",        "computed_at",   26.0, "pending",
     "NS-P3.2 labeler merged (PR #362) but 0 clean-epoch closes yet ⇒ born-empty; "
     "graduates to 26h SLO on first batch"),
    ("pattern_win_rate_registry", "pattern_win_rate_registry", "last_updated", 26.0, "alert",
     "Class C read-model refreshed by registry updater; research reads it as advisory only"),
    ("liquidation_events",     "liquidation_events",       "\"timestamp\"", 5.0,  "alert",
     "collector PR #394 (Binance USDM forceOrder WS) producing since 2026-07-29; "
     "consumer PR #928 (FeatureStoreV2). SLO 5h catches a dead collector within a "
     "work-shift while tolerating sparse cascades (register GAP #1 resolved 2026-08-05)."),
    ("hl_leaderboard_snapshots", "hl_leaderboard_snapshots", "created_at", 36.0, "alert",
     "daily archive (24h cadence); SLO 36h from created_at (write time), NOT snapshot_date "
     "date-floor — F2/PR #894; 36h is looser than the 30h WalletShadowLeaderboardSnapshotStale alert"),
]
# data_epoch_registry is certified in the register but has NO freshness SLO:
# it is event-driven (rows appear when defects are registered). Completeness is
# governed by policy §3 rule 3, not by a clock. funding_rate_snapshots is empty
# and superseded by funding_rate_history (register note; not monitored).

# Optional per-surface row filters, applied inside fetch_age_hours. Used for
# surfaces that are a slice of a shared table (e.g. wallet-shadow journey
# emission = signal_journeys WHERE correlation_id LIKE 'wallet-shadow-%').
# Keys must match SURFACES surface names; the value is appended as a raw SQL
# WHERE clause (constant literals only — never interpolate runtime input).
SURFACE_FILTERS = {
    "wallet_shadow_journeys": "correlation_id LIKE 'wallet-shadow-%'",
}

EMPTY_SENTINEL = -1.0

# ----------------------------------------------------------------------------
# FLAT-BOOK SUPPRESSION (card t_16fdf654, 2026-07-11)
# decision_outcomes + trade_close_events are DOWNSTREAM OF POSITION CLOSING. They
# only advance when the book is actually closing positions. A flat book (0 open
# positions AND no recent closes) is the legitimate NS-P1 paper-drought / paper-
# halt condition, NOT a writer death. Alerting on it is the same cry-wolf defect
# addressed in sycode_critical_stream_freshness.py, and it trains people to ignore
# #critical-alerts. So under a flat book we report these surfaces as FLAT (no
# alert). Coverage is preserved: machine-live surfaces (candles, funding, oi,
# orderbook, signal_journeys, ...) keep firing on genuine death, and a REAL close-
# writer stuck-exit (open positions that won't close) still shows via the critical
# stream monitor. trade_close_events already documents this in its note; this makes
# it automatic instead of a manual "confirm halt is intentional" ritual.
# ----------------------------------------------------------------------------
FLAT_BOOK_SURFACES = {"decision_outcomes", "trade_close_events"}


# ----------------------------------------------------------------------------
# Data collection (SELECT max(ts) only)
# ----------------------------------------------------------------------------
def fetch_open_position_count():
    """Return count of currently-open positions (closed_at IS NULL) for the
    flat-book suppression. Uses the same container + read-only pattern as
    fetch_age_hours. Returns 0 on any error (fail-open: suppress under doubt)."""
    sql = "SELECT count(*) FROM public.managed_positions WHERE closed_at IS NULL;"
    cmd = [
        "docker", "exec",
        "-e", "PGOPTIONS=-c default_transaction_read_only=on",
        DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "postgres",
        "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", sql,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return 0
        return int(proc.stdout.strip() or "0")
    except Exception:
        return 0


def is_flat_book():
    """True when there are no open positions: closing-activity surfaces
    (decision_outcomes, trade_close_events) are legitimately halted."""
    return fetch_open_position_count() == 0


def fetch_age_hours(table, col, where=None):
    """Return age of max(ts) in hours, EMPTY_SENTINEL if table is empty.
    where: optional SQL WHERE clause for surfaces that are a slice of a
    shared table (see SURFACE_FILTERS). Constants only — never interpolate
    runtime input into this clause."""
    filter_sql = f" WHERE {where}" if where else ""
    sql = (
        f"SELECT COALESCE(EXTRACT(EPOCH FROM (now() - max({col})))/3600.0, {EMPTY_SENTINEL}) "
        f"FROM public.{table}{filter_sql};"
    )
    cmd = [
        "docker", "exec",
        "-e", "PGOPTIONS=-c default_transaction_read_only=on",
        DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "postgres",
        "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", sql,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed on {table} rc={proc.returncode}: {proc.stderr.strip()[:300]}")
    return float(proc.stdout.strip())


# ----------------------------------------------------------------------------
# Evaluation (pure function — reused by --self-test)
# ----------------------------------------------------------------------------
def evaluate(surfaces, ages, flat_book=False):
    """surfaces: config rows; ages: {surface: age_hours or EMPTY_SENTINEL}.
    flat_book: if True, closing-activity surfaces (FLAT_BOOK_SURFACES) are
    reported FLAT (no alert) under the legitimate paper-drought / paper-halt
    condition (see FLAT-BOOK SUPPRESSION header).
    Returns (alerts, rows) where rows = [(surface, status, age_h, slo_h, mode)]."""
    alerts, rows = [], []
    for surface, _table, _col, slo_h, mode, _note in surfaces:
        age = ages[surface]
        empty = age == EMPTY_SENTINEL
        if mode == "gap":
            status = "GAP"
        elif empty and mode == "pending":
            status = "PENDING"
        elif empty:
            status = "EMPTY"
            alerts.append(f"ALERT surface-freshness: {surface} is unexpectedly EMPTY "
                          f"(certified surface with SLO {slo_h:g}h)")
        elif surface in FLAT_BOOK_SURFACES and flat_book:
            status = "FLAT"
        elif age > slo_h:
            status = "STALE"
            alerts.append(f"ALERT surface-freshness: {surface} stale {age:.1f}h "
                          f"(SLO {slo_h:g}h)")
        else:
            status = "FRESH"
        rows.append((surface, status, None if empty else age, slo_h, mode))
    return alerts, rows


# ----------------------------------------------------------------------------
# Report rendering
# ----------------------------------------------------------------------------
def render_report(rows, alerts, now_utc):
    notes = {s: n for s, _t, _c, _slo, _m, n in SURFACES}
    lines = []
    lines.append(f"# Surface freshness — certified register SLOs — {now_utc:%Y-%m-%d}")
    lines.append("")
    lines.append(f"Generated {now_utc:%Y-%m-%d %H:%M}Z by `sycode_surface_freshness_monitor.py` "
                 f"(NS-P2.3, card t_a8c6bbf5). SLO source of truth: "
                 f"[[analytics/data-surface-register|data-surface-register]].")
    lines.append("")
    if alerts:
        lines.append(f"## ALERTS ({len(alerts)})")
        lines.append("")
        for a in alerts:
            lines.append(f"- `{a}`")
    else:
        lines.append("## Status: HEALTHY — every alertable surface within its SLO")
    lines.append("")
    lines.append("| surface | status | age | SLO | mode | note |")
    lines.append("|---|---|---|---|---|---|")
    for surface, status, age, slo_h, mode in rows:
        age_s = "—" if age is None else f"{age:.1f}h"
        slo_s = "—" if mode == "gap" else f"{slo_h:g}h"
        flag = {"FRESH": "ok", "PENDING": "pending", "GAP": "**GAP**",
                "STALE": "**STALE**", "EMPTY": "**EMPTY**", "FLAT": "flat"}[status]
        lines.append(f"| {surface} | {flag} | {age_s} | {slo_s} | {mode} | {notes[surface]} |")
    lines.append("")
    lines.append("Companions: `dgx_data_freshness_probe.py` (producer liveness, */30m), "
                 "`sycode_feature_density_monitor.py` (consuming-field fill, daily). "
                 "Consumers: data-surface-register (NS-P2.3), daily north-star sweep, "
                 "discord #critical-alerts on non-zero exit (via registered cron).")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Self-test — synthetic ages proving the evaluation logic
# ----------------------------------------------------------------------------
def self_test():
    surfaces = [
        ("fresh_feed",   "t", "c", 1.0,  "alert",   "n"),
        ("stale_feed",   "t", "c", 1.0,  "alert",   "n"),
        ("dead_feed",    "t", "c", 2.0,  "alert",   "n"),
        ("born_empty",   "t", "c", 26.0, "pending", "n"),
        ("grown_up",     "t", "c", 1.0,  "pending", "n"),
        ("never_wired",  "t", "c", 0.0,  "gap",     "n"),
        ("trade_close_events", "t", "c", 8.0, "alert", "n"),
    ]
    ages = {
        "fresh_feed": 0.4,             # within SLO -> no alert
        "stale_feed": 5.2,             # 5.2h > 1h  -> STALE alert
        "dead_feed": EMPTY_SENTINEL,   # empty + alert mode -> EMPTY alert
        "born_empty": EMPTY_SENTINEL,  # empty + pending -> PENDING, silent
        "grown_up": 3.0,               # pending table WITH rows, 3h > 1h -> STALE alert
        "never_wired": EMPTY_SENTINEL, # gap -> silent forever
        "trade_close_events": 28.9,    # stale, but in FLAT_BOOK_SURFACES under flat book -> FLAT, silent
    }
    alerts, rows = evaluate(surfaces, ages, flat_book=True)
    status = {r[0]: r[1] for r in rows}
    results = [
        ("fresh surface silent", status["fresh_feed"] == "FRESH"),
        ("stale surface fires ALERT", status["stale_feed"] == "STALE"
         and any("stale_feed stale 5.2h" in a for a in alerts)),
        ("unexpectedly empty surface fires ALERT", status["dead_feed"] == "EMPTY"
         and any("dead_feed is unexpectedly EMPTY" in a for a in alerts)),
        ("born-empty pending surface silent", status["born_empty"] == "PENDING"),
        ("pending surface with rows graduates to SLO", status["grown_up"] == "STALE"),
        ("gap surface never alerts", status["never_wired"] == "GAP"),
        ("flat-book closing surface reported FLAT (no alert)", status["trade_close_events"] == "FLAT"),
        ("exactly three alerts", len(alerts) == 3),
    ]

    # Config sanity for hl_leaderboard_snapshots (card t_35bd27be, t_84eee812 step 3):
    # the surface must be REGISTERED with SLO 36h + mode alert, and the ts column
    # must be created_at (write time), NOT the snapshot_date date-floor (F2/PR #894).
    surfaces_by_name = {s: (_t, c, sl, m) for s, _t, c, sl, m, _n in SURFACES}
    hl = surfaces_by_name.get("hl_leaderboard_snapshots")
    results.append(("hl_leaderboard_snapshots registered with 36h SLO alert",
                    hl is not None and hl[2] == 36.0 and hl[3] == "alert"))
    results.append(("hl_leaderboard_snapshots ts column is created_at (not snapshot_date)",
                    hl is not None and hl[1] == "created_at"))

    # Config sanity for the wallet-shadow surfaces (re-applied from card
    # t_3f3e7a65): the two LIVE surfaces must be registered with their SLOs,
    # and the filtered journey slice must have its WHERE filter wired.
    results.append(("pro_trader_fills registered with 6h SLO alert",
                    surfaces_by_name.get("pro_trader_fills") == ("pro_trader_fills", "ingested_at", 6.0, "alert")))
    results.append(("wallet_shadow_journeys registered with 3h SLO alert",
                    surfaces_by_name.get("wallet_shadow_journeys") == ("signal_journeys", "created_at", 3.0, "alert")))
    results.append(("wallet_shadow_journeys has a WHERE filter",
                    "wallet_shadow_journeys" in SURFACE_FILTERS
                    and "correlation_id LIKE 'wallet-shadow-%'" in SURFACE_FILTERS["wallet_shadow_journeys"]))
    all_ok = all(ok for _, ok in results)
    for name, ok in results:
        print(f"SELF-TEST {'PASS' if ok else 'FAIL'}: {name}")
    print(f"SELF-TEST {'PASS' if all_ok else 'FAIL'}: overall")
    return 0 if all_ok else 1


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="NS-P2.3 surface freshness monitor (register SLOs)")
    ap.add_argument("--self-test", action="store_true",
                    help="run evaluation self-test on synthetic ages (no DB, no report write)")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    now_utc = datetime.now(timezone.utc)
    flat_book = is_flat_book()  # suppress benign flat-book staleness on closing surfaces
    ages = {}
    try:
        for surface, table, col, _slo, _mode, _note in SURFACES:
            ages[surface] = fetch_age_hours(table, col, SURFACE_FILTERS.get(surface))
    except Exception as e:
        print(f"ERROR surface-freshness monitor: {e}", file=sys.stderr)
        sys.exit(1)

    alerts, rows = evaluate(SURFACES, ages, flat_book=flat_book)
    report = render_report(rows, alerts, now_utc)

    report_date = f"{now_utc:%Y-%m-%d}"
    report_path = REPORT_DIR / f"{report_date}.md"
    write_markdown_atomic(
        report_path,
        report,
        title=f"Surface freshness — certified register SLOs — {report_date}",
        type="task-evidence",
        status="active",
        created=report_date,
        updated=report_date,
        confidence="high",
        tags=["sycode", "monitoring", "surface-freshness"],
        sources=[REGISTER],
        project="sycode-trading",
        owners=["trading-devops"],
        knowledge_tier="evidence",
        generated=True,
        generator="sycode_surface_freshness_monitor.py",
    )
    # Operational/clean output → STDERR so a --no-agent watchdog cron stays SILENT
    # when healthy (empty stdout = no delivery). ALERT lines go to STDOUT so they
    # ARE delivered on exit 2. (Mirrors sycode_critical_stream_freshness silent-on-clean.)
    print(f"report written: {report_path}", file=sys.stderr)

    if alerts:
        for a in alerts:
            print(a)  # stdout — delivered by the no-agent cron
        sys.exit(2)
    print(f"OK: all {sum(1 for _s, st, _a, _sl, _m in rows if st == 'FRESH')} alertable "
          f"surfaces within SLO ({sum(1 for _s, st, _a, _sl, _m in rows if st == 'PENDING')} pending, "
          f"{sum(1 for _s, st, _a, _sl, _m in rows if st == 'GAP')} gap)", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
