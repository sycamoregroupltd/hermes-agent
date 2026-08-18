#!/usr/bin/env python3
"""HEAD-faithful cohort probe: add cost_fidelity='measured' AND net_pnl_usd IS NOT NULL
to the v_ledger_reward join (matches HEAD execution/fusion_calibration_report_v2.py).
Compare 30d vs 60d to validate the research card's 456/504 claim.
"""
import os, subprocess, sys
PGHOST=os.environ.get("PGHOST","localhost"); PGPORT=os.environ.get("PGPORT","5432")
PGUSER=os.environ.get("PGUSER","postgres"); PGDB=os.environ.get("PGDB","postgres")
PGPASS=os.environ.get("PGPASSWORD","postgres")
INTERIM_LANES=("interim_1h","interim_4h")
def run_sql(sql, timeout=180):
    env=os.environ.copy(); env["PGPASSWORD"]=PGPASS
    r=subprocess.run(["psql","-h",PGHOST,"-p",PGPORT,"-U",PGUSER,"-d",PGDB,"-X","-A","-F","|","--pset","footer=off","-c",sql],
                     capture_output=True,text=True,timeout=timeout,env=env)
    if r.returncode!=0:
        print(f"SQL ERROR rc={r.returncode}: {r.stderr.strip()[:500]}",file=sys.stderr); return []
    lines=[l.strip() for l in r.stdout.strip().split("\n") if l.strip()]
    if len(lines)<2: return []
    cols=[c.strip() for c in lines[0].split("|")]
    return [dict(zip(cols,[v.strip() for v in l.split("|")])) for l in lines[1:]]
resolved_at="COALESCE(d.finalized_at, d.decided_at, d.created_at)"
clean_pred=("d.contaminated = false AND d.is_counterfactual = false "
            "AND d.label_source NOT IN ('interim_1h','interim_4h') AND abs(d.pnl_percent::numeric) <= 1000")
epoch=run_sql("SELECT starts_at::text AS s FROM data_epoch_registry WHERE name='clean-candidate-599f58e7e' LIMIT 1;",timeout=30)
epoch_start=epoch[0]["s"] if epoch else None
print(f"epoch_start={epoch_start}")
for win in ("30","60"):
    window_pred=f"{resolved_at} >= now() - interval '{win} days'"
    sql=f"""
      SELECT COUNT(*) AS clean_n,
        COUNT(*) FILTER (WHERE ledger_matched) AS net_scored,
        COUNT(*) FILTER (WHERE ledger_matched AND ledger_is_win) AS net_wins,
        COUNT(*) FILTER (WHERE ledger_matched AND NOT ledger_is_win) AS net_losses,
        COUNT(*) FILTER (WHERE is_win) AS gross_wins
      FROM (
        SELECT DISTINCT ON (sj.id)
            sj.id AS journey_id,
            d.is_win,
            (lr.position_id IS NOT NULL) AS ledger_matched,
            lr.is_win AS ledger_is_win
        FROM signal_journeys sj
        JOIN decision_outcomes d ON d.journey_id = sj.id
        LEFT JOIN trade_setups ts ON ts.signal_id = sj.id::text
        LEFT JOIN v_ledger_reward lr
            ON (lr.correlation_id = sj.correlation_id OR lr.signal_id = sj.signal_id)
            AND lr.cost_fidelity = 'measured' AND lr.net_pnl_usd IS NOT NULL
        WHERE d.outcome_class IN ('WIN','LOSS') AND {clean_pred} AND {window_pred} AND sj.triggered_at >= '{epoch_start}'
        ORDER BY sj.id, d.is_final DESC, {resolved_at} DESC, (ts.signal_id IS NOT NULL) DESC, ts.generated_at DESC
      ) d;
    """
    rows=run_sql(sql,timeout=180)
    n=rows[0] if rows else {}
    if n:
        nn=int(n["clean_n"]); ns=int(n["net_scored"]); nw=int(n["net_wins"])
        print(f"--- HEAD-strict window {win}d ---  clean={nn}  net_scored={ns}  net_wins={nw}  "
              f"net_wr={(nw/ns*100 if ns else 0):.2f}%  gross_wins={n['gross_wins']}  "
              f"gross_wr={(int(n['gross_wins'])/nn*100 if nn else 0):.2f}%")
