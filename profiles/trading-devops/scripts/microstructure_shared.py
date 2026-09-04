#!/usr/bin/env python3
"""Shared row-set construction for the microstructure IC test and its gate.

Single source of truth for the paired complete-case sample that BOTH
paired-sample-gate.py (the D2 gate variable) and the IC test (P4) consume.

Before this module existed, the gate and the IC test each built the row set
independently and diverged on three axes (raised as t_ffbbc529):

  1. Join staleness -- the gate used a HARD backward join_asof tolerance (5m);
     the IC test used an unbounded backward asof (the round-2 voiding bug,
     t_a5cbc564), attaching feature bars up to ~35h stale and fabricating ~10x
     the honest sample size.
  2. Rolling-window integrity -- the gate computed cvd_zscore segment-scoped
     with a wall-clock span guard; the IC test (P3) z-scored straight across
     dead-tape gaps (107 segments in the current window).
  3. Label construction -- the gate offsets the trigger by the horizon before
     the forward asof; the IC test forward-asof'd on the trigger time itself,
     collapsing 1h and 4h to the same near-term label.

Everything that defines the measured/tested row set lives here so the gate's n
IS the IC test's n. Importing from this module is the only supported path.
"""
from __future__ import annotations

import pandas as pd
import polars as pl
import psycopg2
from datetime import datetime, timedelta, timezone


# Every one of these must be non-null for a row to be a complete case.
MICRO_FEATURES = ["cvd_zscore", "aggressor_imbalance", "cvd"]

# The IC test is a PAIRED micro-vs-baseline comparison, so a row is only
# testable if the baseline scores are present too.
BASELINE_FEATURES = ["composite_confidence_score", "conviction_score"]


# Equality + half-open timestamp range on idx_tick_trades_symbol_ts.
# A composite ROW compare `(symbol, timestamp) >= (...)` seq-scans ~51M
# rows (r5 EXPLAIN 2026-09-04). `symbol BETWEEN x AND x` uses the wrong
# index (idx_tick_trades_timestamp). `symbol = X AND timestamp >= Y AND
# timestamp < Z` is an Index Only Scan (~8k rows / 15m BTCUSDT).
# Chunked because a 7-day one-shot still canceled at 60s under load.
# Fetch MUST NOT lower the wrapper/PGOPTIONS 600s session timeout.
MINUTE_BAR_SQL = """
        SELECT symbol,
               date_trunc('minute', timestamp) AS bar_ts,
               SUM(CASE WHEN is_buyer_maker THEN -quantity ELSE quantity END)::float8 AS cvd,
               SUM(quantity)::float8 AS total_volume,
               COUNT(*)::bigint AS trade_count
        FROM tick_trades
        WHERE symbol = %s
          AND timestamp >= %s
          AND timestamp <  %s
        GROUP BY 1, 2
        ORDER BY 1, 2
"""

# P5 IC + HL battery window. This is a SQL bound, NOT the retired
# est_bars span heuristic and NOT the gate variable (still paired_n).
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_WARMUP_MINUTES = 24
DEFAULT_CHUNK_HOURS = 6


class TickTradesFetchError(RuntimeError):
    """FAIL-CLOSED: a tick_trades chunk failed; do not score a partial window."""


def lookback_bounds(
    now: datetime,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    warmup_minutes: int = DEFAULT_WARMUP_MINUTES,
) -> tuple[datetime, datetime]:
    """Return half-open [start, end) UTC bounds for the tape fetch."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    end = now
    start = now - timedelta(days=int(lookback_days), minutes=int(warmup_minutes))
    return start, end


def iter_symbol_chunks(
    symbols,
    start: datetime,
    end: datetime,
    chunk_hours: int = DEFAULT_CHUNK_HOURS,
):
    """Yield (symbol, chunk_start, chunk_end) half-open UTC slices."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    else:
        start = start.astimezone(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    else:
        end = end.astimezone(timezone.utc)
    step = timedelta(hours=int(chunk_hours))
    if step <= timedelta(0):
        raise ValueError("chunk_hours must be positive")
    for symbol in symbols:
        cursor = start
        while cursor < end:
            nxt = min(cursor + step, end)
            yield symbol, cursor, nxt
            cursor = nxt


def iter_symbol_day_chunks(symbols, start: datetime, end: datetime):
    """24h slices. Fetch uses DEFAULT_CHUNK_HOURS (6h), not this helper."""
    yield from iter_symbol_chunks(symbols, start, end, chunk_hours=24)


def fetch_minute_bars(conn, symbols, start: datetime, end: datetime) -> pl.DataFrame | None:
    """1m CVD bars via per-symbol 6h chunks (bounded, index-friendly).

    Does NOT SET statement_timeout. The monitor wrapper / PGOPTIONS own the
    600s session timeout; lowering it to 180s was the r4 QueryCanceled cause.
    Any chunk exception is FAIL-CLOSED: no partial window is returned.
    """
    # Parallel workers on this 62GB table stampede DataFileRead and make a
    # 7s 1-day chunk take 60s+. Serial index-only scans stay bounded.
    ac = conn.autocommit
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute("SET max_parallel_workers_per_gather = 0")
    finally:
        cur.close()
        conn.autocommit = ac

    frames = []
    chunks = list(iter_symbol_chunks(symbols, start, end))
    for i, (symbol, c0, c1) in enumerate(chunks, 1):
        cur_q = conn.cursor()
        try:
            cur_q.execute(MINUTE_BAR_SQL, (symbol, c0, c1))
            rows = cur_q.fetchall()
            cols = [d[0] for d in cur_q.description]
        except Exception as exc:
            raise TickTradesFetchError(
                f"FAIL-CLOSED: tick_trades chunk failed {symbol} "
                f"{c0.isoformat()} -> {c1.isoformat()}: {exc}"
            ) from exc
        finally:
            cur_q.close()
        raw = pd.DataFrame(rows, columns=cols)
        print(
            f"[bars] chunk {i}/{len(chunks)} {symbol} "
            f"{c0.isoformat()} -> {c1.isoformat()} rows={len(raw)}",
            flush=True,
        )
        if len(raw) > 0:
            frames.append(raw)
    if not frames:
        return None
    combined = pd.concat(frames, ignore_index=True)
    bars = pl.from_pandas(combined)
    return _normalize_bar_ts(bars).sort(["symbol", "bar_ts"])


def _normalize_bar_ts(bars: pl.DataFrame) -> pl.DataFrame:
    """Normalize the `bar_ts` column to tz-aware UTC at microsecond precision.

    pandas/psycopg2 deliver either naive or tz-aware timestamps depending on
    the column; join_asof keys must share one tz or the join silently misses.
    The tz is inspected from the concrete column dtype (not an Expr), then the
    right normalization is applied. Naive input is assumed to already be UTC
    (the source is a single Postgres `postgres` DB in one tz)."""
    tz = getattr(bars["bar_ts"].dtype, "time_zone", None)
    if tz is None:
        norm = pl.col("bar_ts").dt.replace_time_zone("UTC").dt.cast_time_unit("us")
    else:
        norm = pl.col("bar_ts").dt.convert_time_zone("UTC").dt.cast_time_unit("us")
    return bars.with_columns(norm.alias("bar_ts"))


def segment_mask_bars(bars: pl.DataFrame, gap_min: int = 5, window: int = 24) -> pl.DataFrame:
    """Apply segment-scoped gap masking + wall-clock window guard to 1m bars.

    `bars` must carry: symbol, bar_ts (datetime), cvd, total_volume.
    Returns the same rows with segment_id, window_span_min, window_ok,
    cvd_ma_1h, cvd_std_1h, cvd_zscore (segment-scoped) and aggressor_imbalance
    added.

    A tape gap of more than `gap_min` minutes starts a new segment so no
    rolling window ever rolls across dead tape (round-2 was void for lacking
    this, t_a5cbc564). A second guard requires the window to span exactly
    (window-1) minutes of wall clock, so a column named _1h is a real hour
    even inside one segment.

    Idempotent: safe to call on bars that already carry a (possibly wrong)
    cvd_zscore -- the shared values overwrite.
    """
    out = _normalize_bar_ts(bars)
    out = out.sort(["symbol", "bar_ts"]).with_columns(
        pl.col("bar_ts").diff().over("symbol").dt.total_minutes().alias("_gap")
    )
    out = out.with_columns(
        (pl.col("_gap") > gap_min).fill_null(True).cum_sum().over("symbol").alias("segment_id")
    )
    out = out.with_columns(
        pl.when(pl.col("total_volume") > 0)
        .then(pl.col("cvd") / pl.col("total_volume"))
        .otherwise(None)
        .alias("aggressor_imbalance")
    )
    out = out.with_columns([
        pl.col("cvd").rolling_mean(window_size=window).over(["symbol", "segment_id"]).alias("cvd_ma_1h"),
        pl.col("cvd").rolling_std(window_size=window).over(["symbol", "segment_id"]).alias("cvd_std_1h"),
    ])
    out = out.with_columns(
        (pl.col("bar_ts") - pl.col("bar_ts").shift(window - 1).over(["symbol", "segment_id"]))
        .dt.total_minutes().alias("window_span_min")
    )
    out = out.with_columns(
        (pl.col("window_span_min") == (window - 1)).fill_null(False).alias("window_ok")
    )
    out = out.with_columns(
        pl.when(pl.col("window_ok") & (pl.col("cvd_std_1h") > 0))
        .then((pl.col("cvd") - pl.col("cvd_ma_1h")) / pl.col("cvd_std_1h"))
        .otherwise(pl.lit(None, dtype=pl.Float64))
        .alias("cvd_zscore")
    )
    return out.drop(["_gap"])


def attach_forward_label(signals: pl.DataFrame, candles_1h: pl.DataFrame, horizon_hours: int) -> pl.DataFrame:
    """Forward-asof the 1h close `horizon_hours` AHEAD of the trigger.

    The label is the first 1h close at or after trigger + horizon. Joining on
    the trigger time itself grabs the next candle boundary regardless of the
    horizon requested -- it silently collapses 1h and 4h to the same near-term
    label and undercounts the labeled set (464 vs the correct 854 at 1h on the
    round-4 corpus).
    """
    shifted = signals.with_columns(
        (pl.col("triggered_at") + pl.duration(hours=horizon_hours)).alias("target_time")
    ).sort("target_time")
    joined = shifted.join_asof(
        candles_1h, by="symbol", left_on="target_time", right_on="candle_time", strategy="forward"
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


def pair_backward_with_tolerance(labeled: pl.DataFrame, bars: pl.DataFrame, tolerance_min: int) -> pl.DataFrame:
    """Backward join_asof signals->micro bars with a HARD staleness tolerance.

    An unbounded backward asof over a sub-100%-duty-cycle tape attaches feature
    bars up to ~35h stale and fabricates ~10x the honest sample size (round-2
    void, t_a5cbc564). The tolerance is mandatory and non-optional; there is no
    code path here that disables it.
    """
    return labeled.sort("triggered_at").join_asof(
        bars.sort("bar_ts"), by="symbol", left_on="triggered_at", right_on="bar_ts",
        strategy="backward", tolerance=f"{tolerance_min}m",
    )


def complete_case(paired: pl.DataFrame, micro_features=None, baseline_features=None) -> pl.DataFrame:
    """Filter to the paired complete case.

    Every micro feature AND every baseline feature non-null AND a forward label
    present (label >= 0). This is the gate variable and the IC test's sample,
    so the measured n IS the tested n.
    """
    mf = list(micro_features) if micro_features is not None else MICRO_FEATURES
    bf = list(baseline_features) if baseline_features is not None else BASELINE_FEATURES
    out = paired
    for feat in mf + bf:
        if feat in out.columns:
            out = out.filter(pl.col(feat).is_not_null())
    out = out.filter(pl.col("label") >= 0)
    return out


# ---------------------------------------------------------------------------
# DB loaders -- shared so the gate and the IC test read the SAME rows from the
# SAME Postgres `postgres` DB. The gate's window == the IC test's window.
# Binance futures leg only: tick_trades carries 12 symbol namespaces (bare
# Hyperliquid tickers + TESTBTC/VERIFYFIX junk); always filter explicitly.
# ---------------------------------------------------------------------------

PG_CONFIG = {
    "host": "127.0.0.1",
    "port": 5432,
    "user": "postgres",
    "password": "postgres",
    "dbname": "postgres",
}

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "BNBUSDT"]

# Hyperliquid leg: bare tickers (no venue suffix). Namespaces MUST never mix
# (t_45f1a2b1 caveat #5): 'BTC' (Hyperliquid) and 'BTCUSDT' (Binance futures)
# are separate legs and are joined independently per (symbol, minute). The
# loaders below accept either namespace; the join keys are (symbol, minute) so
# the two populations can never collide.
HYPERLIQUID_SYMBOLS = ["BTC", "ETH", "SOL", "AVAX", "BNB"]


def venue_for_symbol(symbol: str) -> str:
    """'binance' for *USDT futures symbols, 'hyperliquid' for bare tickers."""
    return "binance" if symbol.endswith("USDT") else "hyperliquid"


def build_paired_rows(conn, symbols, tolerance_min=5, gap_min=5, window=24,
                      horizon_hours=1, lookback_days=DEFAULT_LOOKBACK_DAYS,
                      now: datetime | None = None):
    """THE single canonical paired complete-case row-set constructor.

    Both paired-sample-gate.py (the D2 gate variable) and
    compute-ic-microstructure.py (P4, the IC test) call this so they consume
    the IDENTICAL rows. This is the fix for t_ffbbc529: previously the gate and
    the test each built the row set independently and diverged on three axes
    (unbounded asof, unmasked P3 z-score, trigger-time label), so the gate's n
    did not describe what the test actually consumed.

    Steps (define the measured/tested n exactly):
      1. Raw 1m bars from tick_trades (Binance leg), segment-scoped gap masking
         + wall-clock window guard (no z-score rolls across dead tape).
      2. Signal window = the bars' tape window (win_start..win_end), so the test
         never measures over a wider universe than the features cover.
      3. Forward label offset by the horizon (1h vs 4h do NOT collapse).
      4. Backward join_asof signals->bars with a HARD tolerance (no 35h staleness).
      5. Complete-case filter: every micro + baseline feature non-null AND a
         non-flat forward label.

    Returns (paired_complete_case_df, meta_dict). meta includes the raw/clean
    bar counts, signal counts, overlap, cvd_zscore null rate, and the
    paired_complete_case_n -- the gate variable and the IC test's n.
    """
    tape_start, tape_end = lookback_bounds(
        now or datetime.now(timezone.utc), lookback_days, window
    )
    bars = load_clean_bars(
        conn, symbols, gap_min=gap_min, window=window,
        start=tape_start, end=tape_end, lookback_days=lookback_days,
    )
    if bars is None or bars.height == 0:
        return None, {"error": "no tick_trades bars for the requested symbols"}
    bars = segment_mask_bars(bars, gap_min=gap_min, window=window)

    win_start, win_end = bars["bar_ts"].min(), bars["bar_ts"].max()
    raw_bars = bars.height
    clean_bars = bars.filter(pl.col("window_ok")).height

    signals = load_signals(conn, symbols, win_start, win_end)
    if signals is None or signals.height == 0:
        return None, {"error": "no signal_journeys inside the tape window",
                      "raw_bars": raw_bars, "clean_bars": clean_bars,
                      "tape_window": [str(win_start), str(win_end)]}

    # Overlap: fraction of signals whose trigger minute is a LIVE tape minute.
    live_minutes = bars.select(["symbol", "bar_ts"]).unique()
    sig_minutes = signals.with_columns(
        pl.col("triggered_at").dt.truncate("1m").alias("bar_ts")
    )
    overlapping = sig_minutes.join(live_minutes, on=["symbol", "bar_ts"], how="inner").height
    overlap_pct = round(100.0 * overlapping / signals.height, 2) if signals.height else 0.0

    candles = load_candles(conn, symbols, "1h", win_start)
    if candles is None:
        return None, {"error": "no 1h candles", "raw_bars": raw_bars,
                      "clean_bars": clean_bars, "signals_in_window": signals.height,
                      "signal_tape_overlap_pct": overlap_pct,
                      "tape_window": [str(win_start), str(win_end)]}

    labeled = attach_forward_label(signals, candles, horizon_hours)
    paired = pair_backward_with_tolerance(labeled, bars, tolerance_min)
    complete = complete_case(paired)

    zs_non_null = paired.filter(pl.col("cvd_zscore").is_not_null()).height
    cvd_null = round(1 - zs_non_null / paired.height, 4) if paired.height else None

    meta = {
        "horizon_hours": horizon_hours,
        "tolerance_min": tolerance_min,
        "gap_min": gap_min,
        "rolling_window_bars": window,
        "raw_bars": raw_bars,
        "clean_bars": clean_bars,
        "signals_in_window": signals.height,
        "signal_tape_overlap_pct": overlap_pct,
        "tape_window": [str(win_start), str(win_end)],
        "cvd_zscore_null_rate": cvd_null,
        "labeled_non_flat": int(paired.filter(pl.col("label") >= 0).height),
        "signals_joined_within_tolerance": int(paired.filter(pl.col("bar_ts").is_not_null()).height),
        "paired_complete_case_n": int(complete.height),
    }
    return complete, meta


def load_clean_bars(
    conn,
    symbols,
    gap_min: int = 5,
    window: int = 24,
    start: datetime | None = None,
    end: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> pl.DataFrame | None:
    """Raw 1m bars (symbol, bar_ts, cvd, total_volume) from tick_trades.

    Bounded to the P5/battery lookback (default 7d + rolling warmup).
    Unbounded GROUP BY on this 62GB table QueryCancels (t_b9bec246).
    Returns None when there are no rows. Segment masking is applied by the
    caller via segment_mask_bars so the same raw bars feed both pipelines.
    `gap_min`/`window` are accepted for call-site compatibility; masking
    still happens in segment_mask_bars.
    """
    del gap_min, window
    if start is None or end is None:
        start, end = lookback_bounds(datetime.now(timezone.utc), lookback_days)
    return fetch_minute_bars(conn, symbols, start, end)


def load_signals(conn, symbols, start, end=None) -> pl.DataFrame | None:
    """signal_journeys with the columns the IC test + gate both need.

    `end` is an exclusive upper bound on triggered_at. Pass None for an open
    window. P4 (compute-ic-microstructure.py) uses the open window so a stale
    hard-coded cutoff can never silently exclude the micro capture window
    (that defect produced the artifact result in t_d689c863).
    """
    query = """
        SELECT id, symbol, direction, triggered_at, entry_price,
               trigger_score, composite_confidence_score, conviction_score
        FROM signal_journeys
        WHERE entry_price > 0
          AND symbol = ANY(%s)
          AND triggered_at >= %s
    """
    params = [list(symbols), start]
    if end is not None:
        query += " AND triggered_at <= %s"
        params.append(end)
    query += " ORDER BY triggered_at"
    
    raw = pd.read_sql(query, conn, params=params)
    if len(raw) == 0:
        return None
    return pl.from_pandas(raw).with_columns(
        pl.col("triggered_at").dt.convert_time_zone("UTC").dt.cast_time_unit("us")
    ).sort("triggered_at")


def load_candles(conn, symbols, timeframe, start) -> pl.DataFrame | None:
    """1h (or other) candles for forward-label construction."""
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
