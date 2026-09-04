#!/usr/bin/env python3
"""
Paired complete-case sample gate for the microstructure IC test.

This is the gate variable approved by trading-risk-reviewer on t_dc702203
(REVIEW_VERDICT=APPROVED, 2026-08-01) and implemented under t_18fda1f4:

    Fire the IC re-run task only when the paired complete-case sample
    n >= 200, measured directly at <= 5 minute join staleness.

It replaces the retired `est_bars = hours_span * 60 * 5 * 0.5` heuristic in
microstructure-data-monitor.sh. That quantity was calendar span, not bars: it
was monotonically non-decreasing and kept rising while the collector was dead,
which is why the old gate passed twice (1.98x, 2.25x) while the binding
constraint -- paired rows -- never moved.

The measurement here is the SAME row set the IC test consumes:

  1. Clean 1m bars per symbol, straight from tick_trades, with SEGMENT-SCOPED
     gap masking. A gap of more than --gap-min minutes starts a new segment;
     rolling features are computed .over(["symbol", "segment_id"]) and are
     additionally masked unless the window spans exactly (window-1) minutes of
     wall clock. Round-2 (t_a5cbc564) is void precisely for lacking this.
  2. Backward join_asof signals -> micro features with a HARD tolerance
     (default 5m). An unbounded asof attaches bars up to 35h stale and
     fabricated round-2's n (634 unbounded vs 66 honest). The tolerance is not
     optional and there is no code path here that disables it.
  3. Count complete-case rows: every micro feature non-null, a forward label
     present, AND the baseline scores present -- because the IC test is a
     PAIRED comparison of micro vs baseline on identical rows, so a row missing
     either side is not a testable row. That count is the gate variable.

Reproduces the round-4 artifact exactly (t_eae52027, /tmp/p4-ic-results.json):
1h paired_n=22 / 4h paired_n=23, cvd_zscore null rate 0.9376, overlap 8.84%.

Exit codes:  0 = measured (regardless of wait/run)  |  1 = measurement failed.

Usage:
  python3 paired-sample-gate.py                       # human + summary line
  python3 paired-sample-gate.py --json /tmp/gate.json # + machine artifact
"""
import argparse
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import polars as pl
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from microstructure_shared import (  # noqa: E402
    DEFAULT_LOOKBACK_DAYS,
    fetch_minute_bars,
    lookback_bounds,
    segment_mask_bars,
)

# pandas nags about the raw psycopg2 connection and polars about asof
# sortedness checks under `by`. Both are expected here; keep cron stdout clean
# so the summary line is the signal.
warnings.filterwarnings("ignore", category=UserWarning)

PG_CONFIG = {
    "host": "127.0.0.1",
    "port": 5432,
    "user": "postgres",
    "password": "postgres",
    "dbname": "postgres",
}

# Binance futures leg. tick_trades carries 12 symbol namespaces (bare
# Hyperliquid tickers + TESTBTC/VERIFYFIX junk); always filter explicitly.
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "BNBUSDT"]

# Every one of these must be non-null for a row to be a complete case.
MICRO_FEATURES = ["cvd_zscore", "aggressor_imbalance", "cvd"]

# The IC test is a PAIRED micro-vs-baseline comparison, so a row is only
# testable if the baseline scores are present too. Dropping this requirement
# overstates the sample (26/41 vs the honest 22/23 on the round-4 corpus).
BASELINE_FEATURES = ["composite_confidence_score", "conviction_score"]


def build_clean_bars(conn, symbols, gap_min, window, start, end):
    """1m bars from tick_trades with segment-scoped gap masking.

    Tape fetch is day-chunked on (symbol, timestamp) over the lookback
    window (default 7d + rolling warmup). Unbounded GROUP BY QueryCancels
    on the 62GB tick_trades table (t_b9bec246). CVD sign convention
    matches build-1m-micro-features.py: is_buyer_maker false => taker buy
    => +quantity.
    """
    raw = fetch_minute_bars(conn, symbols, start, end)
    if raw is None or raw.height == 0:
        return None
    return segment_mask_bars(raw, gap_min=gap_min, window=window)


def load_signals(conn, symbols, start, end):
    query = """
        SELECT id, symbol, direction, triggered_at, entry_price,
               trigger_score, composite_confidence_score, conviction_score
        FROM signal_journeys
        WHERE entry_price > 0
          AND symbol = ANY(%s)
          AND triggered_at >= %s
          AND triggered_at <= %s
        ORDER BY triggered_at
    """
    raw = pd.read_sql(query, conn, params=[list(symbols), start, end])
    if len(raw) == 0:
        return None
    return pl.from_pandas(raw).with_columns(
        pl.col("triggered_at").dt.convert_time_zone("UTC").dt.cast_time_unit("us")
    ).sort("triggered_at")


def load_candles(conn, symbols, timeframe, start):
    query = """
        SELECT symbol, timestamp AS candle_time, close AS next_close
        FROM candles
        WHERE timeframe = %s
          AND symbol = ANY(%s)
          AND timestamp >= %s
        ORDER BY symbol, timestamp
    """
    raw = pd.read_sql(query, conn, params=[timeframe, list(symbols), start])
    if len(raw) == 0:
        return None
    return pl.from_pandas(raw).with_columns(
        pl.col("candle_time").dt.convert_time_zone("UTC").dt.cast_time_unit("us"),
        pl.col("next_close").cast(pl.Float64),
    ).sort("candle_time")


def attach_forward_label(signals, candles, horizon_hours):
    """Forward-asof the close `horizon_hours` AHEAD of the trigger.

    The label must be the first close at or after trigger + horizon. Joining on
    the trigger time itself (no offset) grabs the *next candle boundary*, which
    is a ~0-60 minute forward return regardless of the horizon requested -- it
    silently collapses 1h and 4h to the same near-term label and undercounts
    the labeled set (464 vs the correct 854 at 1h on the round-4 corpus).
    """
    shifted = signals.with_columns(
        (pl.col("triggered_at") + pl.duration(hours=horizon_hours)).alias("target_time")
    ).sort("target_time")

    joined = shifted.join_asof(
        candles, by="symbol", left_on="target_time", right_on="candle_time",
        strategy="forward",
    )
    joined = joined.with_columns(
        ((pl.col("next_close") - pl.col("entry_price")) / pl.col("entry_price") * 100).alias("fwd_return")
    )
    joined = joined.with_columns(
        pl.when(pl.col("direction") == "SHORT").then(-pl.col("fwd_return"))
        .otherwise(pl.col("fwd_return")).alias("fwd_return_dir")
    )
    joined = joined.with_columns(pl.col("fwd_return_dir").clip(-10, 10).alias("fwd_return_clipped"))
    return joined.with_columns(
        pl.when(pl.col("fwd_return_clipped") > 0.2).then(pl.lit(1))
        .when(pl.col("fwd_return_clipped") < -0.2).then(pl.lit(0))
        .otherwise(pl.lit(-1)).alias("label")
    )


def measure_horizon(signals, bars, conn, symbols, horizon, tolerance_min, window_start):
    horizon_hours = int(horizon.rstrip("h"))

    # Always label off 1h candles offset by the horizon. Using native 4h candles
    # instead snaps the label to a 4h grid and loses rows to the coarser bound.
    candles = load_candles(conn, symbols, "1h", window_start)
    if candles is None:
        return {"horizon": horizon, "error": "no 1h candles"}

    labeled = attach_forward_label(signals, candles, horizon_hours)

    # THE join. tolerance is mandatory -- an unbounded backward asof over a
    # sub-100%-duty-cycle tape attaches feature bars up to 35h stale and
    # fabricates ~10x the honest sample size.
    paired = labeled.sort("triggered_at").join_asof(
        bars.sort("bar_ts"), by="symbol", left_on="triggered_at", right_on="bar_ts",
        strategy="backward", tolerance=f"{tolerance_min}m",
    )

    complete = paired
    for feat in MICRO_FEATURES + BASELINE_FEATURES:
        complete = complete.filter(pl.col(feat).is_not_null())
    complete = complete.filter(pl.col("label") >= 0)

    joined_any = paired.filter(pl.col("bar_ts").is_not_null()).height
    zs_non_null = paired.filter(pl.col("cvd_zscore").is_not_null()).height
    n_signals = paired.height

    return {
        "horizon": horizon,
        "signals_in_window": n_signals,
        "signals_joined_within_tolerance": joined_any,
        "cvd_zscore_null_rate": round(1 - zs_non_null / n_signals, 4) if n_signals else None,
        "labeled_non_flat": paired.filter(pl.col("label") >= 0).height,
        "paired_complete_case_n": complete.height,
    }


def check_collector(conn, symbols):
    """Per-venue freshness. A global MAX(timestamp) hides a dead venue leg, and
    a tmux/PID check certifies nothing -- that has produced three false
    all-clears. Liveness here means the Binance leg's own max timestamp."""
    cur = conn.cursor()
    cur.execute("SELECT MAX(timestamp) FROM tick_trades WHERE symbol = ANY(%s)", (list(symbols),))
    last = cur.fetchone()[0]
    cur.close()
    if last is None:
        return None, None
    stale_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    return last, stale_h


def main():
    ap = argparse.ArgumentParser(description="Measure the paired complete-case IC sample")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--tolerance-min", type=int, default=5,
                    help="Hard join staleness tolerance in minutes (default 5)")
    ap.add_argument("--gap-min", type=int, default=5,
                    help="A tape gap longer than this starts a new segment (default 5)")
    ap.add_argument("--window", type=int, default=24,
                    help="Rolling window in bars for cvd z-score (default 24 = 1h)")
    ap.add_argument("--horizons", default="1h,4h")
    ap.add_argument("--target", type=int, default=200,
                    help="Gate threshold on paired complete-case n (default 200)")
    ap.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                    help="Tape SQL bound in days (P5/battery window; default 7). "
                         "Not a span heuristic; gate variable remains paired_n.")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    horizons = [h.strip() for h in args.horizons.split(",") if h.strip()]

    conn = psycopg2.connect(**PG_CONFIG)

    tape_start, tape_end = lookback_bounds(
        datetime.now(timezone.utc), args.lookback_days, args.window
    )
    bars = build_clean_bars(
        conn, symbols, args.gap_min, args.window, tape_start, tape_end
    )
    if bars is None or bars.height == 0:
        print("PAIRED_SAMPLE_GATE: FATAL no tick_trades bars for the requested symbols")
        conn.close()
        return 1

    clean_bars = bars.filter(pl.col("window_ok")).height
    win_start, win_end = bars["bar_ts"].min(), bars["bar_ts"].max()

    # Signal cohort starts at the lookback boundary (tape_start), not the
    # warm-up-extended bars.min(). Warmup bars enable rolling features at
    # the lookback edge; the signal window is [tape_start, win_end).
    signals = load_signals(conn, symbols, tape_start, win_end)
    if signals is None:
        print("PAIRED_SAMPLE_GATE: FATAL no signal_journeys inside the tape window")
        conn.close()
        return 1

    # Overlap: fraction of signals whose trigger minute is a LIVE tape minute.
    # This, not bar count, is the binding constraint -- growth in minutes when
    # nothing fires adds exactly zero testable rows.
    live_minutes = bars.select(["symbol", "bar_ts"]).unique()
    sig_minutes = signals.with_columns(pl.col("triggered_at").dt.truncate("1m").alias("bar_ts"))
    overlapping = sig_minutes.join(live_minutes, on=["symbol", "bar_ts"], how="inner").height
    overlap_pct = round(100.0 * overlapping / signals.height, 2) if signals.height else 0.0

    last_ts, stale_h = check_collector(conn, symbols)
    collector = "alive" if (stale_h is not None and stale_h < 1.0) else "dead"

    results = [
        measure_horizon(signals, bars, conn, symbols, h, args.tolerance_min, tape_start)
        for h in horizons
    ]
    conn.close()

    measured = [r for r in results if "paired_complete_case_n" in r]
    gate_n = max((r["paired_complete_case_n"] for r in measured), default=0)
    action = "run" if gate_n >= args.target else "wait"
    cvd_null = next((r["cvd_zscore_null_rate"] for r in measured), None)

    print("=== Paired Complete-Case Sample Gate ===")
    print(f"Timestamp:        {datetime.now(timezone.utc).isoformat()}")
    print(f"Symbols:          {','.join(symbols)}")
    print(f"Tape window:      {win_start} -> {win_end}")
    print(f"Tape bars:        {bars.height} raw / {clean_bars} clean (segment+window masked)")
    print(f"Segments:         {bars.select(['symbol', 'segment_id']).unique().height}")
    print(f"Signals in window:{signals.height}")
    print(f"Signal/tape overlap: {overlapping}/{signals.height} = {overlap_pct}%")
    print(f"Collector:        {collector} (last Binance tick {last_ts}, {stale_h:.1f}h stale)")
    print("")
    for r in results:
        if "error" in r:
            print(f"  {r['horizon']}: ERROR {r['error']}")
            continue
        print(f"  {r['horizon']}: paired_n={r['paired_complete_case_n']} "
              f"joined<= {args.tolerance_min}m={r['signals_joined_within_tolerance']} "
              f"labeled={r['labeled_non_flat']} "
              f"cvd_zscore_null_rate={r['cvd_zscore_null_rate']}")
    print("")
    print(f"MICROSTRUCTURE_MONITOR: paired_n={gate_n}/{args.target} | "
          f"staleness_tol={args.tolerance_min}m | clean_bars={clean_bars} | "
          f"overlap={overlap_pct}% | cvd_null={cvd_null} | "
          f"collector={collector} | stale={stale_h:.1f}h | action={action}")

    if action == "wait":
        print("")
        print(f"WAIT: paired complete-case n={gate_n} < {args.target}. "
              "The binding constraint is signal/tape overlap, not tape volume -- "
              "a low-duty-cycle collector cannot yield a testable sample however long it runs.")

    if args.json_out:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "symbols": symbols,
            "tolerance_min": args.tolerance_min,
            "gap_min": args.gap_min,
            "rolling_window_bars": args.window,
            "target_paired_n": args.target,
            "tape_window": [str(win_start), str(win_end)],
            "raw_bars": bars.height,
            "clean_bars": clean_bars,
            "signals_in_window": signals.height,
            "signal_tape_overlap_pct": overlap_pct,
            "collector": collector,
            "last_tick": str(last_ts),
            "staleness_hours": round(stale_h, 2) if stale_h is not None else None,
            "horizons": results,
            "gate_paired_n": gate_n,
            "action": action,
        }
        with open(args.json_out, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nJSON artifact: {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
