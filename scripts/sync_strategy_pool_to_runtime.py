#!/usr/bin/env python3
"""
Strategy Pool -> Runtime Strategies Sync Pipeline

IDEMPOTENT: Only inserts missing strategies. Safe to run on any schedule.
SAFETY: Paper-only. Never modifies live trading state.

Maps strategy_pool entries (status=paper, no runtime row) to the `strategies`
table that the trading server's SignalProcessor -> StrategyEngine -> TradeIntent
pipeline consumes.

Schema mapping:
  strategy_pool.entry_rules.direction    -> strategies.signal_filter.directions
  strategy_pool.entry_rules.timeframe   -> strategies.signal_filter.timeframes
  strategy_pool.entry_rules.regime      -> strategies.signal_filter.tags
  strategy_pool.entry_rules.confluence_min -> strategies.signal_filter.minConfidence
  strategy_pool.exit_rules              -> strategies.exit_guidelines
  strategy_pool.risk_per_trade          -> strategies.risk_profile.maxPortfolioRiskPct
  strategy_pool.meta                    -> strategies.meta (includes strategyPoolId)

Usage:
  python3 sync_strategy_pool_to_runtime.py              # dry-run (default)
  python3 sync_strategy_pool_to_runtime.py --apply       # actually insert
  python3 sync_strategy_pool_to_runtime.py --apply --cron  # insert + log for cron
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

ENGINE_MAP = [
    ("TrendFollower", "trend_follower"),
    ("MeanReversion", "mean_reverter"),
    ("BreakoutTrader", "breakout_hunter"),
    ("ScalpTrader", "scalper"),
    ("CalibratedAdvisor", "custom"),
    ("DuckDB_", "custom"),
    ("Pattern_", "pattern_recognizer"),
    ("VolSurge_", "custom"),
    ("FundingCarry", "custom"),
]

SYSTEM_USER_ID = "00000000-0000-0000-0000-000000000000"
PG_CONTAINER = "sycodetrading-supabase-db"


def psql(query, db="postgres"):
    cmd = [
        "docker", "exec", "-i", PG_CONTAINER,
        "psql", "-U", "postgres", "-d", db,
        "-t", "-A", "-F", "\t",
        "-c", query,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"psql error: {result.stderr.strip()}")

    rows = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            parts = line.split("\t")
            rows.append({f"col_{i}": p for i, p in enumerate(parts)})
    return rows


def json_psql(query, db="postgres"):
    clean = query.strip().rstrip(";")
    return psql(f"SELECT row_to_json(t) FROM ({clean}) t;", db)


def infer_engine(name):
    for pat, eng in ENGINE_MAP:
        if name.startswith(pat):
            return eng
    return "custom"


def parse_json(val):
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def build_signal_filter(row):
    er = parse_json(row.get("entry_rules"))
    fd = {"minConfidence": er.get("confluence_min", 40)}

    if er.get("direction"):
        fd["directions"] = [er["direction"].lower()]
    if er.get("timeframe"):
        fd["timeframes"] = [er["timeframe"]]
    elif er.get("timeframes") and isinstance(er["timeframes"], list):
        fd["timeframes"] = er["timeframes"]

    tags = []
    if er.get("regime"):
        tags.append(er["regime"].upper())
    if er.get("indicators") and isinstance(er["indicators"], list):
        for i in er["indicators"]:
            if i not in tags:
                tags.append(i)
    if er.get("required_patterns") and isinstance(er["required_patterns"], list):
        for p in er["required_patterns"]:
            if p not in tags:
                tags.append(p)
    if er.get("volumeZ20_min") is not None:
        tags.append("VOLUME_SURGE")

    pv = row.get("preferred_volatility")
    if pv and isinstance(pv, list):
        for v in pv:
            t = f"VOL_{v}"
            if t not in tags:
                tags.append(t)

    if tags:
        fd["tags"] = tags
    return fd


def build_risk_profile(row):
    rp = row.get("risk_per_trade")
    val = round(float(rp), 2) if rp else 2.0
    return {"maxPortfolioRiskPct": val, "maxLeverage": 3,
            "maxConcurrentPositions": 3, "preferHedgeMode": False}


def build_exit_guidelines(row):
    er = parse_json(row.get("exit_rules"))
    if not er:
        return {}

    g = {}
    stop = er.get("stop_atr") or er.get("stop_loss")
    if stop is not None:
        v = round(float(stop), 2)
        g["stopLossType"] = "fixed" if v <= 1.0 else "atr"
        g["atrMultiple"] = v

    tp = er.get("target_atr") or er.get("take_profit")
    if tp is not None:
        v = round(float(tp), 2)
        g["takeProfitTargets"] = [
            {"rr": round(v * 0.67, 4), "percentToClose": 50},
            {"rr": v, "percentToClose": 100},
        ]

    bth = er.get("bars_to_hold")
    if bth is not None:
        er2 = parse_json(row.get("entry_rules"))
        tf = er2.get("timeframe", "15m") if er2 else "15m"
        mpb = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
        g["maxHoldTimeMinutes"] = int(bth) * mpb.get(tf, 15)

    pa = er.get("partial_exit_at")
    if pa is not None:
        g["partialProfitConfig"] = {
            "enabled": True,
            "tiers": [{"closePct": int(er.get("partial_exit_pct", 25)),
                       "profitPct": round(float(pa), 2)}],
        }
    return g


def build_meta(row):
    return {"strategyPoolId": row["id"], "strategyPoolName": row["name"],
            "syncedAt": datetime.now(timezone.utc).isoformat(),
            "syncedBy": "strategy_pool_sync_pipeline",
            "sourceCreator": row.get("creator", "system")}


def find_missing():
    q = """
    SELECT sp.id, sp.name, sp.description, sp.entry_rules, sp.exit_rules,
           sp.position_sizing, sp.risk_per_trade, sp.preferred_volatility,
           sp.preferred_timeframes, sp.creator, sp.status, sp.tier
    FROM public.strategy_pool sp
    WHERE sp.status = 'paper'
      AND sp.id NOT IN (
        SELECT sp2.id FROM public.strategy_pool sp2
        INNER JOIN public.strategies s ON s.name = sp2.name
      )
    ORDER BY sp.id
    """
    return json_psql(q)


def build_row(row):
    sf = build_signal_filter(row)
    rp = build_risk_profile(row)
    eg = build_exit_guidelines(row)
    mt = build_meta(row)
    desc = (row.get("description") or "")[:500]
    return {
        "user_id": SYSTEM_USER_ID,
        "name": row["name"],
        "description": desc,
        "engine": infer_engine(row["name"]),
        "enabled": True,
        "trading_mode": "paper",
        "signal_filter": json.dumps(sf),
        "risk_profile": json.dumps(rp),
        "exit_guidelines": json.dumps(eg),
        "meta": json.dumps(mt),
    }


def preview(rows):
    if not rows:
        print("No missing paper strategies found. Everything is in sync.")
        return
    print(f"Would sync {len(rows)} strategy_pool entries to strategies table:\n")
    for r in rows:
        sr = build_row(r)
        print(f"  [{r['id']:>2}] {sr['name']}")
        print(f"       engine:  {sr['engine']}")
        print(f"       filter:  {sr['signal_filter']}")
        print(f"       risk:    {sr['risk_profile']}")
        if sr['exit_guidelines'] != '{}':
            print(f"       exit:    {sr['exit_guidelines']}")
    print(f"\nRun with --apply to insert these {len(rows)} strategies")


def q(s):
    if s is None:
        return "NULL"
    return "'" + s.replace("'", "''").replace("\\", "\\\\") + "'"


def apply_sync(rows, cron=False):
    if not rows:
        if not cron:
            print("No missing paper strategies found. Everything is in sync.")
        return 0
    inserted = 0
    errors = 0
    for r in rows:
        sr = build_row(r)
        sql = f"""
        INSERT INTO public.strategies
          (user_id, name, description, engine, enabled, trading_mode,
           signal_filter, risk_profile, exit_guidelines, meta,
           total_trades, winning_trades, total_pnl)
        VALUES
          ('{sr["user_id"]}', {q(sr["name"])}, {q(sr["description"])},
           {q(sr["engine"])}, true, 'paper',
           '{sr["signal_filter"]}', '{sr["risk_profile"]}',
           '{sr["exit_guidelines"]}', '{sr["meta"]}',
           0, 0, '0.00000000')
        ON CONFLICT (user_id, name) DO NOTHING
        RETURNING id;
        """
        try:
            res = psql(sql)
            rid = res[0].get("col_0", "").strip() if res else ""
            if rid:
                inserted += 1
                if not cron:
                    print(f"  + {sr['name']}  [id={rid[:8]}...]")
            else:
                if not cron:
                    print(f"  ~ {sr['name']} (already exists, skipped)")
        except Exception as e:
            errors += 1
            if not cron:
                print(f"  ! {sr['name']}: {e}")
    if not cron:
        print(f"\n{'=' * 50}")
        print(f"Synced: {inserted}  |  Errors: {errors}  |  Total: {len(rows)}")
        print(f"{'=' * 50}")
    return inserted


def main():
    parser = argparse.ArgumentParser(description="Sync strategy_pool -> runtime strategies")
    parser.add_argument("--apply", action="store_true", help="Insert missing strategies")
    parser.add_argument("--cron", action="store_true", help="Quiet for cron")
    args = parser.parse_args()
    try:
        rows = find_missing()
        if args.apply:
            n = apply_sync(rows, cron=args.cron)
            if args.cron and n > 0:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                print(f"[{ts}] strategy_pool sync: {n} new strategies registered")
        else:
            preview(rows)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
