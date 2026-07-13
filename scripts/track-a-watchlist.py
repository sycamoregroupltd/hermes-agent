#!/usr/bin/env python3
"""
SHORT 1h LOW vol + NEUTRAL macro — Watchlist Monitoring Cron (every Sunday)
Canonical source: ~/.hermes/profiles/trading-devops/scripts/short-1h-low-vol-neutral-watchlist.py

Tracks OOS accumulation for Track A pattern (rejected due to 92% temporal
concentration in Jan 2026). Queries signal_journeys for:
  - direction='SHORT', timeframe='1h'
  - macro_regime='NEUTRAL', regime_volatility='LOW'
  - clean_outcome_binary_24h for synthetic 24h WR

Triggers:
  - PROMOTION: WR > 53% AND n > 500 clean OOS signals (post-May 2026)
  - DEATH: WR < 40% OR n < 50/week for 4 consecutive weeks

Output:
  - Report persisted to ~/obsidian/quant-team/tests/watchlist/ (always)
  - Stdout watchdog: silent unless promotion/death trigger fires
"""

import os, re, sys, subprocess, json
from datetime import datetime, timezone, date as dt_date

# ── Paths ──────────────────────────────────────────────────────────────
OUTPUT_DIR = os.path.expanduser("~/obsidian/quant-team/tests/watchlist")
RE_EVAL_BOARD = "sycode-trading"  # board for re-evaluation tickets
PARENT_TASK = "t_f5c49ed0"  # this task id — for child linking

# ── PSQL helpers (same pattern as promotion-pipeline-metrics.py) ────────
PSQL_CMD = [
    "docker", "exec", "-e", "PGPASSWORD=postgres",
    "sycodetrading-supabase-db", "psql",
    "-h", "localhost", "-U", "postgres", "-d", "postgres",
    "-v", "ON_ERROR_STOP=1", "-t", "-A", "-P", "pager=off", "-c",
]

def db_query(sql):
    """Run SQL via psql, return stdout or None on failure."""
    try:
        r = subprocess.run(PSQL_CMD + [sql], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None

def safe_int(v, default=0):
    if v is None or v == "":
        return default
    return int(re.sub(r"[^0-9\-]", "", v))

def safe_float(v, default=0.0):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default

def fmt_pnl(v):
    """Format PnL as % string."""
    if v is None or v == "":
        return "-"
    v = safe_float(v)
    return f"{v*100:+.3f}%"


# ── Period queries ─────────────────────────────────────────────────────

def period_stats(label, interval):
    """Return dict: count, wr, avg_pnl for a trailing interval."""
    sql = f"""
    SELECT
        COUNT(*)::int AS cnt,
        ROUND(AVG(CASE WHEN clean_outcome_binary_24h THEN 100.0 ELSE 0.0 END)::numeric, 1) AS wr_pct,
        ROUND(AVG(clean_pnl_net_24h)::numeric, 6) AS avg_pnl
    FROM signal_journeys
    WHERE direction = 'SHORT'
      AND timeframe = '1h'
      AND macro_regime = 'NEUTRAL'
      AND regime_volatility = 'LOW'
      AND clean_outcome_binary_24h IS NOT NULL
      AND triggered_at >= NOW() - INTERVAL '{interval}'
    """
    raw = db_query(sql)
    if not raw or raw == "":
        return {"label": label, "cnt": 0, "wr": 0.0, "avg_pnl": 0.0, "raw": raw}
    parts = raw.split("|")
    if len(parts) < 3:
        return {"label": label, "cnt": 0, "wr": 0.0, "avg_pnl": 0.0, "raw": raw}
    return {
        "label": label,
        "cnt": safe_int(parts[0]),
        "wr": safe_float(parts[1]),
        "avg_pnl": safe_float(parts[2]),
        "raw": raw,
    }

def total_clean_oos_since(cutoff="2026-05-01"):
    """Count of clean_outcome_binary_24h-labeled signals since cutoff."""
    sql = f"""
    SELECT COUNT(*)::int
    FROM signal_journeys
    WHERE direction = 'SHORT'
      AND timeframe = '1h'
      AND macro_regime = 'NEUTRAL'
      AND regime_volatility = 'LOW'
      AND clean_outcome_binary_24h IS NOT NULL
      AND triggered_at >= '{cutoff}'
    """
    raw = db_query(sql)
    return safe_int(raw)

def symbol_breakdown(interval):
    """Top 5 symbols by count with WR and avg PnL."""
    sql = f"""
    SELECT symbol, COUNT(*)::int AS cnt,
           ROUND(AVG(CASE WHEN clean_outcome_binary_24h THEN 100.0 ELSE 0.0 END)::numeric, 1) AS wr_pct,
           ROUND(AVG(clean_pnl_net_24h)::numeric, 6) AS avg_pnl
    FROM signal_journeys
    WHERE direction = 'SHORT'
      AND timeframe = '1h'
      AND macro_regime = 'NEUTRAL'
      AND regime_volatility = 'LOW'
      AND clean_outcome_binary_24h IS NOT NULL
      AND triggered_at >= NOW() - INTERVAL '{interval}'
    GROUP BY symbol
    ORDER BY cnt DESC
    LIMIT 5
    """
    raw = db_query(sql)
    if not raw or raw == "":
        return []
    rows = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        rows.append({
            "symbol": parts[0],
            "cnt": safe_int(parts[1]),
            "wr": safe_float(parts[2]),
            "avg_pnl": safe_float(parts[3]),
        })
    return rows

def weekly_signal_counts(weeks=4):
    """Weekly signal counts for trailing N weeks to check death trigger."""
    sql = f"""
    SELECT DATE_TRUNC('week', triggered_at)::date AS week,
           COUNT(*)::int AS cnt
    FROM signal_journeys
    WHERE direction = 'SHORT'
      AND timeframe = '1h'
      AND macro_regime = 'NEUTRAL'
      AND regime_volatility = 'LOW'
      AND triggered_at >= NOW() - INTERVAL '{weeks * 7 + 7} days'
    GROUP BY 1
    ORDER BY 1 DESC
    LIMIT {weeks}
    """
    raw = db_query(sql)
    if not raw or raw == "":
        return []
    rows = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 2:
            continue
        rows.append({"week": parts[0], "cnt": safe_int(parts[1])})
    return rows

def all_time_wr_oos():
    """All-time WR and count (for reference)."""
    sql = """
    SELECT
        COUNT(*)::int AS total,
        COUNT(*) FILTER (WHERE clean_outcome_binary_24h IS NOT NULL)::int AS labeled,
        ROUND(COUNT(*) FILTER (WHERE clean_outcome_binary_24h IS NOT NULL) * 100.0 / GREATEST(COUNT(*), 1), 1) AS labeled_pct
    FROM signal_journeys
    WHERE direction = 'SHORT'
      AND timeframe = '1h'
      AND macro_regime = 'NEUTRAL'
      AND regime_volatility = 'LOW'
    """
    raw = db_query(sql)
    if not raw or raw == "":
        return {"total": 0, "labeled": 0, "labeled_pct": 0.0}
    parts = raw.split("|")
    if len(parts) < 3:
        return {"total": 0, "labeled": 0, "labeled_pct": 0.0}
    return {
        "total": safe_int(parts[0]),
        "labeled": safe_int(parts[1]),
        "labeled_pct": safe_float(parts[2]),
    }


# ── Watchdog logic ─────────────────────────────────────────────────────

def check_promotion_trigger(oos_count, trailing_wr_90d):
    """Promotion: WR > 53% AND n > 500 clean OOS signals since May 2026."""
    return trailing_wr_90d > 53.0 and oos_count > 500

def check_death_trigger(weekly_counts, trailing_wr_90d):
    """Death: WR < 40% OR n < 50/week for 4 consecutive weeks."""
    if trailing_wr_90d < 40.0:
        return True, "WR < 40%"

    # Check last 4 complete weeks (not current partial week)
    complete_weeks = [w for w in weekly_counts if w["cnt"] < 50]
    if len(complete_weeks) >= 4:
        return True, f"n < 50/week for {len(complete_weeks)} consecutive weeks"

    return False, ""


# ── Kanban task creation (promotion trigger) ───────────────────────────

def create_re_evaluation_ticket(current_stats):
    """Create a kanban re-evaluation ticket for the pattern."""
    title = f"RE-EVALUATE: SHORT 1h LOW vol NEUTRAL macro — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    body = (
        f"## Automated Re-evaluation Trigger — Watchlist Track A\n\n"
        f"**Triggered at:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"### Current Stats\n"
        f"- OOS signals since May 2026: {current_stats.get('oos_count', '?')}\n"
        f"- 90-day WR: {current_stats.get('wr_90d', '?')}%\n"
        f"- 30-day WR: {current_stats.get('wr_30d', '?')}%\n"
        f"- 7-day WR: {current_stats.get('wr_7d', '?')}%\n\n"
        f"### Re-evaluation Criteria (must pass for promotion)\n"
        f"- [ ] OOS sample size ≥ 500 ✅\n"
        f"- [ ] WR > 53% ✅\n"
        f"- [ ] Quarterly stability: no single quarter > 50%\n"
        f"- [ ] Symbol filter: ETH, LTC, ARB, BNB excluded\n"
        f"- [ ] Sharpe > 0.5 (synthetic labels)\n"
        f"- [ ] Net-of-fee positive expectancy\n"
        f"- [ ] Top-5 symbols sustained WR > 55% outside Jan 2026\n"
        f"- [ ] Jan 18 anomaly understood and excluded\n"
        f"- [ ] Temporal fold test: > 50% of quarterly folds positive\n\n"
        f"### Watchdog Report\n"
        f"{current_stats.get('report_path', '')}\n"
    )

    try:
        r = subprocess.run(
            [
                "/home/frank/.local/bin/hermes", "kanban", "create",
                title,
                "--assignee", "sycode-trading-pm",
                "--body", body,
                "--parent", PARENT_TASK,
                "--priority", "1",
                "--workspace", "scratch",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return r.stdout.strip()
        else:
            return f"ERROR creating ticket: {r.stderr.strip()}"
    except Exception as e:
        return f"ERROR: {e}"


# ── Report building ────────────────────────────────────────────────────

def build_report(now, periods, oos_count, symbols, weekly, all_time):
    """Build the markdown report dict."""
    today_str = now.strftime("%Y-%m-%d")

    lines = []
    lines.append(f"# Watchlist: SHORT 1h LOW vol + NEUTRAL macro — {today_str}")
    lines.append("")
    lines.append(f"_Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}_")
    lines.append("")
    lines.append("**Pattern:** SHORT 1h `regime_volatility=LOW` `macro_regime=NEUTRAL`")
    lines.append("**Status:** Track A watchlist monitoring (rejected 2026-07-05: 92% temporal concentration)")
    lines.append("")
    lines.append("## Trailing Performance")
    lines.append("")
    lines.append("| Period | Signals | WR | Avg PnL |")
    lines.append("|--------|---------|-----|---------|")
    for p in periods:
        wr_str = f"{p['wr']}%" if p["cnt"] > 0 else "-"
        pnl_str = fmt_pnl(p["avg_pnl"])
        lines.append(f"| {p['label']} | {p['cnt']} | {wr_str} | {pnl_str} |")

    lines.append("")
    lines.append(f"**OOS clean signals since May 2026:** {oos_count}")
    lines.append("")

    # Symbol breakdown (trailing 90d)
    if symbols:
        lines.append("## Top Symbols (trailing 90d)")
        lines.append("")
        lines.append("| Symbol | Signals | WR | Avg PnL |")
        lines.append("|--------|---------|-----|---------|")
        for s in symbols:
            wr_str = f"{s['wr']}%" if s['cnt'] > 0 else "-"
            pnl_str = fmt_pnl(s["avg_pnl"])
            lines.append(f"| {s['symbol']} | {s['cnt']} | {wr_str} | {pnl_str} |")
        lines.append("")

    # Weekly signal counts
    if weekly:
        lines.append("## Weekly Signal Counts")
        lines.append("")
        lines.append("| Week | Signals |")
        lines.append("|------|---------|")
        for w in weekly:
            lines.append(f"| {w['week']} | {w['cnt']} |")
        lines.append("")

    # All-time reference
    lines.append("## All-Time Reference")
    lines.append("")
    lines.append(f"- Total raw signals: {all_time.get('total', 0)}")
    lines.append(f"- Labeled (clean_outcome_binary_24h): {all_time.get('labeled', 0)} ({all_time.get('labeled_pct', 0)}%)")
    lines.append("")

    # Re-evaluation criteria summary
    lines.append("## Re-evaluation Criteria Status")
    lines.append("")
    lines.append(f"- OOS sample size ≥ 500: {'✅' if oos_count >= 500 else '❌'} ({oos_count}/500)")
    wr_90d = periods[2]["wr"] if len(periods) > 2 else 0
    lines.append(f"- WR > 53%: {'✅' if wr_90d > 53.0 else '❌'} ({wr_90d}%)")
    lines.append(f"- 4-week death check (n < 50/week): {'⚠️' if any(w['cnt'] < 50 for w in weekly) else '✅'}")

    return "\n".join(lines) + "\n"


# ── Main ───────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    date_label = now.strftime("%Y-%m-%d %H:%M UTC")

    # ── 1. Query all periods ──────────────────────────────────────────
    periods = [
        period_stats("Trailing 7d", "7 days"),
        period_stats("Trailing 30d", "30 days"),
        period_stats("Trailing 90d", "90 days"),
    ]

    # ── 2. OOS count since May 2026 ───────────────────────────────────
    oos_count = total_clean_oos_since("2026-05-01")

    # ── 3. Symbol breakdown (trailing 90d) ────────────────────────────
    symbols = symbol_breakdown("90 days")

    # ── 4. Weekly signal counts for death trigger ─────────────────────
    weekly = weekly_signal_counts(4)

    # ── 5. All-time reference ─────────────────────────────────────────
    all_time = all_time_wr_oos()

    # ── 6. Build report ───────────────────────────────────────────────
    report = build_report(now, periods, oos_count, symbols, weekly, all_time)

    # ── 7. Save report to Obsidian ────────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    report_path = os.path.join(OUTPUT_DIR, f"watchlist-short-1h-low-vol-neutral-{today_str}.md")
    with open(report_path, "w") as f:
        f.write(report)

    # ── 8. Check triggers ─────────────────────────────────────────────
    wr_90d = periods[2]["wr"] if len(periods) > 2 and periods[2]["cnt"] > 0 else 0
    wr_30d = periods[1]["wr"] if len(periods) > 1 and periods[1]["cnt"] > 0 else 0
    wr_7d = periods[0]["wr"] if len(periods) > 0 and periods[0]["cnt"] > 0 else 0

    stdout_lines = []

    promotion = check_promotion_trigger(oos_count, wr_90d)
    if promotion:
        stats = {
            "oos_count": oos_count,
            "wr_90d": wr_90d,
            "wr_30d": wr_30d,
            "wr_7d": wr_7d,
            "report_path": report_path,
        }
        ticket_result = create_re_evaluation_ticket(stats)
        stdout_lines.append(f"[PROMOTION TRIGGER] SHORT 1h LOW vol NEUTRAL macro — OOS re-evaluation needed!")
        stdout_lines.append(f"  WR: {wr_90d}% (90d), OOS count: {oos_count}")
        stdout_lines.append(f"  Report: {report_path}")
        stdout_lines.append(f"  Kanban ticket: {ticket_result}")
        stdout_lines.append("")

    death, death_reason = check_death_trigger(weekly, wr_90d)
    if death:
        stdout_lines.append(f"[DEATH TRIGGER] SHORT 1h LOW vol NEUTRAL macro — pattern may be dead!")
        stdout_lines.append(f"  Reason: {death_reason}")
        stdout_lines.append(f"  WR: {wr_90d}% (90d)")
        stdout_lines.append(f"  Report: {report_path}")
        stdout_lines.append("")

    # ── 9. Watchdog: silent unless trigger fires ──────────────────────
    if stdout_lines:
        sys.stdout.write("\n".join(stdout_lines) + "\n")
    # else: silent — stdout is empty, cron delivers nothing (watchdog pattern)

    return 0


if __name__ == "__main__":
    sys.exit(main())
