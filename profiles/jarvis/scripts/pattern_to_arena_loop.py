#!/usr/bin/env python3
"""
pattern_to_arena_loop.py — Mine → filter → fine-tune → arena candidate packets.

Doctrine (quant-research-operations + trading-data-analysis):
- Full signal_journeys population, synthetic next-open labels
- Direction-adjusted, clip ±10%, 16bps RT net, exclude AXLUSDT
- Freshness gate + chronological OOS half-split
- Paper-only: NO strategy_pool mutation, NO trade_intents, NO live trading

Outputs:
- /home/frank/sycode-mining-outputs/pattern-to-arena/<date>/results.json
- candidate JSON packets under same dir
- Markdown candidate packets staged under /tmp then copied to vault by agent
- arena_prep_manifest.json for kanban card bodies

Exit codes: 0 ok, 2 no candidates, 1 error
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

import duckdb
import numpy as np
import polars as pl

warnings.filterwarnings("ignore")

FEE = 0.16  # % points RT
CLIP = 10.0
FRESH = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1m": 1, "1d": 1440, "1D": 1440}
TFS = ["5m", "15m", "1h", "4h"]
# hard gates for arena-prep (still paper; not promotion)
GATE = {
    "min_n_train": 35,
    "min_n_test": 25,  # paper-arena prep floor; promotion still requires n>=300 fresh
    "min_wr_test": 0.45,
    "min_mean_net_test": 0.0,
    "min_fresh": 0.90,
    "max_stale_share": 0.08,  # train can be noisier; test still freshness-checked
}


def utcnow():
    return datetime.now(timezone.utc)


def slugify(parts: list) -> str:
    s = "_".join(str(p).lower().replace(" ", "-") for p in parts if p is not None)
    s = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in s)
    return s[:80].strip("-_")


def attach():
    con = duckdb.connect()
    con.execute("INSTALL postgres; LOAD postgres;")
    # NO SCHEMA public — binder trap
    # Credential is externalized at runtime (mirrors quant_researcher_6h.py): never
    # a literal DSN in source. PGPASSWORD env overrides; default is the local
    # dev-docker postgres password so the cron path needs no extra config.
    pgpw = os.environ.get("PGPASSWORD", "postgres")
    dsn = f"postgresql://postgres:{pgpw}@127.0.0.1:5432/postgres"
    con.execute(f"ATTACH '{dsn}' AS pg (TYPE POSTGRES);")
    con.execute("SET threads=4; SET memory_limit='10GB'; SET preserve_insertion_order=false;")
    return con


def load_signals(con, days: int) -> pl.DataFrame:
    return con.execute(
        f"""
        SELECT id, symbol, direction, timeframe, triggered_at, entry_price,
               regime_volatility, trading_session, macro_regime,
               TRY_CAST(COALESCE(indicators->>'rsi', indicators->>'rsi14') AS DOUBLE) AS rsi,
               TRY_CAST(indicators->>'williamsR' AS DOUBLE) AS williams_r,
               TRY_CAST(indicators->>'williamsR2' AS DOUBLE) AS williams_r2,
               TRY_CAST(indicators->>'adx' AS DOUBLE) AS adx,
               TRY_CAST(indicators->>'volumeZ20' AS DOUBLE) AS volume_z,
               TRY_CAST(indicators->>'meanReversionScore' AS DOUBLE) AS mr_score,
               TRY_CAST(indicators->>'momentumScore' AS DOUBLE) AS mom_score
        FROM pg.public.signal_journeys
        WHERE entry_price > 0
          AND symbol <> 'AXLUSDT'
          AND triggered_at >= NOW() - INTERVAL '{days} days'
          AND timeframe IN ('5m','15m','1h','4h')
        """
    ).pl()


def load_candles(con, tf: str, days: int) -> pl.DataFrame:
    return con.execute(
        f"""
        SELECT symbol, timestamp AS candle_time, open, close
        FROM pg.public.candles
        WHERE timeframe = '{tf}'
          AND timestamp >= NOW() - INTERVAL '{days + 10} days'
        """
    ).pl()


def label_tf(sig: pl.DataFrame, cans: pl.DataFrame, tf: str) -> pl.DataFrame:
    if sig.height == 0:
        return sig
    c = cans.sort(["symbol", "candle_time"])
    s = sig.sort("triggered_at")
    nxt = s.join_asof(
        c.select(["symbol", "candle_time", "open"]).rename(
            {"open": "entry_open", "candle_time": "fill_time"}
        ),
        by="symbol",
        left_on="triggered_at",
        right_on="fill_time",
        strategy="forward",
    )
    c2 = c.select(["symbol", "candle_time", "close"]).rename(
        {"candle_time": "exit_time", "close": "exit_close"}
    )
    out = nxt.sort("fill_time").join_asof(
        c2.sort("exit_time"),
        by="symbol",
        left_on="fill_time",
        right_on="exit_time",
        strategy="forward",
    )
    out = out.filter(pl.col("entry_open").is_not_null() & pl.col("exit_close").is_not_null())
    out = out.with_columns(
        [
            ((pl.col("exit_close") - pl.col("entry_open")) / pl.col("entry_open") * 100).alias(
                "fwd"
            ),
            ((pl.col("fill_time") - pl.col("triggered_at")).dt.total_minutes()).alias("lag_min"),
        ]
    )
    out = out.with_columns(
        pl.when(pl.col("direction") == "SHORT")
        .then(-pl.col("fwd"))
        .otherwise(pl.col("fwd"))
        .clip(-CLIP, CLIP)
        .alias("fwd_dir")
    )
    out = out.with_columns((pl.col("fwd_dir") - FEE).alias("net"))
    out = out.with_columns(
        [
            pl.when(pl.col("net") > 0.2)
            .then(1)
            .when(pl.col("net") < -0.2)
            .then(0)
            .otherwise(-1)
            .alias("label"),
            (pl.col("lag_min") <= FRESH.get(tf, 60)).alias("fresh"),
            pl.lit(tf).alias("tf"),
        ]
    )
    return out


def add_buckets(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        [
            pl.when(pl.col("volume_z").is_null())
            .then(pl.lit("z_na"))
            .when(pl.col("volume_z") > 3)
            .then(pl.lit("z>3"))
            .when(pl.col("volume_z") > 2)
            .then(pl.lit("z>2"))
            .when(pl.col("volume_z") > 1)
            .then(pl.lit("z>1"))
            .otherwise(pl.lit("z<=1"))
            .alias("vz_b"),
            pl.when(pl.col("williams_r").is_not_null() & (pl.col("williams_r") < -90))
            .then(pl.lit("wr<-90"))
            .when(pl.col("williams_r").is_not_null() & (pl.col("williams_r") < -80))
            .then(pl.lit("wr<-80"))
            .when(pl.col("rsi").is_not_null() & (pl.col("rsi") < 30))
            .then(pl.lit("rsi<30"))
            .when(pl.col("rsi").is_not_null() & (pl.col("rsi") > 70))
            .then(pl.lit("rsi>70"))
            .when(pl.col("mr_score").is_not_null() & (pl.col("mr_score") < -40))
            .then(pl.lit("mr<-40"))
            .when(pl.col("mr_score").is_not_null() & (pl.col("mr_score") > 40))
            .then(pl.lit("mr>40"))
            .when(pl.col("mom_score").is_not_null() & (pl.col("mom_score") > 40))
            .then(pl.lit("mom>40"))
            .when(pl.col("mom_score").is_not_null() & (pl.col("mom_score") < -40))
            .then(pl.lit("mom<-40"))
            .otherwise(pl.lit("osc_mid"))
            .alias("osc_b"),
        ]
    )


def metrics(df: pl.DataFrame) -> dict:
    d = df.filter(pl.col("label") >= 0)
    if d.height == 0:
        return {"n": 0}
    fresh = d.filter(pl.col("fresh"))
    stale_share = 1.0 - (fresh.height / d.height if d.height else 0)
    base = d if fresh.height >= 10 else d
    wr = float((base["label"] == 1).mean())  # type: ignore[arg-type]
    mean_net = float(base["net"].mean())  # type: ignore[arg-type]
    median_net = float(base["net"].median())  # type: ignore[arg-type]
    fresh_share = float(d["fresh"].mean())  # type: ignore[arg-type]
    std_v = base["net"].std()
    std_net = float(std_v) if std_v is not None and base.height > 1 else None  # type: ignore[arg-type]
    return {
        "n": int(d.height),
        "n_fresh": int(fresh.height),
        "wr": wr,
        "mean_net": mean_net,
        "median_net": median_net,
        "fresh_share": fresh_share,
        "stale_share": float(stale_share),
        "std_net": std_net,
        "clip_ceiling_suspect": abs(median_net - CLIP + FEE) < 0.01
        or abs(median_net + CLIP + FEE) < 0.01
        or abs(median_net - 10) < 0.001,
    }


def chronological_oos(df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    d = df.sort("triggered_at")
    cut = max(1, int(d.height * 0.7))
    return d.head(cut), d.tail(d.height - cut)


def mine_candidates(lab: pl.DataFrame) -> list[dict]:
    d = lab.filter(
        pl.col("regime_volatility").is_not_null()
        & pl.col("trading_session").is_not_null()
        & pl.col("label") >= 0
    )
    keys = ["direction", "tf", "regime_volatility", "vz_b", "osc_b", "trading_session"]
    g = (
        d.group_by(keys)
        .agg(
            [
                pl.len().alias("n"),
                (pl.col("label") == 1).mean().alias("wr"),
                pl.col("net").mean().alias("mean_net"),
                pl.col("net").median().alias("median_net"),
                pl.col("fresh").mean().alias("fresh_share"),
            ]
        )
        .filter(pl.col("n") >= 40)
        .filter(pl.col("mean_net") > -0.05)
        .sort("mean_net", descending=True)
    )
    return g.head(80).to_dicts()


def filter_row(df: pl.DataFrame, cand: dict) -> pl.DataFrame:
    q = df
    for k in ["direction", "tf", "regime_volatility", "vz_b", "osc_b", "trading_session"]:
        q = q.filter(pl.col(k) == cand[k])
    return q


def fine_tune(df: pl.DataFrame, base: dict) -> list[dict]:
    """Layer-wise tweaks on top of base cell; both halves must keep same sign mean_net."""
    layers = [
        ("adx>25", pl.col("adx").is_not_null() & (pl.col("adx") > 25)),
        ("adx<20", pl.col("adx").is_not_null() & (pl.col("adx") < 20)),
        ("mom>20", pl.col("mom_score").is_not_null() & (pl.col("mom_score") > 20)),
        ("mom<-20", pl.col("mom_score").is_not_null() & (pl.col("mom_score") < -20)),
        ("mr<-20", pl.col("mr_score").is_not_null() & (pl.col("mr_score") < -20)),
        ("mr>20", pl.col("mr_score").is_not_null() & (pl.col("mr_score") > 20)),
        ("rsi_30_50", pl.col("rsi").is_not_null() & (pl.col("rsi") >= 30) & (pl.col("rsi") <= 50)),
        ("z_strict_le1", pl.col("volume_z").is_not_null() & (pl.col("volume_z") <= 1)),
    ]
    out = []
    base_df = filter_row(df, base)
    tr, te = chronological_oos(base_df)
    base_tr, base_te = metrics(tr), metrics(te)
    out.append(
        {
            "layer": "base",
            "train": base_tr,
            "test": base_te,
            "pass": _passes(base_tr, base_te),
        }
    )
    for name, expr in layers:
        sub = base_df.filter(expr)
        if sub.height < 50:
            continue
        tr, te = chronological_oos(sub)
        mt, me = metrics(tr), metrics(te)
        out.append(
            {
                "layer": name,
                "train": mt,
                "test": me,
                "pass": _passes(mt, me)
                and me.get("mean_net", -9) >= base_te.get("mean_net", -9) - 0.02,
            }
        )
    return out


def _passes(tr: dict, te: dict) -> bool:
    if tr.get("n", 0) < GATE["min_n_train"] or te.get("n", 0) < GATE["min_n_test"]:
        return False
    if te.get("fresh_share", 0) < GATE["min_fresh"]:
        return False
    if te.get("stale_share", 1) > GATE["max_stale_share"]:
        return False
    if te.get("wr", 0) < GATE["min_wr_test"]:
        return False
    if te.get("mean_net", -9) < GATE["min_mean_net_test"]:
        return False
    if te.get("clip_ceiling_suspect"):
        return False
    # same-sign train/test mean
    if tr.get("mean_net", 0) * te.get("mean_net", 0) < 0 and abs(tr.get("mean_net", 0)) > 0.02:
        return False
    return True


def packet_for(cand: dict, tune: list[dict], day: str) -> dict:
    best = None
    for t in tune:
        if t.get("pass") and (
            best is None or t["test"]["mean_net"] > best["test"]["mean_net"]
        ):
            best = t
    status = "arena_ready" if best else "needs_more_data"
    slug = slugify(
        [
            cand["direction"],
            cand["tf"],
            cand["regime_volatility"],
            cand["vz_b"],
            cand["osc_b"],
            cand["trading_session"],
            best["layer"] if best else "base",
        ]
    )
    pid = hashlib.sha1(slug.encode()).hexdigest()[:12]
    rules = {
        "direction": cand["direction"],
        "timeframe": cand["tf"],
        "regime_volatility": cand["regime_volatility"],
        "volume_z_bucket": cand["vz_b"],
        "oscillator_bucket": cand["osc_b"],
        "trading_session": cand["trading_session"],
        "layer": best["layer"] if best else "base",
        "entry": "next_candle_open",
        "exit": "next_candle_close_same_tf",
        "fee_bps_rt": 16,
        "paper_only": True,
    }
    return {
        "packet_id": f"pat_{pid}",
        "slug": slug,
        "status": status,
        "stage": "paper_candidate" if best else "research_candidate",
        "rules": rules,
        "discovery": cand,
        "fine_tune": tune,
        "selected": best,
        "arena_signal_filter": {
            "direction": cand["direction"],
            "timeframe": cand["tf"],
            "session": cand["trading_session"],
            "regime_volatility": cand["regime_volatility"],
            # descriptive only — runtime strategies table not mutated here
        },
        "safety": {
            "live_trading": False,
            "trade_intents": False,
            "strategy_pool_mutation": False,
            "requires_frank_for_live": True,
        },
        "created": day,
        "method": "pattern_to_arena_loop_v1",
    }


def main():
    days = int(os.environ.get("MINE_DAYS", "30"))
    day = utcnow().strftime("%Y-%m-%d")
    out_dir = Path(f"/home/frank/sycode-mining-outputs/pattern-to-arena/{day}")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"START days={days} out={out_dir}", flush=True)

    con = attach()
    sig = load_signals(con, days)
    print(f"signals={sig.height}", flush=True)
    parts = []
    for tf in TFS:
        cans = load_candles(con, tf, days)
        print(f"candles {tf}={cans.height}", flush=True)
        parts.append(label_tf(sig.filter(pl.col("timeframe") == tf), cans, tf))
    lab = add_buckets(pl.concat(parts, how="diagonal_relaxed"))
    print(f"labeled={lab.height}", flush=True)

    cands = mine_candidates(lab)
    print(f"raw_candidates={len(cands)}", flush=True)

    packets = []
    arena_ready = []
    for i, c in enumerate(cands[:25]):
        tune = fine_tune(lab, c)
        pkt = packet_for(c, tune, day)
        packets.append(pkt)
        (out_dir / f"{pkt['slug']}.json").write_text(json.dumps(pkt, indent=2, default=str))
        if pkt["status"] == "arena_ready":
            arena_ready.append(pkt)
        print(
            f"[{i+1}] {pkt['slug']} status={pkt['status']} "
            f"test_n={pkt['selected']['test']['n'] if pkt['selected'] else '-'} "
            f"test_wr={pkt['selected']['test']['wr'] if pkt['selected'] else '-'} "
            f"test_net={pkt['selected']['test']['mean_net'] if pkt['selected'] else '-'}",
            flush=True,
        )

    summary = {
        "day": day,
        "days_window": days,
        "n_signals": sig.height,
        "n_labeled": lab.height,
        "n_raw_candidates": len(cands),
        "n_packets": len(packets),
        "n_arena_ready": len(arena_ready),
        "arena_ready_slugs": [p["slug"] for p in arena_ready],
        "gates": GATE,
        "fee_bps_rt": 16,
        "finished": utcnow().isoformat(),
        "paper_only": True,
    }
    (out_dir / "results.json").write_text(json.dumps(summary, indent=2))
    (out_dir / "arena_prep_manifest.json").write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "packet_id": p["packet_id"],
                        "slug": p["slug"],
                        "rules": p["rules"],
                        "test": p["selected"]["test"] if p["selected"] else None,
                        "train": p["selected"]["train"] if p["selected"] else None,
                        "kanban_title": f"ARENA PAPER: {p['slug']}",
                        "kanban_body_path": str(out_dir / f"{p['slug']}.json"),
                    }
                    for p in arena_ready
                ]
            },
            indent=2,
            default=str,
        )
    )
    print(json.dumps(summary, indent=2), flush=True)
    print(f"ARENA_READY={len(arena_ready)}", flush=True)
    return 0 if packets else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {e}", flush=True)
        raise
