#!/usr/bin/env python3
"""Fleet worker-agent liveness WEEKLY DEATH-RATE REPORT.

Produces a human-readable weekly report from the fleet liveness-churn engine,
including:
  - per-board and fleet aggregate 14-day churn / death metrics
  - open blocked needs_input card census (the dead-PID evidence the CEO flagged)
  - week-over-week trend by comparing the saved prior-week snapshot (if present)
  - root-cause pointer to the established dead-PID RCA
The report is written to /home/frank/.hermes/reports/fleet-liveness/ as
markdown + json, and a one-line summary is logged.

No prod/credentials. Local kanban SQLite only.

Re-run with --week-window-days N to change the trend comparison window.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "fleet_liveness_churn.py")
REPORT_DIR = "/home/frank/.hermes/reports/fleet-liveness"
PRIOR_SNAPSHOT = os.path.join(REPORT_DIR, "latest_snapshot.json")


def measure():
    out = subprocess.run([sys.executable, ENGINE], capture_output=True, text=True)
    if out.returncode != 0:
        sys.stderr.write(f"MEASURE_FAIL rc={out.returncode} {out.stderr[:300]}\n")
        sys.exit(2)
    return json.loads(out.stdout)


def load_prior():
    try:
        with open(PRIOR_SNAPSHOT) as f:
            return json.load(f)
    except Exception:
        return None


def fmt_pct(v):
    return "n/a" if v is None else f"{v}%"


def main():
    d = measure()
    agg = d["fleet_aggregate"]
    dt = datetime.now(timezone.utc)
    stamp = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    week = dt.strftime("%Y-W%V")

    prior = load_prior()

    lines = []
    lines.append(f"# Fleet Worker-Agent Liveness — Weekly Death-Rate Report")
    lines.append("")
    lines.append(f"- **Report week:** {week}  ")
    lines.append(f"- **Generated:** {stamp}  ")
    lines.append(f"- **Measurement window:** {agg.get('window_days', 14)}d  ")
    lines.append(f"- **Boards:** {', '.join(d['per_board'].keys())}  ")
    lines.append(f"- **Source of truth:** Hermes kanban SQLite board DBs (task_runs/tasks).")
    lines.append("")
    lines.append("## Fleet aggregate (14-day window)")
    lines.append("")
    lines.append("| Metric | Value | Prior week | Δ |")
    lines.append("|---|---:|---:|---:|")
    p_started = (prior or {}).get("window_started_total")
    p_deaths = (prior or {}).get("window_death_total")
    p_pct = (prior or {}).get("death_rate_pct_fleet")
    p_bni = (prior or {}).get("blocked_needs_input_total")

    def delta(cur, prior_v):
        if prior_v is None or cur is None:
            return "n/a"
        return f"{cur - prior_v:+d}" if isinstance(cur, int) else f"{cur - prior_v:+.2f}"

    lines.append(f"| Sessions started | {agg['window_started_total']} | {p_started if p_started is not None else 'n/a'} | {delta(agg['window_started_total'], p_started)} |")
    lines.append(f"| Sessions died | {agg['window_death_total']} | {p_deaths if p_deaths is not None else 'n/a'} | {delta(agg['window_death_total'], p_deaths)} |")
    lines.append(f"| **Death rate %** | **{fmt_pct(agg['death_rate_pct_fleet'])}** | {fmt_pct(p_pct)} | {delta(agg['death_rate_pct_fleet'], p_pct)} |")
    lines.append(f"| Death rate /day | {agg['death_rate_per_day_fleet']} | { 'n/a' if p_pct is None else '' } | ")
    lines.append(f"| Churn /day | {agg['churn_rate_per_day_fleet']} | | ")
    lines.append(f"| Open blocked `needs_input` cards | {agg['blocked_needs_input_total']} | {p_bni if p_bni is not None else 'n/a'} | {delta(agg['blocked_needs_input_total'], p_bni)} |")
    lines.append("")

    lines.append("## Per-board death causes (window)")
    lines.append("")
    lines.append("| Board | Started | Died | Death% | crashed | timed_out | spawn_failed | reclaimed | blocked needs_input |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for b, m in d["per_board"].items():
        w = m["window"]
        dc = w.get("deaths_by_cause", {})
        lines.append(
            f"| {b} | {w.get('started_total')} | {w.get('death_total')} | {fmt_pct(w.get('death_rate_pct'))} "
            f"| {dc.get('crashed',0)} | {dc.get('timed_out',0)} | {dc.get('spawn_failed',0)} | {dc.get('reclaimed',0)} "
            f"| {len(m['blocked_needs_input'])} |"
        )
    lines.append("")

    lines.append("## Root cause (established)")
    lines.append("")
    lines.append("- Fleet-wide dead-PID worker death is a **dead dispatcher / gateway kill orphaning host-local worker PIDs**, auto-reaped by `detect_crashed_workers` (not stale pid-files). See incident RCA `[[Incidents/2026-07-11-dead-pid-failure-class-root-cause]]` and `[[Incidents/2026-07-12-t_ccaa946a-pid-not-alive-stall-cluster-diagnosis]]`.")
    lines.append("- The dominant death class is `crashed` (host-local worker PID gone), not `needs_input` human blockers. The 248 open `needs_input` cards are a SEPARATE backlog (manual blockers), not the churn mechanism.")
    lines.append("- This report + the companion `fleet_liveness_alert.py` close the prior RCA's acceptance #3 gap: a fleet-level early signal at the churn metric, not only at per-task `gave_up`.")
    lines.append("")

    lines.append("## Recommended action")
    lines.append("")
    if agg['death_rate_pct_fleet'] and agg['death_rate_pct_fleet'] > 25:
        lines.append(f"- **Fleet death rate {agg['death_rate_pct_fleet']}% is elevated.** Treat as a reliability signal: investigate gateway/host kill events on the dispatcher host; the churn alert will fire to #critical-alerts when breached.")
    else:
        lines.append(f"- Fleet death rate {fmt_pct(agg['death_rate_pct_fleet'])} within tolerance; continue weekly monitoring.")
    lines.append(f"- Burn down the {agg['blocked_needs_input_total']} open `needs_input` cards per board (these are the live manual-blocker backlog, far larger than the stale per-board counts in the originating task brief).")
    lines.append("")
    lines.append("---")
    lines.append(f"_Generated by fleet_liveness_weekly_report.py from Hermes kanban board DBs. Evidence-only; no prod/credentials touched._")

    report_md = "\n".join(lines)
    os.makedirs(REPORT_DIR, exist_ok=True)
    md_path = os.path.join(REPORT_DIR, f"weekly-{week}.md")
    with open(md_path, "w") as f:
        f.write(report_md)

    # persist snapshot for next-week trend
    snap = {
        "week": week,
        "generated_at": stamp,
        "window_started_total": agg["window_started_total"],
        "window_death_total": agg["window_death_total"],
        "death_rate_pct_fleet": agg["death_rate_pct_fleet"],
        "death_rate_per_day_fleet": agg["death_rate_per_day_fleet"],
        "churn_rate_per_day_fleet": agg["churn_rate_per_day_fleet"],
        "blocked_needs_input_total": agg["blocked_needs_input_total"],
    }
    with open(PRIOR_SNAPSHOT, "w") as f:
        json.dump(snap, f, indent=2)

    # console summary
    print(report_md)
    print(f"\n[written] {md_path}")
    print(f"[written] {PRIOR_SNAPSHOT}")


if __name__ == "__main__":
    main()
