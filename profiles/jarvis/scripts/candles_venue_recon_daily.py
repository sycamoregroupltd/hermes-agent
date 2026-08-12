#!/usr/bin/env python3
"""
candles-venue-recon-daily — sampled venue reconciliation for public.candles
(t_6900c048; sibling of the H2 flush-aftermath venue verification that caught
live-ingest 2026 bars mislabeled by one interval, up to 4.8% price / 155%
volume error at the research join).

Runs as a hermes no_agent cron (deployed copy: ~/.hermes/scripts/, canonical
copy reviewed in-repo at scripts/monitoring/). Contract (no_agent doctrine):
stdout EMPTY on a clean day; ALERT lines otherwise; any operational failure
reaches the exit code. Never fabricate a green result.

Checks, all SELECT-only:
  C1 CONVENTION  any row written in the last LOOKBACK_H hours whose timestamp
                 is not aligned to its timeframe grid (canonical convention:
                 timestamp = bar OPEN time, UTC). A regression of the
                 storeCandle fix or a new mis-stamped writer trips this.
  C2 VENUE       N random rows from the window, OHLC compared against Binance
                 klines (spot, then USDT-M futures fallback — same venue order
                 as the ingest backfill). Price mismatch > PRICE_TOL or volume
                 mismatch > VOL_TOL alerts.
  C3 LIVENESS    zero candles rows written in the window at all = dead ingest.
"""
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

LOOKBACK_H = 48
SAMPLE_N = 30
PRICE_TOL = 0.001      # 0.1% max OHLC relative error vs venue
VOL_TOL = 0.02         # 2% volume relative error (venue trims dust asymmetrically)
IV_SECS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1D": 86400}
IV_API = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1D": "1d"}

alerts = []


def psql(query: str):
    env = dict(os.environ, PGPASSFILE=os.path.expanduser("~/.pgpass"))
    out = subprocess.run(
        ["psql", "-h", "localhost", "-U", "postgres", "-d", "postgres",
         "-At", "-F", "\t", "-c", query],
        capture_output=True, text=True, env=env, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr.strip()[:400]}")
    return [line.split("\t") for line in out.stdout.splitlines() if line]


def fetch_venue_bar(symbol: str, timeframe: str, open_ms: int):
    """Binance kline opening exactly at open_ms; spot first, futures fallback."""
    for base in ("https://api.binance.com/api/v3/klines",
                 "https://fapi.binance.com/fapi/v1/klines"):
        url = (f"{base}?symbol={symbol}&interval={IV_API[timeframe]}"
               f"&startTime={open_ms}&limit=1")
        k = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=15) as resp:
                    k = json.load(resp)
                break
            except urllib.error.HTTPError as e:
                if e.code in (400, 404, 451):
                    k = []
                    break
                time.sleep(2 * (attempt + 1))
            except Exception:
                time.sleep(2 * (attempt + 1))
        if k is None:
            raise RuntimeError(f"venue fetch failed for {symbol} {timeframe} @ {open_ms}")
        time.sleep(0.1)
        if k and int(k[0][0]) == open_ms:
            return [float(x) for x in k[0][1:6]]
    return None


def rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(b), 1e-12)


def main() -> None:
    now = datetime.now(timezone.utc)

    # C3 liveness + C1 convention in one pass
    rows = psql(f"""
        WITH iv AS (SELECT * FROM (VALUES ('1m',60),('5m',300),('15m',900),
                     ('1h',3600),('4h',14400),('1D',86400)) AS t(tf, secs))
        SELECT count(*),
               count(*) FILTER (WHERE (extract(epoch FROM c.timestamp)::numeric % iv.secs) <> 0)
        FROM candles c JOIN iv ON iv.tf = c.timeframe
        WHERE c.timestamp >= now() - interval '{LOOKBACK_H} hours'
    """)
    total, misaligned = int(rows[0][0]), int(rows[0][1])
    if total == 0:
        alerts.append(f"ALERT C3 LIVENESS: 0 candles rows in the last {LOOKBACK_H}h — ingest dead")
    if misaligned > 0:
        alerts.append(
            f"ALERT C1 CONVENTION: {misaligned}/{total} rows in the last {LOOKBACK_H}h "
            f"are not grid-aligned (timestamp must be bar OPEN, UTC) — mis-stamping writer regressed")

    # C2 sampled venue reconciliation (only rows old enough to be closed bars)
    sample = psql(f"""
        SELECT symbol, timeframe, extract(epoch FROM timestamp)::numeric,
               open, high, low, close, volume
        FROM candles
        WHERE timestamp >= now() - interval '{LOOKBACK_H} hours'
          AND timestamp < now() - interval '2 hours'
        ORDER BY random() LIMIT {SAMPLE_N * 3}
    """)
    random.shuffle(sample)
    checked = 0
    for sym, tf, epoch, o, h, l, c, v in sample:
        if checked >= SAMPLE_N:
            break
        secs = IV_SECS.get(tf)
        if not secs:
            continue
        open_s = float(epoch)
        if open_s % secs != 0:
            continue  # already alerted by C1; venue join undefined
        # skip bars not yet closed
        if open_s + secs > now.timestamp() - 60:
            continue
        venue = fetch_venue_bar(sym, tf, int(open_s * 1000))
        checked += 1
        if venue is None:
            alerts.append(f"ALERT C2 VENUE: {sym} {tf} @ {datetime.fromtimestamp(open_s, timezone.utc):%Y-%m-%d %H:%M} "
                          f"has no venue bar on spot or futures")
            continue
        vo, vh, vl, vc, vv = venue
        perr = max(rel(float(o), vo), rel(float(h), vh), rel(float(l), vl), rel(float(c), vc))
        verr = rel(float(v), vv)
        if perr > PRICE_TOL or verr > VOL_TOL:
            alerts.append(
                f"ALERT C2 VENUE: {sym} {tf} @ {datetime.fromtimestamp(open_s, timezone.utc):%Y-%m-%d %H:%M} "
                f"price_err={perr:.4%} vol_err={verr:.4%} (db o={o} c={c} vs venue o={vo} c={vc})")

    if total > 0 and checked == 0:
        alerts.append(f"ALERT C2 VENUE: 0 of {len(sample)} sampled rows were checkable against venue")

    for line in alerts:
        print(line)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # operational failure must reach the exit code
        print(f"ALERT MONITOR-FAILURE: {exc}")
        sys.exit(1)
