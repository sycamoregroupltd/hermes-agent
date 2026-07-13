#!/usr/bin/env python3
# invoker: hermes cron job (to be registered by devops card) — manual: python3 ~/.hermes/scripts/sycode_feature_density_monitor.py
#
# NS-P2.5 (sycode-trading/t_e0e72dff): Feature-density monitor over signal_journeys.
#
# WHY CONSUMING-FIELD DENSITY, NOT PRODUCER FRESHNESS: funding_rate_trend sat at
# ~100% NULL for weeks (a swallowed TypeError nulled every enrichment) while the
# producer table (funding_rate_history) stayed perfectly fresh. Freshness monitors
# on producers are blind to this class. This script measures the FILL RATE of the
# consuming fields in signal_journeys itself, per UTC day, and alerts on
# day-over-day density drops — catching the next 82%-NULL field in days, not weeks.
#
# Behavior:
#   - SELECT-only against the sycodetrading-supabase-db container (read-only
#     enforced via PGOPTIONS default_transaction_read_only=on).
#   - Writes a compact markdown report to
#     /home/frank/obsidian/sycode-trading/analytics/feature-density/YYYY-MM-DD.md
#     (the ONLY thing this script ever writes).
#   - Exits 2 and prints ALERT line(s) when any tracked feature drops
#     >ALERT_DROP_PP percentage points vs the prior evaluated day, or falls below
#     ALERT_ABS_FLOOR_PCT absolute. Exits 1 on operational error. Exits 0 when healthy.
#   - --self-test: runs the alert logic on synthetic data (no DB, no report write),
#     proving the >10pp-drop and <50%-floor alerts fire. Exits 0 on pass, 1 on fail.
#
# Consumers: NS-P2.3 data-surface certification register + weekly north-star sweep
# read the reports; the registering cron routes non-zero exits to discord #critical-alerts.

import argparse
import csv
import io
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from second_brain_writer import write_markdown_atomic

# ----------------------------------------------------------------------------
# CONFIG — extend TRACKED_FEATURES freely; each entry is a signal_journeys column.
# ----------------------------------------------------------------------------
TRACKED_FEATURES = [
    "funding_rate_trend",
    "market_oi_delta_percent",
    "pattern_strength",
    "directional_conviction",
    "market_funding_rate",
    "market_funding_rate_annualized",
    "market_open_interest",
    "regime_volatility",
    "regime_trend",
    "regime_score",
    "regime_direction",
    "regime_key",
    "regime_favorable",
    # NS-P2 surface liveness (added 2026-07-09): the reframe's #1 gap (structure/
    # levels) + the liquidation enrichment surface were live but density-UNMONITORED
    # — a silent writer death would drop these to NULL unnoticed. count(jsonb) tracks
    # writer liveness; both sit ~100%/~89% today, well above the 50% floor.
    "structure_levels",
    "liquidation_context",
]

WINDOW_DAYS = 7            # report window: last 7 UTC calendar days incl. today
ALERT_DROP_PP = 10.0       # alert when a feature drops more than this many pp day-over-day
ALERT_ABS_FLOOR_PCT = 50.0 # alert when a feature's fill rate is below this absolute %
MIN_ROWS_FOR_ALERT = 25    # days with fewer rows are reported but not alert-evaluated
                           # (prevents 4-row just-after-midnight false alarms)

# Epoch notes rendered into the report when the date falls inside the window.
# (date_str "YYYY-MM-DD", note)
EPOCH_NOTES = [
    ("2026-07-05", "funding-enrichment TypeError fixed ~22:08Z — funding_rate_trend / "
                   "market_funding_rate near-0% for the 17:00-22:00Z stretch, recovering after"),
]

DB_CONTAINER = "sycodetrading-supabase-db"
REPORT_DIR = Path("/home/frank/obsidian/sycode-trading/analytics/feature-density")

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


# ----------------------------------------------------------------------------
# Data collection (SELECT-only)
# ----------------------------------------------------------------------------
def fetch_density():
    """Return (days, rows_by_day, pct) where pct[day][feature] = fill % (float).

    days is a sorted list of 'YYYY-MM-DD' strings present in the window.
    """
    for f in TRACKED_FEATURES:
        if not _IDENT_RE.match(f):
            raise ValueError(f"illegal feature identifier in config: {f!r}")

    pct_exprs = ",\n       ".join(
        f"round(100.0*count({f})/count(*), 1) AS {f}" for f in TRACKED_FEATURES
    )
    sql = (
        "SELECT (triggered_at AT TIME ZONE 'UTC')::date AS day,\n"
        "       count(*) AS n_rows,\n"
        f"       {pct_exprs}\n"
        "FROM public.signal_journeys\n"
        f"WHERE triggered_at >= ((now() AT TIME ZONE 'UTC')::date - INTERVAL '{WINDOW_DAYS - 1} days')\n"
        "GROUP BY 1 ORDER BY 1;"
    )
    cmd = [
        "docker", "exec",
        "-e", "PGOPTIONS=-c default_transaction_read_only=on",
        DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "postgres",
        "-X", "-q", "-v", "ON_ERROR_STOP=1", "--csv", "-c", sql,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed rc={proc.returncode}: {proc.stderr.strip()[:500]}")

    days, rows_by_day, pct = [], {}, {}
    reader = csv.DictReader(io.StringIO(proc.stdout))
    for row in reader:
        day = row["day"]
        days.append(day)
        rows_by_day[day] = int(row["n_rows"])
        pct[day] = {f: float(row[f]) for f in TRACKED_FEATURES}
    return sorted(days), rows_by_day, pct


# ----------------------------------------------------------------------------
# Alert evaluation (pure function — reused by --self-test)
# ----------------------------------------------------------------------------
def evaluate_alerts(days, rows_by_day, pct):
    """Evaluate the latest alert-eligible day (n >= MIN_ROWS_FOR_ALERT) against the
    previous alert-eligible day. Returns (alerts, eval_day, prev_day) where alerts
    is a list of human-readable ALERT strings (empty = healthy)."""
    eligible = [d for d in days if rows_by_day.get(d, 0) >= MIN_ROWS_FOR_ALERT]
    if not eligible:
        return [], None, None
    eval_day = eligible[-1]
    prev_day = eligible[-2] if len(eligible) >= 2 else None

    alerts = []
    for f in TRACKED_FEATURES:
        cur = pct[eval_day].get(f)
        if cur is None:
            continue
        if prev_day is not None:
            prev = pct[prev_day].get(f)
            if prev is not None and (prev - cur) > ALERT_DROP_PP:
                alerts.append(
                    f"ALERT feature-density: {f} dropped {prev - cur:.1f}pp "
                    f"({prev:.1f}% on {prev_day} -> {cur:.1f}% on {eval_day}, "
                    f"n={rows_by_day[eval_day]})"
                )
        if cur < ALERT_ABS_FLOOR_PCT:
            alerts.append(
                f"ALERT feature-density: {f} below absolute floor "
                f"({cur:.1f}% < {ALERT_ABS_FLOOR_PCT:.0f}% on {eval_day}, "
                f"n={rows_by_day[eval_day]})"
            )
    return alerts, eval_day, prev_day


# ----------------------------------------------------------------------------
# Report rendering
# ----------------------------------------------------------------------------
def render_report(days, rows_by_day, pct, alerts, eval_day, prev_day, now_utc):
    lines = []
    lines.append(f"# Feature density — signal_journeys — {now_utc:%Y-%m-%d}")
    lines.append("")
    lines.append(f"Generated {now_utc:%Y-%m-%d %H:%M}Z by `sycode_feature_density_monitor.py` "
                 f"(NS-P2.5, card t_e0e72dff). Window: last {WINDOW_DAYS} UTC days. "
                 f"Values = % non-null per tracked feature per day.")
    lines.append(f"Alert rules: >{ALERT_DROP_PP:.0f}pp day-over-day drop OR "
                 f"<{ALERT_ABS_FLOOR_PCT:.0f}% absolute, evaluated on the latest day with "
                 f"n>={MIN_ROWS_FOR_ALERT} (here: {eval_day or 'n/a'} vs {prev_day or 'n/a'}).")
    lines.append("")
    if alerts:
        lines.append(f"## ALERTS ({len(alerts)})")
        lines.append("")
        for a in alerts:
            lines.append(f"- `{a}`")
    else:
        lines.append("## Status: HEALTHY — no tracked feature breached alert rules")
    lines.append("")

    day_hdrs = [d[5:] for d in days]  # MM-DD
    lines.append("| feature | " + " | ".join(day_hdrs) + " |")
    lines.append("|---|" + "---|" * len(days))
    lines.append("| _(rows)_ | " + " | ".join(str(rows_by_day[d]) for d in days) + " |")
    for f in TRACKED_FEATURES:
        cells = [f"{pct[d][f]:.1f}" for d in days]
        lines.append(f"| {f} | " + " | ".join(cells) + " |")
    lines.append("")

    window_notes = [(d, n) for d, n in EPOCH_NOTES if d in days]
    if window_notes:
        lines.append("## Epoch notes in window")
        lines.append("")
        for d, n in window_notes:
            lines.append(f"- **{d}** — {n}")
        lines.append("")
    lines.append("Consumers: NS-P2.3 data-surface certification register; weekly north-star sweep; "
                 "discord #critical-alerts on non-zero exit (via registered cron).")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Self-test — synthetic data proving the alert logic fires
# ----------------------------------------------------------------------------
def self_test():
    feats = TRACKED_FEATURES
    days = ["2026-01-01", "2026-01-02"]
    rows = {d: 1000 for d in days}
    base_prev = {f: 99.0 for f in feats}
    base_cur = {f: 98.0 for f in feats}  # -1pp everywhere: no alert expected

    # Case 1: >10pp day-over-day drop must alert
    p = {days[0]: dict(base_prev), days[1]: dict(base_cur)}
    p[days[1]]["funding_rate_trend"] = 84.0  # 99 -> 84 = -15pp
    alerts, _, _ = evaluate_alerts(days, rows, p)
    ok1 = any("funding_rate_trend" in a and "dropped 15.0pp" in a for a in alerts) and len(alerts) == 1

    # Case 2: <50% absolute floor must alert (even with no big drop vs prior)
    p = {days[0]: dict(base_prev), days[1]: dict(base_cur)}
    p[days[0]]["market_oi_delta_percent"] = 49.0
    p[days[1]]["market_oi_delta_percent"] = 45.0  # only -4pp, but <50 absolute
    alerts, _, _ = evaluate_alerts(days, rows, p)
    ok2 = any("market_oi_delta_percent" in a and "below absolute floor" in a for a in alerts) and len(alerts) == 1

    # Case 3: healthy data must NOT alert
    p = {days[0]: dict(base_prev), days[1]: dict(base_cur)}
    alerts, _, _ = evaluate_alerts(days, rows, p)
    ok3 = alerts == []

    # Case 4: low-sample day (n < MIN_ROWS_FOR_ALERT) is skipped, falls back to prior eligible day
    days3 = ["2026-01-01", "2026-01-02", "2026-01-03"]
    rows3 = {"2026-01-01": 1000, "2026-01-02": 1000, "2026-01-03": 4}
    p3 = {days3[0]: dict(base_prev), days3[1]: dict(base_cur), days3[2]: {f: 0.0 for f in feats}}
    alerts, eval_day, _ = evaluate_alerts(days3, rows3, p3)
    ok4 = eval_day == "2026-01-02" and alerts == []

    results = [
        ("drop >10pp fires exactly one alert", ok1),
        ("<50% absolute floor fires exactly one alert", ok2),
        ("healthy data fires no alert", ok3),
        ("low-sample day excluded from evaluation", ok4),
    ]
    all_ok = all(ok for _, ok in results)
    for name, ok in results:
        print(f"SELF-TEST {'PASS' if ok else 'FAIL'}: {name}")
    print(f"SELF-TEST {'PASS' if all_ok else 'FAIL'}: overall")
    return 0 if all_ok else 1


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="NS-P2.5 feature-density monitor over signal_journeys")
    ap.add_argument("--self-test", action="store_true",
                    help="run alert-logic self-test on synthetic data (no DB, no report write)")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    now_utc = datetime.now(timezone.utc)
    try:
        days, rows_by_day, pct = fetch_density()
    except Exception as e:
        print(f"ERROR feature-density monitor: {e}", file=sys.stderr)
        sys.exit(1)
    if not days:
        print("ERROR feature-density monitor: query returned no rows for the window", file=sys.stderr)
        sys.exit(1)

    alerts, eval_day, prev_day = evaluate_alerts(days, rows_by_day, pct)
    report = render_report(days, rows_by_day, pct, alerts, eval_day, prev_day, now_utc)

    report_date = f"{now_utc:%Y-%m-%d}"
    report_path = REPORT_DIR / f"{report_date}.md"
    write_markdown_atomic(
        report_path,
        report,
        title=f"Feature density — signal_journeys — {report_date}",
        type="task-evidence",
        status="active",
        created=report_date,
        updated=report_date,
        confidence="high",
        tags=["sycode", "monitoring", "feature-density"],
        sources=["analytics/data-surface-register.md"],
        project="sycode-trading",
        owners=["trading-devops"],
        knowledge_tier="evidence",
        generated=True,
        generator="sycode_feature_density_monitor.py",
    )
    # Operational/clean output → STDERR so a --no-agent watchdog cron stays SILENT
    # when healthy (empty stdout = no delivery); ALERT lines go to STDOUT so they
    # ARE delivered on exit 2. (Mirrors the surface/critical-stream monitors.)
    print(f"report written: {report_path}", file=sys.stderr)

    if alerts:
        for a in alerts:
            print(a)  # stdout — delivered by the no-agent cron
        sys.exit(2)
    print(f"OK: all {len(TRACKED_FEATURES)} tracked features within thresholds "
          f"on {eval_day} (vs {prev_day})", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
