#!/usr/bin/env python3
"""Read-only cohort probe for t_053d6f6d: reproduce pinned fusion_calibration_report_v2.py
Section 1 dedup cohort counts for window=30d vs window=60d (Tier-1 realized-exit).
Mirrors the pinned report's exact predicates (PIN 5e7d0dc0). No DB writes.
"""
import os, subprocess, sys

PGHOST = os.environ.get("PGHOST", "localhost")
PGPORT = os.environ.get("PGPORT", "5432")
PGUSER = os.environ.get("PGUSER", "postgres")
PGDB = os.environ.get("PGDB", "postgres")
PGPASS = os.environ.get("PGPASSWORD", "postgres")
INTERIM_LANES = ("interim_1h", "interim_4h")

def run_sql(sql, timeout=180):
    env = os.environ.copy(); env["PGPASSWORD"] = PGPASS
    r = subprocess.run(["psql","-h",PGHOST,"-p",PGPORT,"-U",PGUSER,"-d",PGDB,
                        "-X","-A","-F","|","--pset","footer=off","-c",sql],
                       capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0:
        print(f"SQL ERROR rc={r.returncode}: {r.stderr.strip()[:500]}", file=sys.stderr)
        return []
    lines=[l.strip() for l in r.stdout.strip().split("\n") if l.strip()]
    if len(lines)<2: return []
    cols=[c.strip() for c in lines[0].split("|")]
    return [dict(zip(cols,[v.strip() for v in l.split("|")])) for l in lines[1:]]

resolved_at = "COALESCE(d.finalized_at, d.decided_at, d.created_at)"
clean_pred = ("d.contaminated = false AND d.is_counterfactual = false "
              "AND d.label_source NOT IN ('interim_1h','interim_4h') "
              "AND abs(d.pnl_percent::numeric) <= 1000")
epoch = run_sql("SELECT starts_at::text AS s FROM data_epoch_registry WHERE name='clean-candidate-599f58e7e' LIMIT 1;", timeout=30)
epoch_start = epoch[0]["s"] if epoch else None
if not epoch_start:
    print("NO EPOCH START FOUND"); sys.exit(1)
print(f"epoch_start={epoch_start}")

for win in ("30","60"):
    window_pred = f"{resolved_at} >= now() - interval '{win} days'"
    dedup_sql = f"""
      WITH dd AS (
        SELECT DISTINCT ON (sj.id)
            sj.id AS journey_id,
            COALESCE(ts.conviction_score::numeric, sj.composite_confidence_score::numeric) AS conviction_score,
            d.outcome_class, d.is_win, d.pnl_percent::numeric AS pnl_pct, d.label_source, d.is_final,
            {resolved_at} AS resolved_at,
            sj.correlation_id AS correlation_id, sj.signal_id AS raw_signal_id,
            (lr.position_id IS NOT NULL) AS ledger_matched, lr.is_win AS ledger_is_win
        FROM signal_journeys sj
        JOIN decision_outcomes d ON d.journey_id = sj.id
        LEFT JOIN trade_setups ts ON ts.signal_id = sj.id::text
        LEFT JOIN v_ledger_reward lr ON lr.correlation_id = sj.correlation_id OR lr.signal_id = sj.signal_id
        WHERE d.outcome_class IN ('WIN','LOSS') AND {clean_pred} AND {window_pred} AND sj.triggered_at >= '{epoch_start}'
        ORDER BY sj.id, d.is_final DESC, {resolved_at} DESC, (ts.signal_id IS NOT NULL) DESC, ts.generated_at DESC
      )
      SELECT
        COUNT(*) AS clean_n,
        COUNT(*) FILTER (WHERE ledger_matched) AS net_scored,
        COUNT(*) FILTER (WHERE ledger_matched AND ledger_is_win) AS net_wins,
        COUNT(*) FILTER (WHERE ledger_matched AND NOT ledger_is_win) AS net_losses,
        COUNT(*) FILTER (WHERE is_win) AS gross_wins
      FROM dd
    """
    rows = run_sql(dedup_sql, timeout=180)
    n = rows[0] if rows else {}
    if n:
        nn = int(n["clean_n"]); ns = int(n["net_scored"])
        nw = int(n["net_wins"])
        print(f"--- window {win}d ---  clean={nn}  net_scored={ns}  net_wins={nw}  "
              f"net_wr={(nw/ns*100 if ns else 0):.2f}%  gross_wins={n['gross_wins']}  "
              f"gross_wr={(int(n['gross_wins'])/nn*100 if nn else 0):.2f}%")
