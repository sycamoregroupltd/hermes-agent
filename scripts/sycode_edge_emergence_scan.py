#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See goal-orchestrator-operating-runbook (canonical-copy rule).
# INVOKER: weekly Hermes cron 'sycode-edge-emergence-scan' (install card: edge-emergence-watchdog-20260707).
#          Also runnable manually: python3 ~/.hermes/scripts/sycode_edge_emergence_scan.py
#
# WHY: 2026-07-07 two exhaustive edge hunts + a combined-filter test found NO net-of-cost edge in ANY current
# feature (baseline -0.38R, everything reverts OOS). Conclusion: no edge in the CURRENT data — but the clean-label
# corpus GROWS with signal generation. This watchdog re-runs the core edge-slice scan on the (widening) corpus and
# ALERTS if any feature slice EVER crosses into positive net R with a 95% CI excluding 0 that holds out-of-sample —
# i.e. a candidate edge emerging as more data accrues. It is read-only; it makes NO trading decision. A human/reviewer
# validates any candidate (the founding lesson: every past 'edge' was a leak artifact, so a hit here is a LEAD, not a trade).
import subprocess, json, sys, statistics, math

DB_CONTAINER = "sycodetrading-supabase-db"
BRACKET = "s1_t1_v1"
EPOCH = "2026-07-05"
MIN_N = 300
FEATURES = [  # (column, human)  — decile-sliced; direction-aware handled separately
    ("market_oi_delta_percent", "oi_delta"),
    ("market_funding_rate", "funding"),
    ("market_top_trader_ls_ratio", "top_trader_ls"),
    ("direction_quality_prob", "dq_prob"),
    ("composite_confidence_calibrated_p_win", "composite_conf"),
    ("conviction_probability", "conviction"),
    ("directional_conviction", "directional_conviction"),
]

def q(sql):
    p = subprocess.run(["docker","exec","-e","PGOPTIONS=-c default_transaction_read_only=on",
                        DB_CONTAINER,"psql","-U","postgres","-d","postgres","-tAqc",sql],
                       capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError(f"psql rc={p.returncode}: {p.stderr.strip()[:300]}")
    return [r for r in p.stdout.strip().splitlines() if r]

def ci95(mean, sd, n):
    if n < 2 or sd is None: return (None, None)
    se = sd / math.sqrt(n)
    return (mean - 1.96*se, mean + 1.96*se)

def scan():
    base = q(f"""SELECT count(*), avg(r_achieved), stddev(r_achieved)
                 FROM r_multiple_labels l JOIN signal_journeys sj ON sj.id=l.journey_id
                 WHERE l.bracket_config_id='{BRACKET}' AND l.contaminated IS NOT TRUE
                   AND l.r_achieved IS NOT NULL AND sj.triggered_at>'{EPOCH}'""")[0].split("|")
    n0, m0 = int(base[0]), float(base[1] or 0)
    candidates = []
    split = q(f"""SELECT to_timestamp(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch from triggered_at)))
                  FROM signal_journeys WHERE triggered_at>'{EPOCH}'""")[0]
    for col, name in FEATURES:
        # top & bottom decile x (all / OOS), direction-aware
        rows = q(f"""
          WITH b AS (SELECT l.r_achieved r, upper(sj.direction) dir, sj.{col} f,
                            ntile(10) OVER (ORDER BY sj.{col}) nt,
                            (sj.triggered_at > '{split}'::timestamptz) oos
                     FROM r_multiple_labels l JOIN signal_journeys sj ON sj.id=l.journey_id
                     WHERE l.bracket_config_id='{BRACKET}' AND l.contaminated IS NOT TRUE
                       AND l.r_achieved IS NOT NULL AND sj.triggered_at>'{EPOCH}' AND sj.{col} IS NOT NULL)
          SELECT nt, dir, oos, count(*), avg(r), stddev(r) FROM b GROUP BY 1,2,3 HAVING count(*)>={MIN_N//4}""")
        agg = {}
        for row in rows:
            nt, dir_, oos, n, mean, sd = row.split("|")
            key = (int(nt), dir_)
            agg.setdefault(key, {})[oos=="t"] = (int(n), float(mean or 0), float(sd or 0))
        for (nt, dir_), halves in agg.items():
            full_n = sum(h[0] for h in halves.values())
            if full_n < MIN_N: continue
            full_mean = sum(h[0]*h[1] for h in halves.values())/full_n
            # pooled sd approx
            full_sd = statistics.mean([h[2] for h in halves.values()]) if halves else 0
            lo, hi = ci95(full_mean, full_sd, full_n)
            oos = halves.get(True); is_ = halves.get(False)
            # CANDIDATE (strict, to avoid single-half / leak artifacts): full CI excludes 0 on the POSITIVE side,
            # AND BOTH halves are present with adequate n AND BOTH have positive mean (persistence across the time split).
            HALF_MIN = MIN_N // 3
            if (lo is not None and lo > 0
                    and is_ and oos and is_[0] >= HALF_MIN and oos[0] >= HALF_MIN
                    and is_[1] > 0 and oos[1] > 0):
                candidates.append(dict(feature=name, decile=nt, direction=dir_, n=full_n,
                                       mean_r=round(full_mean,3), ci_lo=round(lo,3),
                                       is_mean=round(is_[1],3), oos_mean=round(oos[1],3)))
    return dict(baseline_n=n0, baseline_mean_r=round(m0,3), candidates=candidates)

if __name__ == "__main__":
    try:
        res = scan()
    except Exception as e:
        print(f"EDGE-SCAN ERROR: {e}"); sys.exit(1)
    print(json.dumps(res, indent=1))
    if res["candidates"]:
        import datetime
        day = datetime.date.today().isoformat()
        summary = "{} slice(s) crossed into positive net R (full CI>0, both IS+OOS halves >0).".format(len(res["candidates"]))
        details = json.dumps(res["candidates"], indent=1)
        feats = ", ".join("{}[d{}/{}] {}R(OOS {})".format(c["feature"], c["decile"], c["direction"], c["mean_r"], c["oos_mean"]) for c in res["candidates"][:5])
        print("\n🚨 EDGE-EMERGENCE ALERT: {} VALIDATE adversarially (leak/OOS/multiple-testing) — a LEAD, not a trade.".format(summary))
        # CONSUMER 1 — Telegram alert to Frank (alerts only, no secrets, no trade)
        try:
            subprocess.run(["hermes","send","-t","telegram","-m",
                "🚨 Sycode EDGE-EMERGENCE LEAD ({}): {} {} — LEAD not a trade; carded for adversarial validation.".format(day, summary, feats)],
                timeout=30, capture_output=True, text=True)
        except Exception as e:
            print("(telegram alert failed: {})".format(e))
        # CONSUMER 2 — validation card (idempotency-keyed by day → no weekly re-spam of a persistent lead)
        try:
            subprocess.run(["hermes","kanban","--board","sycode-trading","create",
                "EDGE-EMERGENCE LEAD {}: {} candidate slice(s) — adversarial validation required".format(day, len(res["candidates"])),
                "--assignee","trading-strategy-dev","--priority","2",
                "--idempotency-key","edge-emergence-lead-{}".format(day),
                "--body","Auto-raised by the sycode-edge-emergence-scan cron. LEAD not a trade (founding lesson: every past 'edge' was a leak artifact). VALIDATE adversarially before ANY trust: fresh OOS split, leak check (close-time backfill writers), multiple-testing correction across the scanned features x deciles x directions, confirm net-of-cost r_achieved, independent reviewer. Candidates:\n"+details],
                timeout=30, capture_output=True, text=True)
        except Exception as e:
            print("(card creation failed: {})".format(e))
        sys.exit(0)
    else:
        print(f"\nNo edge emerged (baseline {res['baseline_mean_r']}R, n={res['baseline_n']}). Corpus still edge-free — expected until new data surfaces (liquidations) land.")
        sys.exit(0)
