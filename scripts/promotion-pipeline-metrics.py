#!/usr/bin/env python3
"""
Promotion Pipeline Health Dashboard — no_agent cron (every 6h)
Canonical source: ~/.hermes/profiles/sycode-trading-pm/scripts/promotion-pipeline-metrics.py

Collects:
  - Inbox stage counts (candidates-inbox.md)
  - Kanban promotion/review task counts (SQLite)
  - DB pipeline health (signal_journeys, strategy_pool, signal_definitions)
  - Stalled-pattern escalation alerts
  - SLA breach report per pipeline stage

Outputs to: ~/obsidian/quant-team/governance/promotion-pipeline-metrics.md
"""
import os, re, sys, sqlite3, argparse
from datetime import datetime, timezone

from second_brain_writer import write_markdown_atomic

# ── Paths ──────────────────────────────────────────────────────────────
INBOX_FILE = os.path.expanduser("~/obsidian/quant-team/strategies/candidates-inbox.md")
OUTPUT_FILE = os.path.expanduser("~/obsidian/quant-team/governance/promotion-pipeline-metrics.md")
KANBAN_DB = os.path.expanduser("~/.hermes/kanban/boards/sycode-trading/kanban.db")
HOME = os.path.expanduser("~")

# ── PSQL helpers (same pattern as persistence-health-watch.sh) ─────────
PSQL_CMD = [
    "docker", "exec", "-e", "PGPASSWORD=postgres",
    "sycodetrading-supabase-db", "psql",
    "-h", "localhost", "-U", "postgres", "-d", "postgres",
    "-v", "ON_ERROR_STOP=1", "-t", "-A", "-P", "pager=off", "-c",
]

def db_query(sql):
    """Run SQL via psql, return stdout or None on failure."""
    import subprocess
    try:
        r = subprocess.run(PSQL_CMD + [sql], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


# ── SLA targets (from pipeline audit t_46b48819) ────────────────────────
# Each target: (target_hours, breach_hours, label)
# Breach threshold: >25% over target => flag with ⛔
SLA_BY_STAGE = {
    "discovery_to_synthesis":       (24,  32,  "Discovery→Synthesis"),
    "synthesis_to_registration":    (4,    6,  "Synthesis→Registration"),
    "registration_to_review_expedited":  (12, 15, "Registration→Review (expedited)"),
    "registration_to_review_standard":   (48, 60, "Registration→Review (standard)"),
    "review_to_deploy":             (24,  36,  "Review→Deploy"),
    "total_standard":               (120, 168, "Total (standard)"),
    "total_expedited":              (24,   36, "Total (expedited)"),
}

# Map kanban task title keywords → pipeline stage for SLA matching
STAGE_FROM_KEYWORD = {
    "mining":    "discovery_to_synthesis",
    "research":  "discovery_to_synthesis",
    "discover":  "discovery_to_synthesis",
    "sweep":     "discovery_to_synthesis",
    "synthesis": "synthesis_to_registration",
    "synthes":   "synthesis_to_registration",
    "regist":    "registration_to_review_standard",
    "review":    "review_to_deploy",
    "deploy":    "review_to_deploy",
    "wire":      "review_to_deploy",
}

# Pipeline-specific keywords that identify a task as promotion-pipeline work
PIPELINE_KEYWORDS = [
    "promot", "promotion", "pattern", "expedite",
    "synthesis", "synthes", "weekend short",
    "us session", "funding rate", "macro regime",
    "vol regime", "volatility", "cycle-trading",
    "phase b follow",  # very specific pipeline task
]
STAGE_LOOKUP_KEYWORDS = PIPELINE_KEYWORDS


def _stage_from_title(title):
    """Return the SLA stage key a task belongs to, or None.

    Only matches tasks that are explicitly about the promotion pipeline,
    not general engineering code reviews (PR reviews, branch reviews).
    """
    tl = title.lower()

    # Exclude engineering/non-pipeline task categories
    exclude_indicators = [
        "pr #", "pr —", "branch —", "landing approval",
        "rebase", "merge", "code review",
        "research-actionable",  # research followup, not pipeline stage
        "verify fusion", "wire signal fusion",  # infra tasks
        "p3 fix", "p0: deploy",  # infra deploy tasks, not pattern promotions
    ]
    if any(ind in tl for ind in exclude_indicators):
        return None

    # Pipeline-specific keywords (strong match) — must contain at least one
    is_pipeline = any(kw in tl for kw in PIPELINE_KEYWORDS)

    if not is_pipeline:
        return None

    # Determine stage
    for keyword, stage in STAGE_FROM_KEYWORD.items():
        if keyword in tl:
            return stage

    return None


def _extract_pattern_name(title):
    """Extract a human-readable pattern name from a task title."""
    # Try to find known pattern fragments
    patterns = []
    known = [
        "Weekend Short", "US Session Open", "Funding Rate Cycle",
        "Macro Regime", "Vol Regime", "Volatility Regime",
        "SHORT 1h LOW vol", "LONG 4h TRANSITIONING",
        "SHORT 1h NORMAL RSI", "Weekend Short 1m",
        "US Session Open Short 1h", "cycle-trading",
    ]
    for k in known:
        if k.lower() in title.lower():
            patterns.append(k)
    if patterns:
        return " + ".join(patterns)
    # Fallback: extract first meaningful noun phrase after a colon or em-dash
    parts = re.split(r'[:\u2014\u2013]', title, maxsplit=2)
    for p in parts:
        p = p.strip()
        if p and len(p) > 5 and p.lower() != p:  # has some caps = likely proper name
            return p[:60]
    if len(title) > 8:
        return title[:60]
    return title


def fmt_hours(hours):
    """Format hours into human duration string."""
    if hours < 0.017:  # <1 minute
        return "<1m"
    elif hours < 1.0:
        return f"{int(hours*60)}m"
    elif hours < 24:
        return f"{hours:.1f}h"
    else:
        return f"{hours/24:.1f}d"


def compute_sla_breaches(now, kanban_db):
    """
    Scan the kanban board for active promotion-pipeline tasks
    and compare their elapsed time against SLA targets.

    Returns a list of dicts: {
        "pattern": str, "stage_label": str, "stage_key": str,
        "elapsed_hours": float, "target_hours": float, "breach_hours": float,
        "is_breached": bool, "breach_pct": float, "status": str
    }
    """
    results = []

    if not os.path.isfile(kanban_db):
        return results

    try:
        conn = sqlite3.connect(f"file:{kanban_db}?mode=ro", uri=True)
        cur = conn.cursor()
    except Exception:
        return results

    try:
        # Query all non-done tasks that match pipeline keywords
        where_clause = " OR ".join(
            f"title LIKE '%{kw}%'" for kw in STAGE_LOOKUP_KEYWORDS
        )
        cur.execute(f"""
            SELECT id, title, status, created_at, started_at, completed_at
            FROM tasks
            WHERE ({where_clause})
              AND status NOT IN ('done', 'cancelled', 'archived')
            ORDER BY created_at DESC
        """)
        rows = cur.fetchall()
    except Exception:
        conn.close()
        return results

    for task_id, title, status, created_at, started_at, completed_at in rows:
        stage_key = _stage_from_title(title)
        if not stage_key:
            continue

        # Determine effective start time for "time in stage"
        if started_at:
            stage_start = started_at
        else:
            stage_start = created_at

        elapsed_seconds = now.timestamp() - stage_start
        elapsed_hours = elapsed_seconds / 3600.0

        target_hours, breach_hours, stage_label = SLA_BY_STAGE.get(
            stage_key, (None, None, None)
        )
        if target_hours is None:
            continue

        # If task is completed, use completed_at instead
        if completed_at:
            elapsed_hours = (completed_at - stage_start) / 3600.0

        is_breached = elapsed_hours > breach_hours
        breach_pct = ((elapsed_hours - target_hours) / target_hours * 100) if target_hours > 0 else 0

        results.append({
            "task_id": task_id,
            "pattern": _extract_pattern_name(title),
            "stage_label": stage_label,
            "stage_key": stage_key,
            "elapsed_hours": round(elapsed_hours, 1),
            "target_hours": target_hours,
            "breach_hours": breach_hours,
            "is_breached": is_breached,
            "breach_pct": round(breach_pct, 0),
            "status": status,
        })

    conn.close()
    return results


# ── Inbox parsing ──────────────────────────────────────────────────────
def parse_inbox(path):
    """Return (counts_dict, dates_dict, lines) from candidates-inbox.md."""
    if not os.path.isfile(path):
        return {"new": 0, "validating": 0, "promoted": 0, "rejected": 0}, {}, []

    text = open(path).read()
    lines = text.split("\n")
    counts = {"new": 0, "validating": 0, "promoted": 0, "rejected": 0}
    dates_found = sorted(set(re.findall(r"(\d{4}-\d{2}-\d{2})", text)))

    for line in lines:
        s = line.strip()
        if not s.startswith("|"):
            continue
        # Skip separator rows (dashes/colons only between pipes)
        if re.match(r"^\|[-\s:|]+\|$", s):
            continue
        parts = [p.strip() for p in s.split("|")]
        if len(parts) < 6:
            continue
        # Skip header rows — "Fingerprint" only appears in header text
        if any(re.search(r"^Fingerprint", p, re.IGNORECASE) for p in parts):
            continue
        # Status is last non-empty cell
        raw_status = parts[-2] if parts[-1] == "" else parts[-1]
        raw_status = raw_status.lower()
        if "promoted" in raw_status:
            counts["promoted"] += 1
        elif "rejected" in raw_status:
            counts["rejected"] += 1
        elif "validating" in raw_status:
            counts["validating"] += 1
        else:
            counts["new"] += 1

    return counts, dates_found, lines


# ── Main ───────────────────────────────────────────────────────────────
def main(args=None):
    now = datetime.now(timezone.utc)
    date_label = now.strftime("%Y-%m-%d %H:%M UTC")
    verbose = args and args.verbose

    # ── 1. Inbox ───────────────────────────────────────────────────────
    counts, dates, _ = parse_inbox(INBOX_FILE)

    # ── 2. Kanban board ═ current promotion/review tasks ────────────────
    kanban_info = {}
    if os.path.isfile(KANBAN_DB):
        try:
            conn = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute("""
                SELECT status, COUNT(*) FROM tasks
                WHERE (title LIKE '%promot%' OR title LIKE '%review%'
                       OR title LIKE '%expedite%')
                  AND status NOT IN ('done', 'archived')
                GROUP BY status
            """)
            kanban_info["rows"] = cur.fetchall()
            conn.close()
        except Exception:
            kanban_info["error"] = str(sys.exc_info()[1])

    # ── 3. DB queries ──────────────────────────────────────────────────
    sj_7d = db_query("SELECT COUNT(*) FROM signal_journeys WHERE triggered_at >= NOW() - INTERVAL '7 days'")
    sj_24h = db_query("SELECT COUNT(*) FROM signal_journeys WHERE triggered_at >= NOW() - INTERVAL '24 hours'")
    sj_active = db_query("""SELECT COUNT(*) FROM signal_journeys
        WHERE triggered_at >= NOW() - INTERVAL '24 hours'
          AND current_stage IN ('TRIGGERED','VALIDATING','APPROVED','EXECUTED')""")
    sp_paper = db_query("SELECT COUNT(*) FROM strategy_pool WHERE status = 'paper'")
    sp_archived = db_query("SELECT COUNT(*) FROM strategy_pool WHERE status = 'archived'")
    sig_def_active = db_query("SELECT COUNT(*) FROM signal_definitions WHERE last_seen_at >= NOW() - INTERVAL '24 hours'")
    sig_def_total = db_query("SELECT COUNT(*) FROM signal_definitions")

    # Safe-int conversion
    def safe_int(v, default=0):
        if v is None or v == "":
            return default
        return int(re.sub(r"[^0-9]", "", v))

    sj_7d = safe_int(sj_7d)
    sj_24h = safe_int(sj_24h)
    sj_active = safe_int(sj_active)
    sp_paper = safe_int(sp_paper)
    sp_archived = safe_int(sp_archived)
    sig_def_active = safe_int(sig_def_active)
    sig_def_total = safe_int(sig_def_total)

    # ── 4. Compute age info ────────────────────────────────────────────
    hours_old = {}
    for i, status in enumerate(["promoted", "rejected", "new", "validating"]):
        if dates:
            ref = dates[0]
            try:
                dt = datetime.strptime(ref, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                hours_old[status] = (ref, int((now - dt).total_seconds() / 3600))
            except ValueError:
                pass

    # ── 5. Build output (aim <50 lines) ────────────────────────────────
    lines = []
    lines.append(f"# Promotion Pipeline \u2014 {date_label}")
    lines.append("")
    lines.append("**Funnel Health:**")
    lines.append("")
    lines.append("| Stage | Count | Oldest | Notes |")
    lines.append("|---|---|---|---|")

    for stage, label in [("new", "Inbox new"), ("validating", "Inbox validating"),
                          ("promoted", "Inbox promoted"), ("rejected", "Inbox rejected")]:
        cnt = counts.get(stage, 0)
        ag = hours_old.get(stage, ("-", ""))
        age_str = f"{ag[0]} ({ag[1]}h)" if ag[1] else "-"
        src = "\u2014"
        lines.append(f"| {label} | {cnt} | {age_str} | {src} |")

    lines.append(f"| Paper strategies (strategy_pool) | {sp_paper} | \u2014 | From DB (status=paper) |")
    lines.append(f"| Active generators (signals 24h) | {sj_active} | \u2014 | Signal_journeys TRIGGERED+ |")
    lines.append(f"| Signal definitions active 24h | {sig_def_active}/{sig_def_total} | \u2014 | From DB |")

    lines.append("")
    lines.append("**Kanban Promotion Tasks:**")
    rows = kanban_info.get("rows", [])
    if rows and kanban_info.get("error") is None:
        for status, cnt in rows:
            lines.append(f"- {status}: {cnt} task(s)")
    else:
        lines.append("- No active promotion/review tasks on board")

    lines.append("")
    lines.append("**Signal Pipeline Health:**")
    lines.append("")
    lines.append(f"- Signal journeys in last 7d: {sj_7d}")
    lines.append(f"- Signal journeys in last 24h: {sj_24h}")
    lines.append(f"- Active (TRIGGERED+/24h): {sj_active}")
    lines.append(f"- Strategy_pool paper: {sp_paper}")
    lines.append(f"- Strategy_pool archived: {sp_archived}")
    lines.append(f"- Signal definitions active in 24h: {sig_def_active}/{sig_def_total}")

    lines.append("")
    lines.append("**Blocked / Stalled:**")
    stall_found = False

    if "new" in hours_old and counts.get("new", 0) > 0:
        ref, h = hours_old["new"]
        if h > 72:
            lines.append(f"- ⚠️ Patterns in 'new' >72h (since {ref}, {h}h) — VALIDATION STALLED")
            stall_found = True
        elif h > 48:
            lines.append(f"- ⚡ Patterns in 'new' >48h (since {ref}, {h}h) — validate soon")
            stall_found = True

    if "validating" in hours_old and counts.get("validating", 0) > 0:
        ref, h = hours_old["validating"]
        if h > 168:
            lines.append(f"- ⚠️ Patterns in 'validating' >1w (since {ref}, {h}h) — stalled")
            stall_found = True

    if "promoted" in hours_old:
        ref, h = hours_old["promoted"]
        if h > 48:
            lines.append(f"- ⚠️ Promoted pattern >48h without signal activity (since {ref}, {h}h)")
            stall_found = True

    if sj_active == 0 and sj_24h > 0:
        lines.append("- ⚠️ 0 active signal journeys (TRIGGERED+) in last 24h despite raw volume")
        stall_found = True
    elif sj_24h == 0 and sp_paper > 0:
        lines.append("- ⚠️ No signal journeys at all in 24h — pipeline may be stalled")
        stall_found = True

    if not stall_found:
        lines.append("- \u2705 No stalled patterns detected")

    lines.append("")

    # ── 5b. SLA Breach Report ───────────────────────────────────────────
    sla_breaches = compute_sla_breaches(now, KANBAN_DB)
    lines.append("### ⏱ SLA Breach Report")
    lines.append("")
    if sla_breaches:
        lines.append("| Pattern | Stage | Time in Stage | SLA Target | Breach? |")
        lines.append("|---|---|---|---|---|")
        for b in sla_breaches:
            time_str = fmt_hours(b["elapsed_hours"])
            target_str = f"\u2264{fmt_hours(b['target_hours'])}"
            if b["is_breached"]:
                breach_str = f"\u26d4 BREACH ({b['breach_pct']:.0f}%)"
            else:
                breach_str = f"\u2705 OK ({b['breach_pct']:.0f}%)"
            lines.append(f"| {b['pattern']} | {b['stage_label']} | {time_str} | {target_str} | {breach_str} |")
    else:
        lines.append("_No active promotion-pipeline tasks with SLA tracking._")

    lines.append("")

    # ── 6. NEEDS-FRANK escalation ──────────────────────────────────────
    critical = False
    if "new" in hours_old and counts.get("new", 0) > 0:
        ref, h = hours_old["new"]
        if h > 72:
            lines.append("[NEEDS-FRANK] Pattern(s) in 'new' >72h without validation. Pipeline intake bottleneck.")
            critical = True

    if "promoted" in hours_old:
        ref, h = hours_old["promoted"]
        if h > 48:
            lines.append("[NEEDS-FRANK] Promoted pattern >48h without signal regeneration. Paper strategy may be stalled.")
            critical = True

    if sj_24h == 0 and sp_paper > 0:
        lines.append("[NEEDS-FRANK] Zero signal journeys in 24h while paper strategies exist. Entire signal pipeline may be down.")
        critical = True

    # SLA breach escalation (>25% over target = material breach)
    for b in sla_breaches:
        if b["is_breached"] and b["breach_pct"] > 25:
            lines.append(
                f"[NEEDS-FRANK] Pattern \"{b['pattern']}\" breached "
                f"{b['stage_label']} by {b['breach_pct']:.0f}% "
                f"({fmt_hours(b['elapsed_hours'])} vs target {fmt_hours(b['target_hours'])}) "
                f"— auto-generated PM alert"
            )
            critical = True

    if not critical:
        lines.append("_No Frank-escalation items._")

    # ── 7. Write output ────────────────────────────────────────────────
    output = "\n".join(lines) + "\n"

    report_date = now.strftime("%Y-%m-%d")
    write_markdown_atomic(
        OUTPUT_FILE,
        output,
        title="Promotion Pipeline Health Dashboard",
        type="moc",
        status="active",
        created="2026-07-05",
        updated=report_date,
        confidence="high",
        tags=["sycode", "promotion-pipeline", "health", "dashboard"],
        sources=[
            "strategies/candidates-inbox.md",
            "/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db",
            "sycodetrading-supabase-db:signal_journeys",
        ],
        project="sycode-trading",
        owners=["trading-devops"],
        knowledge_tier="compiled",
        generated=True,
        generator="promotion-pipeline-metrics.py",
        operational_status="needs-frank" if critical else "healthy",
    )

    if verbose:
        print(output)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Promotion Pipeline Health Dashboard")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print output to stdout")
    args = parser.parse_args()
    sys.exit(main(args))
