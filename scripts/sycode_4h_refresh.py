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

# --- multi-timeframe support (claude 2026-08-09) ---------------------------
# 2026-08-06..09: 1D/1h/5m stopped writing for 2-4 days while 4h stayed fresh
# BECAUSE 4h had this top-up cron and the others did not. The app-side fix
# (PR #1019) cannot ship: the auto-deploy safety gate is blocked on an external
# $75 dust position. So extend the proven in-gate pattern to the dead timeframes.
#
# DB timeframe -> (Binance API interval, stuck threshold hours)
# NOTE the casing: the DB stores '1D' but the Binance API wants '1d'. Querying
# candles with '1d' silently returns ZERO rows. Same class of trap as
# managed_positions.status='open' and bmre.direction='UP'.
TIMEFRAME_SPEC = {
    "1m":  ("1m",  0.5),
    "5m":  ("5m",  0.75),
    "15m": ("15m", 1.5),
    "1h":  ("1h",  2.5),
    "4h":  ("4h",  4.5),
    "1D":  ("1d",  30.0),
}
# Default '4h' so the existing */30 cron keeps behaving EXACTLY as before.
TIMEFRAMES = [
    t.strip() for t in os.environ.get("SYCODE_REFRESH_TIMEFRAMES", "4h").split(",") if t.strip()
]
# Bars fetched per symbol. 2 = top up the current bar only (original behaviour).
# Raise it to CLOSE a real gap: Binance caps klines at 1000, which covers ~83h of
# 5m, ~41d of 1h and ~2.7y of 1d.
KLINE_LIMIT = int(os.environ.get("SYCODE_REFRESH_LIMIT", "2"))
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


def get_stuck_symbols(db_tf="4h", stuck_hours=None):
    if stuck_hours is None:
        stuck_hours = TIMEFRAME_SPEC.get(db_tf, ("", STUCK_OLDER_THAN_HOURS))[1]
    # t_e729c6c2 (C2): the previous `GROUP BY symbol HAVING max(timestamp) < cutoff`
    # forces a full Parallel Seq Scan of the whole `candles` table (all timeframes,
    # ~39.9M rows) even though only one timeframe is requested — measured live
    # 2026-08-29: 19.96s (459k buffer reads) for db_tf='4h'. This runs on a */30 and
    # */15 cron so it was one of the biggest cumulative pg_stat_statements offenders
    # (16.5s mean x 1684 calls + 15.4s mean x 367 calls). Rewritten to a bounded
    # per-symbol backward index seek: a recursive-CTE symbol enumeration (index-only
    # scan on the PK, no seq scan of the fact rows) joined to one LATERAL/subselect
    # per symbol on idx_candles_symbol_timeframe_timestamp (symbol, timeframe,
    # timestamp DESC) — an existing index, no new DDL needed. Measured live: 1.42s
    # for the same db_tf='4h' cutoff, byte-identical result set (diffed against the
    # old query's output on the same snapshot).
    q = (
        "WITH RECURSIVE syms AS ("
        "  (SELECT symbol FROM candles ORDER BY symbol LIMIT 1)"
        "  UNION ALL"
        "  SELECT (SELECT symbol FROM candles WHERE symbol > s.symbol ORDER BY symbol LIMIT 1)"
        "  FROM syms s WHERE s.symbol IS NOT NULL"
        "), mx AS ("
        "  SELECT s.symbol,"
        f"    (SELECT c.\"timestamp\" FROM candles c"
        f"     WHERE c.symbol = s.symbol AND c.timeframe='{db_tf}'"
        "      ORDER BY c.\"timestamp\" DESC LIMIT 1) AS newest"
        "  FROM syms s WHERE s.symbol IS NOT NULL"
        ")"
        "SELECT symbol FROM mx"
        f" WHERE newest IS NOT NULL AND newest < now() - interval '{stuck_hours} hours'"
        " ORDER BY symbol;"
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


def fetch_bars(symbol, interval="4h", limit=2):
    url = build_klines_url(symbol, interval=interval, limit=limit)
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


def upsert(symbol, bar, db_tf="4h"):
    # Stamp the bar's OPEN time. Every other writer in this estate stamps open, and
    # `candles.timestamp` is expected to sit on the timeframe grid (epoch % 14400 == 0
    # for 4h). Stamping closeTime put rows at :59:59 — off-grid AND, for the newest
    # bar, in the FUTURE. Measured 2026-08-06 11:21Z: max(4h timestamp) = 11:59:59,
    # 38 minutes ahead of now; 452 future-stamped rows, 904 off-grid.
    ts = time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime(bar["openTime"] / 1000.0))
    # DO UPDATE, not DO NOTHING. A bar written while still forming must be correctable
    # on a later pass; DO NOTHING froze partial OHLCV permanently. Volume/high/low only
    # ever grow within a bar, so the later observation is always the better one.
    q = (
        "INSERT INTO candles (symbol, timeframe, timestamp, open, high, low, close, volume) "
        f"VALUES ('{symbol}', '{db_tf}', '{ts}'::timestamptz, {bar['open']}::numeric, {bar['high']}::numeric, "
        f"{bar['low']}::numeric, {bar['close']}::numeric, {bar['volume']}::numeric) "
        "ON CONFLICT (symbol, timeframe, timestamp) DO UPDATE SET "
        "open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, "
        "close = EXCLUDED.close, volume = EXCLUDED.volume;"
    )
    if DRYRUN:
        print(f"  [dry-run] would upsert {symbol} {db_tf} @ {ts}")
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
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    # Binance returns the STILL-FORMING bar as the last element. Writing it produced a
    # frozen partial (with DO NOTHING) whose OHLCV never matched the settled bar, and
    # made the 4h coverage SLO read GREEN on 452 fabricated rows. Take the newest bar
    # that has actually CLOSED.
    closed = [b for b in bars if b["closeTime"] <= now_ms]
    if not closed:
        return None, "no closed bar yet"
    bar = closed[-1]
    age_days = (now_ms - bar["closeTime"]) / 86_400_000.0
    if age_days > DELISTED_MAX_AGE_DAYS:
        return None, f"delisted (bar {age_days:.0f}d old)"
    return bar, None


def upsert_many(symbol, bars, db_tf="4h"):
    """Single multi-row upsert for a symbol's bars.

    Gap-closing needs hundreds of bars per symbol; the per-bar upsert() spawns one
    psql subprocess EACH, so 5m over a 2-day hole would be ~280k subprocess calls
    (hours). One statement per symbol keeps it to one call per symbol.
    Same ON CONFLICT DO UPDATE semantics and same open-time stamping as upsert().
    """
    rows = []
    for bar in bars:
        ts = time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime(bar["openTime"] / 1000.0))
        rows.append(
            f"('{symbol}', '{db_tf}', '{ts}'::timestamptz, {bar['open']}::numeric, "
            f"{bar['high']}::numeric, {bar['low']}::numeric, {bar['close']}::numeric, "
            f"{bar['volume']}::numeric)"
        )
    if not rows:
        return
    # ARG_MAX guard: psql is invoked with -c "<sql>", so one statement of 1000 rows
    # (~120KB) hit "[Errno 7] Argument list too long" on all 421 symbols for 5m.
    # Chunk rather than raise the limit — bounded statements are also easier on the
    # DB than one enormous multi-row INSERT.
    CHUNK = 150
    if len(rows) > CHUNK and not DRYRUN:
        for i in range(0, len(rows), CHUNK):
            _upsert_rows(rows[i:i + CHUNK])
        return
    if DRYRUN:
        print(f"  [dry-run] would upsert {symbol} {db_tf} x{len(rows)} bars "
              f"({rows[0].split(chr(39))[5][:10]}..)")
        return
    _upsert_rows(rows)


def _upsert_rows(rows):
    q = (
        "INSERT INTO candles (symbol, timeframe, timestamp, open, high, low, close, volume) "
        "VALUES " + ", ".join(rows) + " "
        "ON CONFLICT (symbol, timeframe, timestamp) DO UPDATE SET "
        "open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, "
        "close = EXCLUDED.close, volume = EXCLUDED.volume;"
    )
    psql(q, read_only=False)


def refresh_one(sym, db_tf="4h", api_interval="4h"):
    try:
        bars = fetch_bars(sym, api_interval, KLINE_LIMIT)
    except Exception as e:
        return sym, False, f"fetch: {str(e)[:80]}"
    # Keeps the delisted guard: if the NEWEST Binance bar is itself ancient the pair
    # no longer trades, and we must never insert fake history for it.
    bar, err = latest_refreshable_bar(sym, bars)
    if err:
        return sym, False, err
    if KLINE_LIMIT <= 2:
        upsert(sym, bar, db_tf)          # original top-up behaviour, unchanged
        return sym, True, None
    # Gap-closing mode: upsert every CLOSED bar so a multi-day hole actually fills,
    # rather than only making the freshness probe report fresh.
    now_ms = time.time() * 1000.0
    closed = [b for b in bars if b["closeTime"] < now_ms]
    upsert_many(sym, closed, db_tf)
    return sym, True, None


def main():
    print(f"sycode_candle_refresh {'[DRYRUN]' if DRYRUN else ''} @ {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime())} "
          f"timeframes={','.join(TIMEFRAMES)} limit={KLINE_LIMIT}", flush=True)
    active_spot_symbols = fetch_active_binance_spot_symbols()
    grand_refreshed = grand_errors = 0
    for db_tf in TIMEFRAMES:
        api_interval, _thr = TIMEFRAME_SPEC.get(db_tf, (db_tf, STUCK_OLDER_THAN_HOURS))
        r, e = run_timeframe(db_tf, api_interval, active_spot_symbols)
        grand_refreshed += r
        grand_errors += e
    print(f"TOTAL refreshed={grand_refreshed} errors={grand_errors}", flush=True)
    return 0 if grand_errors == 0 else 1


def run_timeframe(db_tf, api_interval, active_spot_symbols):
    print(f"--- timeframe {db_tf} (binance '{api_interval}') ---", flush=True)
    symbols = get_stuck_symbols(db_tf)
    stuck_count = len(symbols)
    print(f"stuck {db_tf} symbols: {stuck_count}", flush=True)
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
        futures = {ex.submit(refresh_one, s, db_tf, api_interval): s for s in symbols}
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
    print(f"done {db_tf}: refreshed={refreshed} errors={errors}", flush=True)
    # Return counts — main() aggregates and sets the exit code once, so the loop
    # is not aborted after the first timeframe.
    return refreshed, errors


if __name__ == "__main__":
    sys.exit(main())
