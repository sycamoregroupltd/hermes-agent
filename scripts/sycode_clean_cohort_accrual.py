#!/usr/bin/env python3
# invoker: hermes cron job (to be registered by devops card, daily 06:50) — manual: python3 ~/.hermes/scripts/sycode_clean_cohort_accrual.py
#
# NS-P3.5 (sycode-trading/t_99c3673d): Clean-cohort accrual dashboard.
#
# Reports, per active arm (timeframe x direction), the accrual of CLEAN-EPOCH
# outcomes against the research bars:
#   - n >= 100 clean outcomes  -> C4 fusion re-baseline may start (t_b53a2562, NS-P1.8)
#   - n >= 300 clean outcomes per arm -> NS-P4 research-engine entry gate
#
# Clean cohort = rows with created_at > EPOCH_START. EPOCH_START is the
# factory-parity redeploy f553af43c cutoff (2026-07-05 22:41Z), the moment BOTH
# 07-05 enrichment breaks were verifiably cured. The registry epoch
# clean-candidate-599f58e7e opens earlier (22:08Z); rows in 22:08-22:41 carry the
# second enrichment break and are excluded here on purpose (conservative cohort).
#
# While clean-epoch closes/outcomes are zero (paper execution + outcome factory
# state), the dashboard reports SIGNAL accrual per arm plus projections:
#   - projected close rate per arm from pre-epoch realized ratios
#     (trade_close_events / signal_journeys over 06-29 -> 07-05 22:08Z)
#   - ETA to the 100/300 bars at the observed clean-epoch signal rate, CONDITIONAL
#     on the outcome factory running (journey_finalizer throughput historically
#     exceeded signal inflow, so signal rate is the binding rate; add the label
#     horizon as lag).
#
# Behavior:
#   - SELECT-only against sycodetrading-supabase-db (read-only enforced via
#     PGOPTIONS default_transaction_read_only=on).
#   - Writes a markdown report to
#     /home/frank/obsidian/sycode-trading/analytics/clean-cohort-accrual/YYYY-MM-DD.md
#     (the ONLY thing this script ever writes; same-day reruns overwrite).
#   - Exits 2 + ALERT line when the factory is STALLED: clean-epoch signals are
#     accruing but ZERO new final clean-epoch outcomes landed in the last 24h
#     (the "tokens into space" failure mode for NS-P3). Exits 1 on operational
#     error, 0 otherwise.
#   - --self-test: proves the ETA math and the stall-alert logic on synthetic
#     data (no DB, no report write).
#
# Consumers: weekly north-star sweep, NS-P4 entry-gate decisions, Frank digest;
# the registering cron routes non-zero exit to discord #critical-alerts.

import argparse
import csv
import io
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from second_brain_writer import write_markdown_atomic

DB_CONTAINER = "sycodetrading-supabase-db"
REPORT_DIR = Path("/home/frank/obsidian/sycode-trading/analytics/clean-cohort-accrual")

EPOCH_START = "2026-07-05 22:41:00+00"   # f553af43c redeploy; see header
REGISTRY_EPOCH = "clean-candidate-599f58e7e (opens 2026-07-05 22:08Z; we cut at 22:41Z, see header)"
PRE_EPOCH_LO = "2026-06-29 00:00:00+00"  # post-leak window for projection ratios
PRE_EPOCH_HI = "2026-07-05 22:08:00+00"

BAR_REBASELINE = 100   # C4 fusion re-baseline (t_b53a2562, NS-P1.8)
BAR_RESEARCH = 300     # NS-P4 entry gate, per arm
MIN_ARM_SIGNALS = 5    # arms below this since epoch are listed but not projected

# NS-P3.1 random-entry null-baseline arm. The injector (randomEntryInjector.ts)
# opens PAPER positions with strategy_name = this id; its outcomes land in
# decision_outcomes via managed_positions but carry contamination_reason =
# 'missing_journey_lineage' (the control bypasses signal_journeys). That flag is
# an EXPECTED control-arm artifact, NOT a data defect, so the accrual report
# deliberately includes this arm regardless of the contamination flag.
DEFAULT_CONTROL_ARM = "random_entry_paper_control_v1"


def run_sql(sql):
    cmd = [
        "docker", "exec",
        "-e", "PGOPTIONS=-c default_transaction_read_only=on",
        DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "postgres",
        "-X", "-q", "-v", "ON_ERROR_STOP=1", "--csv", "-c", sql,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed rc={proc.returncode}: {proc.stderr.strip()[:500]}")
    return list(csv.DictReader(io.StringIO(proc.stdout)))


# ----------------------------------------------------------------------------
# Data collection (SELECT-only)
# ----------------------------------------------------------------------------
def fetch():
    d = {}
    d["arms"] = run_sql(f"""
        SELECT timeframe, direction,
               count(*) AS n_signals,
               count(*) FILTER (WHERE executed_at IS NOT NULL) AS n_executed,
               EXTRACT(EPOCH FROM (now() - '{EPOCH_START}'::timestamptz))/3600.0 AS epoch_hours
        FROM signal_journeys WHERE created_at > '{EPOCH_START}'
        GROUP BY 1,2 ORDER BY 3 DESC;""")
    d["clean_outcomes"] = run_sql(f"""
        SELECT timeframe, direction, count(*) AS n_outcomes,
               count(*) FILTER (WHERE created_at > now() - interval '24 hours') AS n_last24h
        FROM decision_outcomes
        WHERE created_at > '{EPOCH_START}' AND is_final
          AND COALESCE(contaminated, false) = false
        GROUP BY 1,2;""")
    d["clean_closes"] = run_sql(f"""
        SELECT timeframe, direction, count(*) AS n_closes
        FROM trade_close_events WHERE created_at > '{EPOCH_START}'
          AND COALESCE(contaminated, false) = false
        GROUP BY 1,2;""")
    d["r_labels"] = run_sql("SELECT count(*) AS n FROM r_multiple_labels;")
    d["pre_ratio"] = run_sql(f"""
        WITH s AS (
          SELECT timeframe, direction, count(*) AS n_signals
          FROM signal_journeys
          WHERE created_at >= '{PRE_EPOCH_LO}' AND created_at < '{PRE_EPOCH_HI}'
          GROUP BY 1,2),
        c AS (
          SELECT timeframe, direction, count(*) AS n_closes
          FROM trade_close_events
          WHERE created_at >= '{PRE_EPOCH_LO}' AND created_at < '{PRE_EPOCH_HI}'
          GROUP BY 1,2)
        SELECT s.timeframe, s.direction, s.n_signals, COALESCE(c.n_closes,0) AS n_closes
        FROM s LEFT JOIN c USING (timeframe, direction);""")
    d["factory"] = run_sql("""
        SELECT max(created_at) FILTER (WHERE label_source='journey_finalizer')::text AS finalizer_last,
               max(created_at) FILTER (WHERE label_source='trade_close')::text AS closer_last
        FROM decision_outcomes WHERE created_at > now() - interval '14 days';""")

    # NS-P3.1 strategy/bandit level positions and outcomes
    d["strategy_positions"] = run_sql(f"""
        SELECT strategy_name, count(*) AS n_positions,
               count(*) FILTER (WHERE status = 'closed') AS n_closed
        FROM managed_positions WHERE created_at > '{EPOCH_START}'
        GROUP BY 1;""")
    # NS-P3.1 control arm: its outcomes carry contamination_reason =
    # 'missing_journey_lineage' (expected control artifact, NOT a defect) so the
    # clean-cohort filter COALESCE(contaminated,false)=false would EXCLUDE the
    # entire null baseline. We therefore include control-arm outcomes
    # unconditionally by arm id, while keeping the uncontaminated filter for all
    # other (signal-derived) arms.
    d["strategy_outcomes"] = run_sql(f"""
        SELECT COALESCE(o.bandit_arm_id_at_decision, mp.strategy_name) AS arm_name,
               count(*) AS n_outcomes,
               count(*) FILTER (WHERE o.created_at > now() - interval '24 hours') AS n_last24h,
               avg(o.canonical_reward::numeric) AS avg_reward,
               stddev(o.canonical_reward::numeric) AS stddev_reward,
               count(*) FILTER (WHERE o.canonical_reward > 0) AS n_wins,
               count(*) FILTER (WHERE o.canonical_reward <= 0) AS n_losses,
               bool_or(COALESCE(o.contaminated, false)) AS any_contaminated,
               max(o.contamination_reason) FILTER (WHERE o.contaminated) AS contamination_reason
        FROM decision_outcomes o
        LEFT JOIN managed_positions mp ON o.position_id = mp.id
        WHERE o.created_at > '{EPOCH_START}' AND o.is_final
          AND (COALESCE(o.contaminated, false) = false
               OR mp.strategy_name = '{DEFAULT_CONTROL_ARM}')
        GROUP BY 1;""")
    return d


# ----------------------------------------------------------------------------
# Pure computation (reused by --self-test)
# ----------------------------------------------------------------------------
def compute_expectancy_and_ci(n, avg_reward, stddev_reward):
    if n <= 0 or avg_reward is None:
        return None, None
    expectancy = float(avg_reward)
    if n >= 2 and stddev_reward is not None:
        stddev = float(stddev_reward)
        sem = stddev / math.sqrt(n)
        ci_half = 1.96 * sem
    else:
        ci_half = None
    return expectancy, ci_half


def compute(arms, clean_outcomes, clean_closes, pre_ratio, strategy_positions=None, strategy_outcomes=None, epoch_hours=24.0):
    """Returns (rows, totals, stalled, strat_rows). rows: one dict per arm with accrual,
    rate, projections, ETAs. stalled: True when signals accrue but no final
    clean outcome landed in the last 24h."""
    out_by_arm = {(r["timeframe"], r["direction"]): int(r["n_outcomes"]) for r in clean_outcomes}
    out24_total = sum(int(r["n_last24h"]) for r in clean_outcomes)
    close_by_arm = {(r["timeframe"], r["direction"]): int(r["n_closes"]) for r in clean_closes}
    ratio_by_arm = {}
    for r in pre_ratio:
        n_s, n_c = int(r["n_signals"]), int(r["n_closes"])
        ratio_by_arm[(r["timeframe"], r["direction"])] = (n_c / n_s) if n_s else 0.0

    rows, total_signals = [], 0
    for a in arms:
        key = (a["timeframe"], a["direction"])
        n_sig = int(a["n_signals"])
        total_signals += n_sig
        hours = float(a["epoch_hours"])
        rate_day = (n_sig / hours * 24.0) if hours > 0 else 0.0
        n_out = out_by_arm.get(key, 0)
        close_rate = ratio_by_arm.get(key)
        projectable = n_sig >= MIN_ARM_SIGNALS and rate_day > 0
        # Outcomes trail signals by the label horizon; finalizer throughput
        # historically exceeded signal inflow, so signal rate binds.
        eta100 = max(0.0, (BAR_REBASELINE - max(n_out, 0)) / rate_day) if projectable else None
        eta300 = max(0.0, (BAR_RESEARCH - max(n_out, 0)) / rate_day) if projectable else None
        rows.append({
            "arm": f"{a['timeframe']} {a['direction']}",
            "n_signals": n_sig,
            "n_executed": int(a["n_executed"]),
            "n_outcomes": n_out,
            "n_closes": close_by_arm.get(key, 0),
            "rate_day": rate_day,
            "close_rate": close_rate,
            "proj_closes_day": (close_rate * rate_day) if close_rate is not None else None,
            "eta100_d": eta100,
            "eta300_d": eta300,
        })
    totals = {
        "signals": total_signals,
        "outcomes": sum(out_by_arm.values()),
        "closes": sum(close_by_arm.values()),
        "out24": out24_total,
    }
    stalled = totals["signals"] > 0 and out24_total == 0

    # Strategy / Bandit arm level processing
    strat_pos_by_arm = {}
    if strategy_positions:
        for r in strategy_positions:
            if r["strategy_name"]:
                strat_pos_by_arm[r["strategy_name"]] = {
                    "n_positions": int(r["n_positions"]),
                    "n_closed": int(r["n_closed"])
                }

    default_control_arm = DEFAULT_CONTROL_ARM
    if default_control_arm not in strat_pos_by_arm:
        strat_pos_by_arm[default_control_arm] = {"n_positions": 0, "n_closed": 0}

    strat_out_by_arm = {}
    if strategy_outcomes:
        for r in strategy_outcomes:
            if r["arm_name"]:
                strat_out_by_arm[r["arm_name"]] = {
                    "n_outcomes": int(r["n_outcomes"]),
                    "n_last24h": int(r["n_last24h"]),
                    "avg_reward": float(r["avg_reward"]) if r["avg_reward"] is not None and r["avg_reward"] != "" else None,
                    "stddev_reward": float(r["stddev_reward"]) if r["stddev_reward"] is not None and r["stddev_reward"] != "" else None,
                    "n_wins": int(r["n_wins"]) if r["n_wins"] is not None and r["n_wins"] != "" else 0,
                    "n_losses": int(r["n_losses"]) if r["n_losses"] is not None and r["n_losses"] != "" else 0,
                    "any_contaminated": (r["any_contaminated"] == "t") if r["any_contaminated"] is not None else False,
                    "contamination_reason": r["contamination_reason"] if r["contamination_reason"] else None,
                }

    if default_control_arm not in strat_out_by_arm:
        strat_out_by_arm[default_control_arm] = {
            "n_outcomes": 0,
            "n_last24h": 0,
            "avg_reward": None,
            "stddev_reward": None
        }

    strat_rows = []
    for arm_name in sorted(set(list(strat_pos_by_arm.keys()) + list(strat_out_by_arm.keys()))):
        pos_info = strat_pos_by_arm.get(arm_name, {"n_positions": 0, "n_closed": 0})
        out_info = strat_out_by_arm.get(arm_name, {
            "n_outcomes": 0,
            "n_last24h": 0,
            "avg_reward": None,
            "stddev_reward": None
        })
        n_out = out_info["n_outcomes"]
        n_last24h = out_info["n_last24h"]
        avg_rew = out_info["avg_reward"]
        std_rew = out_info["stddev_reward"]
        n_wins = out_info.get("n_wins", 0)
        n_losses = out_info.get("n_losses", 0)
        any_cont = out_info.get("any_contaminated", False)
        cont_reason = out_info.get("contamination_reason")

        expectancy, ci_half = compute_expectancy_and_ci(n_out, avg_rew, std_rew)

        strat_rows.append({
            "arm_name": arm_name,
            "n_positions": pos_info["n_positions"],
            "n_closed": pos_info["n_closed"],
            "n_outcomes": n_out,
            "n_last24h": n_last24h,
            "n_wins": n_wins,
            "n_losses": n_losses,
            "any_contaminated": any_cont,
            "contamination_reason": cont_reason,
            "expectancy": expectancy,
            "ci_half": ci_half,
        })

    return rows, totals, stalled, strat_rows


# ----------------------------------------------------------------------------
# Report rendering
# ----------------------------------------------------------------------------
def fmt_expectancy_ci(exp, ci):
    if exp is None:
        return "—"
    if ci is None:
        return f"{exp:.3f} R"
    return f"{exp:.3f} ± {ci:.3f} R"


def render(rows, totals, stalled, n_rlabels, factory, now_utc, strat_rows, epoch_hours):
    L = []
    L.append(f"# Clean-cohort accrual — n per arm vs research bars — {now_utc:%Y-%m-%d}")
    L.append("")
    L.append(f"Generated {now_utc:%Y-%m-%d %H:%M}Z by `sycode_clean_cohort_accrual.py` "
             f"(NS-P3.5, card t_99c3673d). Cohort: `created_at > {EPOCH_START}` "
             f"— registry epoch {REGISTRY_EPOCH}.")
    L.append(f"Bars: **n≥{BAR_REBASELINE}** clean outcomes → C4 fusion re-baseline "
             f"(t_b53a2562); **n≥{BAR_RESEARCH}/arm** → NS-P4 entry gate.")
    L.append("")
    if stalled:
        L.append("## ALERT: FACTORY STALLED")
        L.append("")
        L.append(f"- `ALERT clean-cohort-accrual: {totals['signals']} clean-epoch signals accrued "
                 f"but 0 final clean-epoch outcomes in the last 24h — outcome factory idle "
                 f"(journey_finalizer last: {factory.get('finalizer_last') or 'n/a'}, "
                 f"trade_close labeler last: {factory.get('closer_last') or 'n/a'})`")
    else:
        L.append(f"## Status: accruing — {totals['outcomes']} clean outcomes, "
                 f"{totals['out24']} in last 24h")
    L.append("")
    L.append(f"Totals: **{totals['signals']} signals**, **{totals['outcomes']} final outcomes**, "
             f"**{totals['closes']} realized closes**, **{n_rlabels} R-multiple labels** on the clean cohort.")
    L.append("")

    L.append("## Heuristic Arms (timeframe x direction)")
    L.append("")
    L.append("| arm | signals | executed | final outcomes | closes | signals/day | "
             "pre-epoch close rate | proj. closes/day | ETA n≥100 | ETA n≥300 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda x: -x["n_signals"]):
        cr = "—" if r["close_rate"] is None else f"{100*r['close_rate']:.1f}%"
        pc = "—" if r["proj_closes_day"] is None else f"{r['proj_closes_day']:.0f}"
        def fmt_eta(eta, bar):
            if eta is None:
                return "—"
            if r["n_outcomes"] >= bar:
                return "**met**"
            return "<0.1d" if eta < 0.1 else f"{eta:.1f}d"
        e1 = fmt_eta(r["eta100_d"], BAR_REBASELINE)
        e3 = fmt_eta(r["eta300_d"], BAR_RESEARCH)
        L.append(f"| {r['arm']} | {r['n_signals']} | {r['n_executed']} | {r['n_outcomes']} | "
                 f"{r['n_closes']} | {r['rate_day']:.0f} | {cr} | {pc} | {e1} | {e3} |")
    L.append("")

    L.append("## Strategy & Seeded Baseline Arms")
    L.append("")
    L.append("Tracks the accrual and statistical baselines for registered strategy and control/injector arms "
             "by their unique `bandit_arm_id` or `strategy_name`. Expectancy and confidence intervals (CI) "
             "are computed on the `canonical_reward` (in R-units) of clean-epoch outcomes.")
    L.append("")
    L.append("**Baseline-arm note:** the NS-P3.1 null-control arm `random_entry_paper_control_v1` is a "
             "PAPER control that opens via `executeIntent` directly and therefore never produces a "
             "`signal_journeys` row; its `decision_outcomes` legitimately carry `contamination_reason = "
             "missing_journey_lineage`. That flag is an EXPECTED control artifact, not a data defect, so "
             "the baseline arm is reported here **regardless of the contamination flag** (the heuristic "
             "timeframe×direction view above retains the uncontaminated clean-cohort filter). Its "
             "expectancy/CI is the published NS-P3 random-entry null baseline.")
    L.append("")
    L.append("| arm name | positions | closed | final outcomes | wins | losses | outcomes/day | expectancy (95% CI) | ETA n≥100 | ETA n≥300 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(strat_rows, key=lambda x: -x["n_outcomes"]):
        out_day_rate = (r["n_outcomes"] / epoch_hours * 24.0) if epoch_hours > 0 else 0.0
        ci_str = fmt_expectancy_ci(r["expectancy"], r["ci_half"])

        def fmt_eta(eta, bar):
            if eta is None:
                return "—"
            if r["n_outcomes"] >= bar:
                return "**met**"
            return "<0.1d" if eta < 0.1 else f"{eta:.1f}d"

        rate_day = out_day_rate
        projectable = r["n_outcomes"] >= 2 and rate_day > 0
        eta100 = max(0.0, (BAR_REBASELINE - r["n_outcomes"]) / rate_day) if projectable else None
        eta300 = max(0.0, (BAR_RESEARCH - r["n_outcomes"]) / rate_day) if projectable else None

        e1 = fmt_eta(eta100, BAR_REBASELINE)
        e3 = fmt_eta(eta300, BAR_RESEARCH)
        arm_label = r["arm_name"]
        if r.get("any_contaminated"):
            arm_label = f"{arm_label} ⚠"
        L.append(f"| {arm_label} | {r['n_positions']} | {r['n_closed']} | {r['n_outcomes']} | "
                 f"{r['n_wins']} | {r['n_losses']} | {out_day_rate:.1f} | {ci_str} | {e1} | {e3} |")
    L.append("")
    L.append("⚠ = arm includes rows flagged `contaminated` (expected for the NS-P3.1 control: "
             "`missing_journey_lineage`). Expectancy/CI still valid as the published null baseline.")
    L.append("")

    L.append("ETA basis: observed clean-epoch signal rate (outcomes trail signals by the label "
             "horizon; journey_finalizer throughput historically exceeded signal inflow, so signal "
             "rate binds). **ETAs are CONDITIONAL on the outcome factory running** — while the "
             "stall alert above is firing, real ETA = stall duration + these numbers + ~24h label lag. "
             "Pre-epoch close rate = realized closes / signals per arm over "
             f"{PRE_EPOCH_LO[:10]} → {PRE_EPOCH_HI[:16]}Z. Reconciliation: counts come from "
             "`signal_journeys` / `decision_outcomes` (is_final, uncontaminated — the canonical-view "
             "filter) / `trade_close_events` / `r_multiple_labels` directly.")
    L.append("")
    L.append("Known lane caveats: 1m arms have never been 24h-labeled (NS-P3.2 limitation — "
             "R-multiple labeler covers ~10 majors on 1m candles); `clean_outcome_binary_24h` "
             "journey-label lane dead since 07-01 (register GAP #3).")
    L.append("")
    L.append("Consumers: weekly north-star sweep · NS-P4 entry-gate decisions · Frank digest · "
             "discord #critical-alerts on non-zero exit (via registered cron). "
             "SLO context: [[data-surface-register]].")
    L.append("")
    return "\n".join(L)


# ----------------------------------------------------------------------------
# Self-test — synthetic data proving ETA math + stall alert
# ----------------------------------------------------------------------------
def self_test():
    arms = [
        {"timeframe": "5m", "direction": "LONG", "n_signals": "300", "n_executed": "0", "epoch_hours": "24.0"},
        {"timeframe": "1h", "direction": "SHORT", "n_signals": "2", "n_executed": "0", "epoch_hours": "24.0"},
    ]
    pre = [{"timeframe": "5m", "direction": "LONG", "n_signals": "1000", "n_closes": "25"}]

    # Case 1: stalled factory (signals, zero outcomes in 24h) must alert
    rows, totals, stalled, strat_rows = compute(arms, [], [], pre, [], [], 24.0)
    r5m = next(r for r in rows if r["arm"] == "5m LONG")
    ok1 = stalled is True
    # Case 2: ETA math — 300 signals/24h -> 300/day; ETA100 = 100/300 d, ETA300 = 1.0 d
    ok2 = abs(r5m["rate_day"] - 300.0) < 1e-6 and abs(r5m["eta100_d"] - 1/3) < 1e-6 \
        and abs(r5m["eta300_d"] - 1.0) < 1e-6
    # Case 3: pre-epoch close-rate projection: 2.5% of 300/day = 7.5 closes/day
    ok3 = abs(r5m["close_rate"] - 0.025) < 1e-9 and abs(r5m["proj_closes_day"] - 7.5) < 1e-6
    # Case 4: tiny arm (n<MIN_ARM_SIGNALS) listed but not projected
    r1h = next(r for r in rows if r["arm"] == "1h SHORT")
    ok4 = r1h["eta100_d"] is None and r1h["eta300_d"] is None
    # Case 5: healthy factory (fresh outcomes in last 24h) must NOT alert; met-bar detection
    outs = [{"timeframe": "5m", "direction": "LONG", "n_outcomes": "150", "n_last24h": "150"}]
    rows2, _totals2, stalled2, strat_rows2 = compute(arms, outs, [], pre, [], [], 24.0)
    r5m2 = next(r for r in rows2 if r["arm"] == "5m LONG")
    ok5 = stalled2 is False and r5m2["n_outcomes"] == 150 \
        and abs(r5m2["eta300_d"] - 0.5) < 1e-6  # (300-150)/300 = 0.5d

    # Case 6: default control arm always present
    ok6 = any(r["arm_name"] == "random_entry_paper_control_v1" for r in strat_rows)

    # Case 7: control-arm outcomes are NOT dropped by the contamination filter.
    # Simulate 13 contaminated control outcomes (all missing_journey_lineage): the
    # report MUST surface them (n_outcomes == 13), not zero.
    ctrl_outs = [{
        "arm_name": "random_entry_paper_control_v1",
        "n_outcomes": "13", "n_last24h": "3",
        "avg_reward": "-0.40", "stddev_reward": "1.10",
        "n_wins": "5", "n_losses": "8",
        "any_contaminated": "t", "contamination_reason": "missing_journey_lineage",
    }]
    _r, _t, _s, strat_rows7 = compute(arms, [], [], pre, [], ctrl_outs, 24.0)
    ctrl = next(r for r in strat_rows7 if r["arm_name"] == "random_entry_paper_control_v1")
    ok7 = ctrl["n_outcomes"] == 13 and ctrl["n_wins"] == 5 and ctrl["n_losses"] == 8 \
        and ctrl["any_contaminated"] is True and ctrl["expectancy"] is not None

    results = [
        ("stalled factory fires alert", ok1),
        ("ETA math at signal rate", ok2),
        ("pre-epoch close-rate projection", ok3),
        ("low-sample arm not projected", ok4),
        ("healthy factory silent; remaining-ETA uses accrued n", ok5),
        ("default control arm present in strat_rows", ok6),
        ("control-arm outcomes survive contamination filter", ok7),
    ]
    all_ok = all(ok for _, ok in results)
    for name, ok in results:
        print(f"SELF-TEST {'PASS' if ok else 'FAIL'}: {name}")
    print(f"SELF-TEST {'PASS' if all_ok else 'FAIL'}: overall")
    return 0 if all_ok else 1


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="NS-P3.5 clean-cohort accrual dashboard")
    ap.add_argument("--self-test", action="store_true",
                    help="run ETA/stall-alert self-test on synthetic data (no DB, no report write)")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    now_utc = datetime.now(timezone.utc)
    try:
        d = fetch()
    except Exception as e:
        print(f"ERROR clean-cohort accrual: {e}", file=sys.stderr)
        sys.exit(1)

    epoch_start_dt = datetime.fromisoformat(EPOCH_START.replace("+00", "+00:00"))
    epoch_hours = (now_utc - epoch_start_dt).total_seconds() / 3600.0

    rows, totals, stalled, strat_rows = compute(
        d["arms"],
        d["clean_outcomes"],
        d["clean_closes"],
        d["pre_ratio"],
        d["strategy_positions"],
        d["strategy_outcomes"],
        epoch_hours
    )
    n_rlabels = int(d["r_labels"][0]["n"]) if d["r_labels"] else 0
    factory = d["factory"][0] if d["factory"] else {}
    report = render(rows, totals, stalled, n_rlabels, factory, now_utc, strat_rows, epoch_hours)

    report_date = f"{now_utc:%Y-%m-%d}"
    report_path = REPORT_DIR / f"{report_date}.md"
    write_markdown_atomic(
        report_path,
        report,
        title=f"Clean-cohort accrual — n per arm vs research bars — {report_date}",
        type="task-evidence",
        status="active",
        created=report_date,
        updated=report_date,
        confidence="high",
        tags=["sycode", "clean-cohort", "accrual", "north-star"],
        sources=[
            "sycodetrading-supabase-db:signal_journeys",
            "sycodetrading-supabase-db:decision_outcomes",
            "sycodetrading-supabase-db:managed_positions",
        ],
        project="sycode-trading",
        owners=["sycode-trading-pm"],
        knowledge_tier="evidence",
        generated=True,
        generator="sycode_clean_cohort_accrual.py",
    )
    print(f"report written: {report_path}")

    if stalled:
        print(f"ALERT clean-cohort-accrual: {totals['signals']} clean-epoch signals accrued "
              f"but 0 final clean-epoch outcomes in the last 24h — outcome factory idle")
        sys.exit(2)
    print(f"OK: {totals['outcomes']} clean outcomes accrued ({totals['out24']} in last 24h) "
          f"across {len(rows)} arms")
    sys.exit(0)


if __name__ == "__main__":
    main()
