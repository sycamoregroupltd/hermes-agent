#!/usr/bin/env python3
# sycode_4h_refresh.py — recurring 4h-candle freshness repair (t_1ff283a8).
#
# ROOT CAUSE (verified 2026-07-14): the live candle ingestion writer
# (CandleIngestionService) only advances `4h` for ~10 majors; the broader
# universe (~463 symbols) relies on a backfill that froze at 2026-07-10..12.
# Result: 4h freshness = 10/473 vs expected floor 440. 15m is live for 259
# symbols, proving the gap is 4h-writer-specific, not a network/feed outage.
#
# This script closes the gap by refreshing the LATEST closed 4h bar for every
# stuck symbol directly from Binance kline REST (per-symbol, small, idempotent).
# It does NOT do a full history backfill (that is a follow-up card). It only
# tops up the current bar so the freshness probe reports fresh.
#
# EXECUTION MODEL (matches existing no-agent monitors): run via host terminal
# using host-local psql (127.0.0.1:5432 reachable on host) and Binance public
# REST. Read-only fetch + idempotent upsert (ON CONFLICT DO NOTHING). No schema
# migration, no deploy, no credential change — pure data repair in-gate.
#
# Set SYCODE_4H_DRYRUN=1 to report planned actions without writing.

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# Force unbuffered stdout so progress is visible in cron logs.
reconfigure_stdout = getattr(sys.stdout, "reconfigure", None)
if reconfigure_stdout is not None:
    reconfigure_stdout(line_buffering=True)

# Parallel fetch workers (Binance allows 1200 req/min; 16 is conservative).
WORKERS = 16

DB = "postgres"
USER = "postgres"
PGHOST = os.environ.get("PGHOST", "localhost")
PGPORT = os.environ.get("PGPORT", "5432")
DRYRUN = os.environ.get("SYCODE_4H_DRYRUN") == "1"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_EXCHANGE_INFO_URL = (
    "https://api.binance.com/api/v3/exchangeInfo?permissions=SPOT&showPermissionSets=false"
)
MIN_BINANCE_ACTIVE_USDT_SYMBOLS = 300

# How far back a symbol's newest 4h bar must be before we consider it "stuck".
# 4h bars close every 4h; we refresh anything older than 4.5h.
STUCK_OLDER_THAN_HOURS = 4.5
# A Binance 4h bar older than this (days) means the pair is delisted / no longer
# trades on Binance — it can NEVER be fresh. Skip it so we never insert fake
# historical data; these should be excluded from the freshness floor instead.
DELISTED_MAX_AGE_DAYS = 2


def psql(query, read_only=True):
    cmd = [
        "psql",
        "-h", PGHOST,
        "-p", PGPORT,
        "-U", USER,
        "-d", DB,
        "-X", "-q", "-t", "-A",
        "-v", "ON_ERROR_STOP=1",
        "-c", query,
    ]
    env = os.environ.copy()
    if read_only:
        env["PGOPTIONS"] = "-c default_transaction_read_only=on"
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"psql rc={r.returncode}: {r.stderr.strip()[:200]}")
    return r.stdout


def get_stuck_symbols():
    q = (
        "SELECT symbol FROM public.candles WHERE timeframe='4h' GROUP BY symbol "
        f"HAVING max(\"timestamp\") < now() - interval '{STUCK_OLDER_THAN_HOURS} hours' "
        "ORDER BY symbol;"
    )
    out = psql(q, read_only=True)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def build_klines_url(symbol, interval="4h", limit=2):
    """Build a Binance klines URL with RFC 3986 query encoding.

    Binance accepts non-ASCII symbols such as 币安人生USDT when the symbol query
    parameter is percent-encoded. Interpolating the raw symbol into the URL can
    raise before any HTTP request is made or produce an invalid request path.
    """
    query = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    return f"{BINANCE_KLINES_URL}?{query}"


def fetch_latest_4h(symbol):
    url = build_klines_url(symbol, interval="4h", limit=2)
    req = urllib.request.Request(url, headers={"User-Agent": "sycode-4h-refresh/1.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    # Each bar: [openTime, o, h, l, c, v, closeTime, qv, trades, tbv, tbqv, ignore]
    bars = []
    for b in data:
        bars.append({
            "openTime": int(b[0]),
            "closeTime": int(b[6]),
            "open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5],
        })
    return bars


def parse_active_binance_spot_symbols(payload):
    """Extract currently tradeable Binance spot-USDT symbols from exchangeInfo."""
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, list):
        raise RuntimeError("Binance exchangeInfo missing symbols list")
    return {
        row["symbol"]
        for row in symbols
        if isinstance(row, dict)
        and isinstance(row.get("symbol"), str)
        and row.get("status") == "TRADING"
        and row.get("quoteAsset") == "USDT"
        and row.get("isSpotTradingAllowed", True) is not False
    }


def fetch_active_binance_spot_symbols():
    request = urllib.request.Request(
        BINANCE_EXCHANGE_INFO_URL,
        headers={"User-Agent": "sycode-4h-refresh/1.1"},
    )
    with urllib.request.urlopen(request, timeout=45) as resp:
        payload = json.load(resp)
    active = parse_active_binance_spot_symbols(payload)
    if len(active) < MIN_BINANCE_ACTIVE_USDT_SYMBOLS:
        raise RuntimeError(
            "Binance active spot-USDT universe unexpectedly small: "
            f"{len(active)} < {MIN_BINANCE_ACTIVE_USDT_SYMBOLS}"
        )
    return active


def select_refresh_targets(stuck_symbols, active_spot_symbols):
    """Return stuck symbols that are still tradeable Binance spot-USDT pairs."""
    return [symbol for symbol in stuck_symbols if symbol in active_spot_symbols]


def upsert(symbol, bar):
    ts = time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime(bar["closeTime"] / 1000.0))
    q = (
        "INSERT INTO candles (symbol, timeframe, timestamp, open, high, low, close, volume) "
        f"VALUES ('{symbol}', '4h', '{ts}'::timestamptz, {bar['open']}::numeric, {bar['high']}::numeric, "
        f"{bar['low']}::numeric, {bar['close']}::numeric, {bar['volume']}::numeric) "
        "ON CONFLICT (symbol, timeframe, timestamp) DO NOTHING;"
    )
    if DRYRUN:
        print(f"  [dry-run] would upsert {symbol} 4h @ {ts}")
        return True
    psql(q, read_only=False)
    return True


def latest_refreshable_bar(symbol, bars, now_ms=None):
    """Return (bar, error) for the newest non-delisted 4h bar.

    This pure helper keeps delisted/legacy-symbol behavior regression-testable:
    if Binance's newest 4h bar is too old, the caller must skip the symbol and
    must not insert an artificial/current row for it.
    """
    if not bars:
        return None, "no bars"
    bar = bars[-1]
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    age_days = (now_ms - bar["closeTime"]) / 86_400_000.0
    if age_days > DELISTED_MAX_AGE_DAYS:
        return None, f"delisted (bar {age_days:.0f}d old)"
    return bar, None


def refresh_one(sym):
    try:
        bars = fetch_latest_4h(sym)
    except Exception as e:
        return sym, False, f"fetch: {str(e)[:80]}"
    bar, err = latest_refreshable_bar(sym, bars)
    if err:
        return sym, False, err
    upsert(sym, bar)
    return sym, True, None


def main():
    print(f"sycode_4h_refresh {'[DRYRUN]' if DRYRUN else ''} @ {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime())}", flush=True)
    symbols = get_stuck_symbols()
    stuck_count = len(symbols)
    print(f"stuck 4h symbols: {stuck_count}", flush=True)
    active_spot_symbols = fetch_active_binance_spot_symbols()
    symbols = select_refresh_targets(symbols, active_spot_symbols)
    print(
        "refreshable active Binance spot-USDT symbols: "
        f"{len(symbols)} (skipped inactive/non-USDT/malformed={stuck_count - len(symbols)})",
        flush=True,
    )
    refreshed = 0
    errors = 0
    err_samples = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(refresh_one, s): s for s in symbols}
        done = 0
        for fut in as_completed(futures):
            done += 1
            try:
                sym, ok, err = fut.result()
                if ok:
                    refreshed += 1
                else:
                    errors += 1
                    if err and len(err_samples) < 10:
                        err_samples.append(f"{sym}: {err}")
            except Exception as e:
                errors += 1
                sym = futures[fut]
                if len(err_samples) < 10:
                    err_samples.append(f"{sym}: {str(e)[:120]}")
            if done % 100 == 0:
                print(f"  progress {done}/{len(symbols)} refreshed={refreshed} errors={errors}", flush=True)
    for e in err_samples:
        print(f"  ERROR {e}", flush=True)
    print(f"done: refreshed={refreshed} errors={errors}", flush=True)
    # Exit non-zero on errors so a wrapping cron can alert.
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
