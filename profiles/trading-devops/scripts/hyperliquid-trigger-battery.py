#!/usr/bin/env python3
"""
Hyperliquid-namespaced trigger battery (t_db9814a1).

Mirror of the Binance `*USDT` trigger battery for the P5 microstructure IC
test. The Binance leg's signal_journeys rows are produced by the live signal
engine scanning *USDT symbols (WATCHLIST); bare-ticker Hyperliquid rows stopped
being produced because HL candle ingestion was switched off (bare `candles`
rows end 2026-01-20, signals end 2026-04-28). P5 IC was therefore structurally
unmeasurable on the HL leg (n=0 in-window signals) -- see
Reviews/task-evidence/2026-07-31-t_45f1a2b1-p5-round3-gate-decision-wait.md.

This script rebuilds that population for the HL leg:

  1. Pulls REAL 15m candles from the Hyperliquid public info API
     (https://api.hyperliquid.xyz/info, type=candleSnapshot) for the bare
     tickers BTC/ETH/SOL/AVAX/BNB over a live window (default: last 7 days).
  2. Runs a DETERMINISTIC technical trigger battery on those candles --
     RSI oversold-bounce / overbought-rejection, volume spike, EMA34/100
     golden/death cross, 20-bar breakout -- using the same weights as the
     production TRIGGER_CONFIG (server/src/domains/signals/services/trigger/
     config.ts) so the score scale mirrors the Binance battery.
  3. Inserts signal_journeys rows with trigger_score non-null, marked
     `signal_id = hl-battery-<SYM>-<bar_open>` and `is_active=false` so the
     research instrument is enumerable and deletable and no live finalizer /
     stale-hygiene path treats it as a live journey. A successful refresh
     ALWAYS deletes the prior in-window `hl-battery-*` population in the
     same transaction as the insert, including the zero-trigger case, so
     stale rows cannot keep the HL paired-n gate open.
  4. Upserts REAL 1h candles for the same symbols into `candles` so the IC
     label join (load_candles in microstructure_shared.py) can build forward
     labels without the dead-population gap.

Independence: triggers come from HL candle data (the same source the production
engine would scan), NOT from tick_trades -- so the micro features built from the
tape are not circular with the trigger battery. This mirrors how the Binance
leg's battery (candle/indicator triggers) is independent of its tape features.

Namespace separation (t_45f1a2b1 caveat #5): bare tickers ('BTC') and Binance
futures ('BTCUSDT') are separate legs. This script only ever touches bare
ticker symbols; the IC/gate scripts join per (symbol, minute) so the
namespaces never collide.

Usage:
  python3 hyperliquid-trigger-battery.py --dry-run
  python3 hyperliquid-trigger-battery.py --window-days 7
  python3 hyperliquid-trigger-battery.py --start 2026-07-28 --end 2026-08-03

Flags:
  --dry-run            fetch + compute only; print what would be inserted
  --no-write-signals   skip the signal_journeys insert (candles still written)
  --no-write-candles   skip the candles upsert (signals still written)
  --window-days N      lookback window in days (default 7)
  --start / --end      ISO overrides for the window (end defaults to now)
  --json PATH          write a JSON artifact of what was generated/inserted
  --symbols S1,S2,...  bare-ticker symbols (default BTC,ETH,SOL,AVAX,BNB)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd
import numpy as np
import psycopg2
from psycopg2.extras import execute_values

# Reuse the canonical PG config + symbol constants from the shared row-set
# module so the battery talks to the same DB the gate/IC test read.
from microstructure_shared import PG_CONFIG, HYPERLIQUID_SYMBOLS


def refresh_in_window_battery_rows(cur, symbols, start, end, signal_rows, now=None):
    """Delete ALL in-window hl-battery-* rows, then insert `signal_rows`.

    Caller owns the transaction. `signal_rows` may be empty: deletion still
    runs so a valid window with zero triggers cannot leave last tick's rows
    in place for the paired-sample gate to count as current.
    """
    now = now or datetime.now(timezone.utc)
    cur.execute(
        "DELETE FROM signal_journeys "
        "WHERE signal_id LIKE 'hl-battery-%%' "
        "AND symbol = ANY(%s) "
        "AND triggered_at >= %s AND triggered_at < %s",
        (list(symbols), start, end),
    )
    deleted = cur.rowcount
    insert_sql = """
        INSERT INTO signal_journeys (
            id, correlation_id, signal_id, symbol, direction, timeframe,
            current_stage, is_active, entry_price, triggered_at,
            trigger_score, trigger_patterns,
            composite_confidence_score, conviction_score,
            is_hypothetical, indicator_source, capture_metadata, created_at, updated_at
        ) VALUES %s
    """
    capture_metadata = json.dumps({
        "source": "hl-trigger-battery",
        "task": "t_db9814a1",
        "trigger_engine": "deterministic-15m-v1",
        "confidence_mode": "trigger-score-derived",
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
    })
    rows = []
    for r in signal_rows or []:
        rows.append((
            r["id"], r["correlation_id"], r["signal_id"], r["symbol"],
            r["direction"], r["timeframe"], "TRIGGERED", False,
            r["entry_price"], r["triggered_at"], r["trigger_score"],
            json.dumps(r["trigger_patterns"]),
            r["composite_confidence_score"], r["conviction_score"],
            True, "synthetic", capture_metadata, now, now,
        ))
    if rows:
        execute_values(cur, insert_sql, rows, page_size=1000)
    return deleted, len(rows)

API_BASE = "https://api.hyperliquid.xyz/info"

# Production trigger weights (mirror server/src/domains/signals/services/
# trigger/config.ts) so battery scores sit on the same 0-100 scale as the
# Binance battery (observed 75 / 76.38).
WEIGHTS = {
    "RSI_OVERSOLD_BOUNCE": 25,
    "RSI_OVERBOUGHT_REJECTION": 10,
    "VOLUME_SPIKE_UP": 25,
    "VOLUME_SPIKE_DOWN": 5,
    "EMA_GOLDEN_CROSS": 15,
    "EMA_DEATH_CROSS": 15,
    "PRICE_BREAK_RESISTANCE": 30,
    "PRICE_BREAK_SUPPORT": 30,
}
RSI_PERIOD = 14
EMA_FAST = 34
EMA_SLOW = 100
VOL_LOOKBACK = 20
BREAKOUT_LOOKBACK = 20
BREAKOUT_CONFIRMATION = 0.5  # Production breakoutConfirmation 0.5% (config.ts:33)
VOL_SPIKE_MULT = 2.0


def fetch_candles(symbol: str, interval: str, start_ms: int, end_ms: int,
                  api_base: str = API_BASE, retries: int = 3) -> pd.DataFrame:
    """Fetch HL candleSnapshot for one coin into a DataFrame.

    HL returns open-stamped candles: t = bar open (ms), T = bar close (ms).
    Numbers arrive as strings; converted to float64. Retries with backoff so a
    transient HL API timeout fails the run visibly AFTER the retries are
    exhausted, but never leaves a partial DB write (fetch happens first).
    """
    import time as _time
    import urllib.request

    body = json.dumps({
        "type": "candleSnapshot",
        "req": {"coin": symbol, "interval": interval,
                "startTime": int(start_ms), "endTime": int(end_ms)},
    }).encode()
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                api_base, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HL API {resp.status} for {symbol} {interval}")
                raw = json.loads(resp.read().decode())
            break
        except Exception as exc:  # noqa: BLE001 -- retry any transient transport error
            last_err = exc
            if attempt < retries:
                _time.sleep(2.0 * attempt)
    else:
        raise RuntimeError(f"HL API fetch failed for {symbol} {interval} "
                           f"after {retries} attempts: {last_err}")
    if not isinstance(raw, list) or len(raw) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df["T"] = pd.to_datetime(df["T"], unit="ms", utc=True)
    for col in ("o", "h", "l", "c", "v"):
        df[col] = df[col].astype(float)
    return df.sort_values("t").reset_index(drop=True)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add RSI(14) Wilder, EMA34/100, volume SMA20, 20-bar high/low."""
    out = df.copy()

    # Wilder RSI
    delta = out["c"].diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / RSI_PERIOD, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    rsi_raw = (100.0 - 100.0 / (1.0 + rs.to_numpy(dtype=float)))
    out["rsi"] = pd.Series(
        np.where(np.isnan(rsi_raw), 50.0, rsi_raw), index=out.index)

    out["ema_fast"] = out["c"].ewm(span=EMA_FAST, adjust=False).mean()
    out["ema_slow"] = out["c"].ewm(span=EMA_SLOW, adjust=False).mean()
    out["vol_sma"] = out["v"].rolling(VOL_LOOKBACK).mean()
    out["prev_high20"] = out["h"].rolling(BREAKOUT_LOOKBACK).max().shift(1)
    out["prev_low20"] = out["l"].rolling(BREAKOUT_LOOKBACK).min().shift(1)
    return out


def detect_triggers(df: pd.DataFrame) -> pd.DataFrame:
    """One signal per (symbol, 15m bar) with the max-scoring direction.

    Returns rows with: bar_open, triggered_at, direction, trigger_score,
    trigger_patterns, entry_price.
    """
    d = add_indicators(df)
    prev = d.shift(1)

    rows = []
    for i in range(len(d)):
        if i < EMA_SLOW:  # EMA100 warmup -- no triggers on immature series
            continue
        row = d.iloc[i]
        p = prev.iloc[i]

        long_triggers: list[str] = []
        short_triggers: list[str] = []

        if pd.notna(p["rsi"]) and p["rsi"] <= 30 and row["rsi"] > 35:
            long_triggers.append("RSI_OVERSOLD_BOUNCE")
        if pd.notna(p["rsi"]) and p["rsi"] >= 70 and row["rsi"] < 65:
            short_triggers.append("RSI_OVERBOUGHT_REJECTION")

        vol_ok = pd.notna(row["vol_sma"]) and row["vol_sma"] > 0 and \
            row["v"] > VOL_SPIKE_MULT * row["vol_sma"]
        if vol_ok and row["c"] > row["o"]:
            long_triggers.append("VOLUME_SPIKE_UP")
        elif vol_ok and row["c"] < row["o"]:
            short_triggers.append("VOLUME_SPIKE_DOWN")

        if pd.notna(p["ema_fast"]) and pd.notna(p["ema_slow"]):
            if p["ema_fast"] <= p["ema_slow"] and row["ema_fast"] > row["ema_slow"]:
                long_triggers.append("EMA_GOLDEN_CROSS")
            if p["ema_fast"] >= p["ema_slow"] and row["ema_fast"] < row["ema_slow"]:
                short_triggers.append("EMA_DEATH_CROSS")

        # Production breakoutConfirmation: close must exceed prev_high20 by
        # >= 0.5% (not just any close beyond the 20-bar high). Mirrors
        # patternDetector.ts breakoutLevel = resistance * (1 + 0.005).
        if pd.notna(row["prev_high20"]):
            breakout_level = row["prev_high20"] * (1.0 + BREAKOUT_CONFIRMATION / 100.0)
            if row["c"] >= breakout_level:
                long_triggers.append("PRICE_BREAK_RESISTANCE")
        if pd.notna(row["prev_low20"]):
            breakdown_level = row["prev_low20"] * (1.0 - BREAKOUT_CONFIRMATION / 100.0)
            if row["c"] <= breakdown_level:
                short_triggers.append("PRICE_BREAK_SUPPORT")

        long_score = sum(WEIGHTS[t] for t in long_triggers)
        short_score = sum(WEIGHTS[t] for t in short_triggers)
        if long_score + short_score == 0:
            continue

        direction = "LONG" if long_score >= short_score else "SHORT"
        score = max(long_score, short_score)
        patterns = long_triggers if direction == "LONG" else short_triggers

        # Triggered at the bar close minus 1s so a backward tape join lands on
        # the trigger bar's final minute (still within the 5m gate tolerance).
        triggered_at = row["T"] - timedelta(seconds=1)
        rows.append({
            "symbol": row["s"],
            "bar_open": row["t"],
            "triggered_at": triggered_at,
            "direction": direction,
            "trigger_score": min(100.0, float(score)),
            "trigger_patterns": patterns,
            "entry_price": float(row["c"]),
        })
    return pd.DataFrame(rows)


def confidence_from_score(score: float) -> tuple[float, float]:
    """Deterministic battery confidence (non-null for the complete-case filter).

    Mirrors the Binance convention conviction_score = composite * 100 but is
    explicitly NOT the production confidence model -- see capture_metadata
    confidence_mode='trigger-score-derived'.
    """
    composite = round(max(0.50, min(0.95, 0.50 + score / 200.0)), 4)
    conviction = round(composite * 100.0, 2)
    return composite, conviction


def iso(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def run_battery(symbols, start, end, write_signals=True, write_candles=True,
                api_base=API_BASE, dry_run=False):
    """Fetch, detect, and (unless dry-run) write candles + signal_journeys."""
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    
    # Fetch ≥100 pre-window 15m bars for indicator warmup (EMA100=100 bars,
    # RSI=14, vol SMA=20, breakout=20). extend_start ensures the fetch includes
    # enough pre-window bars; signals are still emitted only within [start, end).
    extend_start = start - timedelta(minutes=15 * 100)
    
    start_ms = int(extend_start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    signal_rows: list[dict] = []
    candle_rows: list[tuple] = []
    per_symbol = {}

    for sym in symbols:
        candles15 = fetch_candles(sym, "15m", start_ms, end_ms, api_base)
        candles1h = fetch_candles(sym, "1h", start_ms, end_ms, api_base)
        if len(candles15) == 0:
            print(f"[battery] {sym}: no 15m candles in window -- skipping")
            per_symbol[sym] = {"candles_15m": 0, "signals": 0}
            continue

        # detect_triggers on the extended fetch; filter signals to [start, end)
        sigs = detect_triggers(candles15)
        sigs_in_window = sigs[
            (sigs["triggered_at"] >= start) & (sigs["triggered_at"] < end)
        ]
        n_in_window = len(sigs_in_window)
        per_symbol[sym] = {
            "candles_15m": len(candles15),
            "candles_1h": len(candles1h),
            "signals": n_in_window,
        }
        for _, r in sigs_in_window.iterrows():
            comp, conv = confidence_from_score(r["trigger_score"])
            marker = f"hl-battery-{r['symbol']}-{iso(r['bar_open'])}"
            signal_rows.append({
                "id": str(uuid.uuid4()),
                "correlation_id": marker,
                "signal_id": marker,
                "symbol": r["symbol"],
                "direction": r["direction"],
                "timeframe": "15m",
                "trigger_score": r["trigger_score"],
                "trigger_patterns": r["trigger_patterns"],
                "entry_price": r["entry_price"],
                "triggered_at": r["triggered_at"],
                "composite_confidence_score": comp,
                "conviction_score": conv,
            })
        for _, r in candles1h.iterrows():
            candle_rows.append((
                r["s"], "1h", r["t"].to_pydatetime(),
                r["o"], r["h"], r["l"], r["c"], r["v"],
            ))

    print("=== Hyperliquid trigger battery ===")
    print(f"Window:           {start.isoformat()} -> {end.isoformat()}")
    print(f"Symbols:          {','.join(symbols)}")
    print(f"Signals detected: {len(signal_rows)}")
    for sym in symbols:
        info = per_symbol.get(sym, {})
        print(f"  {sym:6s} 15m={info.get('candles_15m', 0):4d} "
              f"1h={info.get('candles_1h', 0):4d} signals={info.get('signals', 0):4d}")

    if dry_run:
        print("\n[dry-run] No DB writes. Sample signals:")
        for r in signal_rows[:8]:
            print(f"  {r['symbol']:6s} {r['direction']:5s} score={r['trigger_score']:5.1f} "
                  f"at={r['triggered_at'].isoformat()} "
                  f"patterns={','.join(r['trigger_patterns'])}")
        return signal_rows, candle_rows

    conn = psycopg2.connect(**PG_CONFIG)
    try:
        if write_candles and candle_rows:
            cur = conn.cursor()
            upsert_candles = """
                INSERT INTO candles (symbol, timeframe, timestamp, open, high, low, close, volume)
                VALUES %s
                ON CONFLICT (symbol, timeframe, timestamp) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume
            """
            execute_values(cur, upsert_candles, candle_rows, page_size=1000)
            conn.commit()
            cur.close()
            print(f"[battery] Upserted {len(candle_rows)} REAL 1h candles "
                  f"(candles table, bare-ticker symbols)")

        if write_signals:
            cur = conn.cursor()
            # Explicit refresh transaction: ALWAYS delete the prior in-window
            # hl-battery-* population, including the zero-trigger case, then
            # insert the new rows (possibly empty). Stale prior rows must not
            # survive a successful empty refresh.
            deleted, inserted = refresh_in_window_battery_rows(
                cur, symbols, start, end, signal_rows,
            )
            conn.commit()
            cur.close()
            print(f"[battery] signal_journeys: removed {deleted} prior battery "
                  f"rows, inserted {inserted} fresh rows (is_active=false, "
                  f"is_hypothetical=true, signal_id='hl-battery-*')")
        conn.close()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    return signal_rows, candle_rows


def main():
    ap = argparse.ArgumentParser(description="Hyperliquid trigger battery (t_db9814a1)")
    ap.add_argument("--symbols", default=",".join(HYPERLIQUID_SYMBOLS))
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("--start", default=None, help="ISO start (default: end - window-days)")
    ap.add_argument("--end", default=None, help="ISO end (default: now)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-write-signals", action="store_true")
    ap.add_argument("--no-write-candles", action="store_true")
    ap.add_argument("--json", default=None, help="JSON artifact path")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00")) if args.end else \
        datetime.now(timezone.utc)
    start = datetime.fromisoformat(args.start.replace("Z", "+00:00")) if args.start else \
        end - timedelta(days=args.window_days)

    t0 = time.time()
    signal_rows, candle_rows = run_battery(
        symbols, start, end,
        write_signals=not args.no_write_signals,
        write_candles=not args.no_write_candles,
        dry_run=args.dry_run,
    )
    elapsed = time.time() - t0
    print(f"\n[battery] done in {elapsed:.1f}s. "
          f"{len(signal_rows)} signals, {len(candle_rows)} 1h candles "
          f"({'DRY-RUN no writes' if args.dry_run else 'written'}).")

    if args.json:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dry_run": args.dry_run,
            "symbols": symbols,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "n_signals": len(signal_rows),
            "n_candles_1h": len(candle_rows),
            "marker": "signal_id LIKE 'hl-battery-%'",
            "is_active": False,
            "is_hypothetical": True,
            "sample": [
                {k: (v.isoformat() if isinstance(v, datetime) else v)
                 for k, v in r.items() if k != "id"}
                for r in signal_rows[:5]
            ],
        }
        with open(args.json, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        print(f"[battery] JSON artifact: {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
