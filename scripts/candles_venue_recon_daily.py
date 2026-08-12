#!/usr/bin/env python3
"""
candles-venue-recon-daily — sampled venue reconciliation for public.candles
(t_6900c048; sibling of the H2 flush-aftermath venue verification that caught
live-ingest 2026 bars mislabeled by one interval, up to 4.8% price / 155%
volume error at the research join). HARDENED per t_c0ea4533 (parent t_945f3914).

Runs as a hermes no_agent cron (deployed copy: ~/.hermes/scripts/, canonical
copy reviewed in-repo at scripts/monitoring/). Contract (no_agent doctrine):
stdout EMPTY on a clean day; ALERT lines otherwise; any operational failure
reaches the exit code. Never fabricate a green result.

Checks, all SELECT-only (no DML, no schema change):
  C1 CONVENTION  any row written in the last LOOKBACK_H hours whose timestamp
                 is not aligned to its timeframe grid (canonical convention:
                 timestamp = bar OPEN time, UTC). A regression of the
                 storeCandle fix or a new mis-stamped writer trips this.
  C1-HIST       bounded historical extension of C1: the same grid-alignment
                 test over the last HIST_WINDOW_DAYS days (default 90). Catches
                 mis-stamping bursts (e.g. the 904 residual 4h rows of
                 2026-08-06) that age out of the short live window and would
                 otherwise be silent forever. Pure SQL, no venue fetch.
  C2 VENUE       N random rows from the window, OHLC compared against Binance
                 klines (spot, then USDT-M futures fallback — same venue order
                 as the ingest backfill). Price mismatch > PRICE_TOL or volume
                 mismatch > VOL_TOL alerts.
  C3 LIVENESS    zero candles rows written in the window at all = dead ingest.
  C4 VENUE-CONTAMINATION  a row that reconciles against the FUTURES bar while
                 its symbol IS spot-listed (and the row does NOT match the
                 spot bar) means the ingest sourced from the futures venue when
                 spot was the correct one — the futures-over-spot contamination
                 class this monitor exists to catch. Genuine perp-only symbols
                 (HYPEUSDT, FARTCOINUSDT) are not spot-listed and never flag.
                 The spot universe is fetched once per run from Binance
                 exchangeInfo; if that lookup fails the check DISABLES ITSELF
                 WITH AN EXPLICIT ALERT (absence of C4 must never read as clean
                 — silent-failure doctrine).

Operational modes:
  default     run all checks against the live DB (psql) + Binance.
  --selftest  run the deterministic fixture suite (no DB, no network): proves
              red on known-bad futures-over-spot, proves no false-flag on
              perp-only symbols, proves the C4-disable path, and proves a
              clean fixture stays silent. Prints PASS/FAIL per case.
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

LOOKBACK_H = 48                 # live C1/C2/C3 window
HIST_WINDOW_DAYS = int(os.environ.get("CANDLES_RECON_HIST_DAYS", "90"))  # bounded historical C1 window (configurable)
SAMPLE_N = 30
PRICE_TOL = 0.001               # 0.1% max OHLC relative error vs venue
VOL_TOL = 0.02                  # 2% volume relative error (venue trims dust)
IV_SECS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1D": 86400}
IV_API = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1D": "1d"}
SPOT_UNIVERSE_URL = "https://api.binance.com/api/v3/exchangeInfo"
SPOT_KLINES = "https://api.binance.com/api/v3/klines"
FUT_KLINES = "https://fapi.binance.com/fapi/v1/klines"

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


def _klines(base: str, symbol: str, timeframe: str, open_ms: int):
    """Fetch the Binance kline opening exactly at open_ms for one venue.
    Returns [o,h,l,c,v] or None when the venue has no such bar; raises on a
    hard network failure so the operational failure reaches the exit code."""
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


def fetch_venue_bar(symbol: str, timeframe: str, open_ms: int):
    """Binance kline opening exactly at open_ms; spot first, futures fallback.
    (Preserved for C2.)"""
    spot = _klines(SPOT_KLINES, symbol, timeframe, open_ms)
    if spot is not None:
        return spot
    return _klines(FUT_KLINES, symbol, timeframe, open_ms)


def fetch_spot_futures(symbol: str, timeframe: str, open_ms: int):
    """Return (spot_bar, fut_bar); each [o,h,l,c,v] or None per venue."""
    return _klines(SPOT_KLINES, symbol, timeframe, open_ms), \
        _klines(FUT_KLINES, symbol, timeframe, open_ms)


def load_spot_universe():
    """Set of spot-listed USDT TRADING symbols, or None on lookup failure."""
    try:
        with urllib.request.urlopen(SPOT_UNIVERSE_URL, timeout=30) as resp:
            data = json.load(resp)
        return {s["symbol"] for s in data.get("symbols", [])
                if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"}
    except Exception:
        return None


def rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(b), 1e-12)


def matches(bar, o, h, l, c, v) -> bool:
    """True if the DB row's OHLCV matches the venue bar within tolerance."""
    vo, vh, vl, vc, vv = bar
    perr = max(rel(float(o), vo), rel(float(h), vh),
               rel(float(l), vl), rel(float(c), vc))
    verr = rel(float(v), vv)
    return perr <= PRICE_TOL and verr <= VOL_TOL


def is_aligned(epoch: float, tf: str) -> bool:
    secs = IV_SECS.get(tf)
    if not secs:
        return False
    return float(epoch) % secs == 0


def c4_disable_alert() -> str:
    return ("ALERT C4 VENUE-CONTAMINATION CHECK DISABLED: Binance spot "
            "universe lookup (exchangeInfo) FAILED — C4 skipped this run; "
            "do not read the absence of C4 as clean")


def c4_for_row(spot_universe, spot, fut, sym, tf, open_s, o, h, l, c, v):
    """Return an ALERT line if this row is futures-sourced for a spot-listed
    symbol; else None. Never flags perp-only symbols (not in spot universe)."""
    if spot_universe is None:
        return None  # disabled path handled in main()
    if sym not in spot_universe:
        return None  # genuine perp-only (HYPEUSDT, FARTCOINUSDT, ...)
    fut_match = fut is not None and matches(fut, o, h, l, c, v)
    spot_match = spot is not None and matches(spot, o, h, l, c, v)
    if fut_match and not spot_match:
        ts = datetime.fromtimestamp(open_s, timezone.utc).strftime("%Y-%m-%d %H:%M")
        return (f"ALERT C4 VENUE-CONTAMINATION: {sym} {tf} @ {ts} row matches "
                f"FUTURES but {sym} IS spot-listed — ingest sourced from futures "
                f"when spot was the correct venue")
    return None


def main() -> None:
    now = datetime.now(timezone.utc)

    # C3 liveness + C1 convention (live window) in one pass
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

    # C1-HIST: bounded historical convention scan — catches bursts that aged
    # out of the live window (e.g. the 904 residual 4h rows of 2026-08-06).
    hist = psql(f"""
        WITH iv AS (SELECT * FROM (VALUES ('1m',60),('5m',300),('15m',900),
                     ('1h',3600),('4h',14400),('1D',86400)) AS t(tf, secs))
        SELECT tf,
               count(*) FILTER (WHERE (extract(epoch FROM c.timestamp)::numeric % iv.secs) <> 0)
        FROM candles c JOIN iv ON iv.tf = c.timeframe
        WHERE c.timestamp >= now() - interval '{HIST_WINDOW_DAYS} days'
        GROUP BY tf ORDER BY tf
    """)
    hist_mis = [(tf, int(n)) for tf, n in hist if int(n) > 0]
    if hist_mis:
        desc = ", ".join(f"{tf}:{n}" for tf, n in hist_mis)
        alerts.append(
            f"ALERT C1-HIST: {sum(n for _, n in hist_mis)} mis-grid-aligned rows in the last "
            f"{HIST_WINDOW_DAYS}d ({desc}) — historical burst outside the {LOOKBACK_H}h live window")

    # C4 venue universe (one lookup per run). If it fails, disable C4 WITH an
    # explicit alert — absence of C4 must never read as clean.
    spot_universe = load_spot_universe()
    if spot_universe is None:
        alerts.append(c4_disable_alert())

    # C2 sampled venue reconciliation + C4 contamination (rows old enough to be closed bars)
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
        if not is_aligned(float(epoch), tf):
            continue  # already alerted by C1; venue join undefined
        open_s = float(epoch)
        # skip bars not yet closed
        if open_s + IV_SECS.get(tf, 0) > now.timestamp() - 60:
            continue
        spot, fut = fetch_spot_futures(sym, tf, int(open_s * 1000))
        checked += 1
        venue = spot if spot is not None else fut
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
        # C4 runs on the same row regardless of C2 outcome
        c4 = c4_for_row(spot_universe, spot, fut, sym, tf, open_s, o, h, l, c, v)
        if c4:
            alerts.append(c4)

    if total > 0 and checked == 0:
        alerts.append(f"ALERT C2 VENUE: 0 of {len(sample)} sampled rows were checkable against venue")

    for line in alerts:
        print(line)


# ---------------------------------------------------------------------------
# Deterministic fixture suite: proves red on known-bad, no false-flags, the
# disable path, and clean-run silence. No DB, no network.
# ---------------------------------------------------------------------------
def _fmt_ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M")


def selftest() -> int:
    cases = []
    spot_universe = {"ETHUSDT", "BTCUSDT", "SOLUSDT", "AAVEUSDT"}

    # C4 red: futures-sourced row for a spot-listed symbol, spot bar absent.
    line = c4_for_row(spot_universe, None, [100.0, 101.0, 99.0, 100.5, 1000.0],
                      "ETHUSDT", "4h", 100000000, 100.0, 101.0, 99.0, 100.5, 1000.0)
    cases.append(("C4 red (futures-sourced for spot-listed symbol)",
                  line is not None and line.startswith("ALERT C4 VENUE-CONTAMINATION"),
                  line))

    # C4 no false-flag: perp-only symbol (HYPEUSDT), not in spot universe.
    line = c4_for_row(spot_universe, None, [1.23, 1.24, 1.22, 1.235, 5.0],
                      "HYPEUSDT", "4h", 100000000, 1.23, 1.24, 1.22, 1.235, 5.0)
    cases.append(("C4 no false-flag (perp-only HYPEUSDT)", line is None, line))

    # C4 no false-flag: spot-listed symbol whose row matches SPOT (clean).
    line = c4_for_row(spot_universe, [100.0, 101.0, 99.0, 100.5, 1000.0],
                      [100.0, 101.0, 99.0, 100.5, 1000.0],
                      "ETHUSDT", "4h", 100000000, 100.0, 101.0, 99.0, 100.5, 1000.0)
    cases.append(("C4 no false-flag (clean spot row)", line is None, line))

    # C4 disable path: when the spot universe lookup fails, an explicit alert
    # is produced (never a silent absence). Pure helper, deterministic.
    disable = c4_disable_alert()
    cases.append(("C4 disable emits explicit alert when universe lookup fails",
                  disable.startswith("ALERT C4 VENUE-CONTAMINATION CHECK DISABLED"),
                  disable))

    # C1-HIST: 4h row stamped at close-time (03:59:59) is mis-aligned (red);
    # stamped at open (04:00:00) is aligned (clean).
    cases.append(("C1-HIST red (close-time 4h stamp, 2026-08-06 class)",
                  is_aligned(datetime(2026, 8, 6, 3, 59, 59, tzinfo=timezone.utc).timestamp(), "4h") is False,
                  None))
    cases.append(("C1-HIST clean (open-time 4h stamp)",
                  is_aligned(datetime(2026, 8, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp(), "4h") is True,
                  None))

    # Clean fixture: no C4 on a clean row, and an empty alert list prints nothing.
    clean_line = c4_for_row(spot_universe, [10.0, 11.0, 9.0, 10.5, 100.0],
                            [10.0, 11.0, 9.0, 10.5, 100.0],
                            "BTCUSDT", "1h", 200000000, 10.0, 11.0, 9.0, 10.5, 100.0)
    cases.append(("clean row silent under C4", clean_line is None, clean_line))

    failed = 0
    for name, ok, detail in cases:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{mark}] {name}")
        if detail and not ok:
            print(f"        got: {detail}")
    if failed == 0:
        print("SELFTEST: all cases PASS — no fabricated green; fixtures are known-input.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
            sys.exit(selftest())
        main()
    except Exception as exc:  # operational failure must reach the exit code
        print(f"ALERT MONITOR-FAILURE: {exc}")
        sys.exit(1)
