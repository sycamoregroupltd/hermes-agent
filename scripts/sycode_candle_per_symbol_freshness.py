#!/usr/bin/env python3
# invoker: hermes cron (register after PM review) — manual: python3 ~/.hermes/scripts/sycode_candle_per_symbol_freshness.py
#
# NS-P2.x / t_d7d23f62: PER-SYMBOL candle freshness monitor.
# Closes the silent-writer-death blind spot in dgx_data_freshness_probe.py.
#
# WHY THIS EXISTS
# ---------------
# The canonical dgx_data_freshness_probe.py watches `candles` as ONE pipeline via
#   SELECT max(timestamp) FROM candles
# That is table-level. Because 10 core symbols keep landing 1m/5m/1h/1D candles
# continuously, the table-level max() stays <3h even when 360/370 symbols' daily
# bars are frozen at 2026-06-24. The probe reported GREEN while the 1D feed was
# 92% dead. Silent-writer-death class — the dominant fleet failure mode.
#
# This monitor probes the newest row for each symbol in the current Binance
# spot-USDT universe with a LATERAL ORDER BY timestamp DESC LIMIT 1 lookup.
# The lookup is bounded by the explicit universe and uses the existing
# (symbol,timeframe,timestamp) index; it avoids a table-wide GROUP BY/MAX.
# Read-only (PGOPTIONS default_transaction_read_only=on).
#
# SLOs (mode=alert if fresh_count < floor):
#   - 1m/5m/1h/1D: curated feed; floor=10.
#   - 15m:           broad rotating feed; floor=250 for the 3h window.
#   - 4h:            all currently tradeable Binance spot-USDT symbols with
#                    a represented 4h candle; the floor is dynamic.
#
# A FLOOR (absolute minimum) plus a SOFT-DROP (fresh_count < 90% of observed
# baseline, tracked in state) gives two detection lanes:
#   - hard floor breach  -> always alert (writer totally dead)
#   - gradual erosion    -> alert when dropping below 90% of last-good baseline
#                           (catches the "10 symbols quietly lost coverage" case)
#
# Exit 2 + ALERT lines on any breach; exit 1 on operational error; exit 0 clean.
# Optional --self-test: runs evaluate() on synthetic ages, no DB, no write.

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

DB_CONTAINER = "sycodetrading-supabase-db"
BINANCE_SPOT_EXCHANGE_INFO_URL = (
    "https://api.binance.com/api/v3/exchangeInfo?permissions=SPOT&showPermissionSets=false"
)
MIN_BINANCE_ACTIVE_USDT_SYMBOLS = 300
MIN_TRADEABLE_4H_SYMBOLS = 300


class TimeframeSpec(NamedTuple):
    timeframe: str
    budget_hours: int
    floor: int | None
    note: str
    soft_drop: bool


class RuntimeConfig(NamedTuple):
    timeframe: str
    budget_hours: int
    floor: int
    note: str
    soft_drop: bool
    universe_size: int | None

# (timeframe, budget_hours, hard_floor_fresh_symbols, note)
# budget_hours: max staleness tolerated before a symbol counts as "stale".
# hard_floor:   absolute minimum fresh-symbol count before we scream. Floors are
#               set to the INTENDED design (essentially full universe streaming);
#               reality on 2026-07-13 is far lower — that is the finding, not a
#               mis-calibration. Universe sizes: 1m/5m/1h/1D~370, 15m~504, 4h~473.
TIMEFRAME_SPECS = [
    TimeframeSpec("1m", 3, 10, "curated 10-symbol major feed", True),
    TimeframeSpec("5m", 3, 10, "curated 10-symbol major feed", True),
    TimeframeSpec("15m", 3, 250, "broad rotating feed; reviewed 3h floor", True),
    TimeframeSpec("1h", 3, 10, "curated 10-symbol major feed", True),
    TimeframeSpec("4h", 8, None, "dynamic Binance SPOT/TRADING/USDT symbols already represented in candles", False),
    TimeframeSpec("1D", 27, 10, "curated 10-symbol major feed", True),
]

STATE = Path(os.getenv(
    "CANDLE_FRESHNESS_STATE",
    "/home/frank/.hermes/profiles/jarvis/cron/state/sycode_candle_per_symbol_freshness.json"))
# Soft-drop baseline = max fresh_count observed per tf (ratchet up only).
SOFT_DROP_PCT = 0.90  # alert if fresh_count < 90% of baseline

EMPTY_SENTINEL = -1.0


def build_symbol_age_query(timeframe, eligible_symbols=None):
    """Build an index-backed latest-row probe with safely quoted literals.

    Each symbol is discovered once, then its newest row is fetched through the
    existing (symbol,timeframe,timestamp) index.  Values are supplied as
    SQL literals are escaped centrally; eligible_symbols is JSON so symbols
    cannot alter the query syntax.
    """
    if timeframe not in {spec.timeframe for spec in TIMEFRAME_SPECS}:
        raise ValueError("unsupported timeframe: %r" % timeframe)
    timeframe_sql = _sql_literal(timeframe)
    eligible_cte = (
        "SELECT requested.value AS symbol "
        "FROM jsonb_array_elements_text(" + _sql_literal(json.dumps(sorted(eligible_symbols), separators=(",", ":"))) + ") requested "
        if eligible_symbols is not None else
        "SELECT DISTINCT c.symbol FROM public.candles c WHERE c.timeframe = " + timeframe_sql
    )
    return (
        "WITH eligible_symbols AS (" + eligible_cte + ") "
        "SELECT e.symbol, EXTRACT(EPOCH FROM (now() - latest.timestamp))::bigint "
        "FROM eligible_symbols e "
        "CROSS JOIN LATERAL (SELECT c.timestamp FROM public.candles c "
        "WHERE c.symbol = e.symbol AND c.timeframe = " + timeframe_sql + " "
        "ORDER BY c.timestamp DESC LIMIT 1) latest ORDER BY e.symbol;"
    )


def _sql_literal(value):
    """Return a PostgreSQL string literal without allowing SQL injection."""
    return "'" + value.replace("'", "''") + "'"


def fetch_symbol_ages(timeframe, eligible_symbols=None):
    """Return {symbol: age_seconds} from bounded latest-row probes."""
    sql = build_symbol_age_query(timeframe, eligible_symbols)
    cmd = [
        "docker", "exec",
        "-e", "PGOPTIONS=-c default_transaction_read_only=on",
        DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "postgres",
        "-X", "-q", "-t", "-A", "-F", "\t", "-v", "ON_ERROR_STOP=1",
        "-c", sql,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError("psql failed tf=%s rc=%d: %s" % (
            timeframe, proc.returncode, proc.stderr.strip()[:200]))
    ages = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            symbol, age_seconds = line.split("\t", 1)
            ages[symbol] = int(age_seconds)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("unexpected psql row tf=%s: %r" % (timeframe, line[:120])) from exc
    return ages


def parse_active_binance_spot_symbols(payload):
    """Extract current tradeable Binance spot-USDT symbols."""
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, list):
        raise RuntimeError("Binance exchangeInfo missing symbols list")
    return {
        row["symbol"] for row in symbols if isinstance(row, dict)
        and isinstance(row.get("symbol"), str)
        and row.get("status") == "TRADING"
        and row.get("quoteAsset") == "USDT"
        and row.get("isSpotTradingAllowed", True) is not False
    }


def fetch_active_binance_spot_symbols():
    request = urllib.request.Request(BINANCE_SPOT_EXCHANGE_INFO_URL, headers={"User-Agent": "sycode-candle-freshness/3.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        active = parse_active_binance_spot_symbols(json.load(response))
    if len(active) < MIN_BINANCE_ACTIVE_USDT_SYMBOLS:
        raise RuntimeError("Binance active spot-USDT universe unexpectedly small: %d < %d" % (len(active), MIN_BINANCE_ACTIVE_USDT_SYMBOLS))
    return active


def materialize_configs(symbol_ages_by_tf, active_spot_symbols):
    configs, fresh_counts = [], {}
    for spec in TIMEFRAME_SPECS:
        ages = symbol_ages_by_tf.get(spec.timeframe, {})
        universe_size = None
        if spec.floor is None:
            eligible = set(ages).intersection(active_spot_symbols)
            if len(eligible) < MIN_TRADEABLE_4H_SYMBOLS:
                raise RuntimeError("tradeable 4h universe unexpectedly small: %d < %d" % (len(eligible), MIN_TRADEABLE_4H_SYMBOLS))
            floor, universe_size = len(eligible), len(eligible)
        else:
            eligible, floor = set(ages), spec.floor
        fresh_counts[spec.timeframe] = sum(1 for symbol in eligible if ages.get(symbol, spec.budget_hours * 3600 + 1) <= spec.budget_hours * 3600)
        configs.append(RuntimeConfig(spec.timeframe, spec.budget_hours, floor, spec.note, spec.soft_drop, universe_size))
    return configs, fresh_counts


def read_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def write_state(payload):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(".%s.tmp-%d" % (STATE.name, os.getpid()))
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, STATE)


# ---------------------------------------------------------------------------
# Evaluation (pure — reused by --self-test)
# ---------------------------------------------------------------------------
def evaluate(tf_configs, fresh_counts, baseline=None):
    """tf_configs: list of RuntimeConfig values.
    fresh_counts: {tf: int}.
    baseline: {tf: int} last-good max observed (for soft-drop). Optional.
    Returns (alerts, rows) where rows = [(tf, fresh, floor, baseline, status)]."""
    alerts, rows = [], []
    for config in tf_configs:
        tf, budget, floor = config.timeframe, config.budget_hours, config.floor
        fresh = fresh_counts.get(tf, 0)
        base = (baseline or {}).get(tf)
        if fresh < floor:
            status = "ALERT_FLOOR"
            coverage = ("%d/%d tradeable" % (fresh, config.universe_size)
                        if config.universe_size is not None else str(fresh))
            alerts.append("  🔴 candles[%s]: only %s symbols fresh (floor=%d, budget=%dh) — writer dead/eroded"
                          % (tf, coverage, floor, budget))
        elif (config.soft_drop and base is not None
              and fresh < int(base * SOFT_DROP_PCT) and base > 0):
            status = "ALERT_DROP"
            alerts.append("  🔴 candles[%s]: fresh symbols dropped to %d (%.0f%% of baseline %d) — gradual coverage loss"
                          % (tf, fresh, 100.0 * fresh / base, base))
        else:
            status = "OK"
        rows.append((tf, fresh, floor, base, status))
    return alerts, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true",
                    help="run evaluate() on synthetic ages, no DB, no write")
    args = ap.parse_args()

    if args.self_test:
        test_configs = [RuntimeConfig("1m", 3, 10, "test", True, None),
                        RuntimeConfig("15m", 3, 250, "test", True, None),
                        RuntimeConfig("4h", 8, 300, "test", False, 300)]
        healthy = {"1m": 10, "15m": 300, "4h": 300}
        a1, _ = evaluate(test_configs, healthy, baseline=healthy)
        dead = dict(healthy); dead["1m"] = 9
        a2, r2 = evaluate(test_configs, dead, baseline=healthy)
        drop = dict(healthy); drop["15m"] = 260
        a3, r3 = evaluate(test_configs, drop, baseline=healthy)
        ok = (len(a1) == 0) and (len(a2) == 1) and (len(a3) == 1)
        for r in (r2 + r3):
            print("  self-test row:", r)
        print("SELF-TEST %s" % ("PASS" if ok else "FAIL"))
        sys.exit(0 if ok else 1)

    # Live run
    state = read_state()
    baseline = state.get("baseline", {})
    symbol_ages_by_tf = {}
    fresh_counts = {}
    alerts, rows = [], []
    try:
        active_spot_symbols = fetch_active_binance_spot_symbols()
        for spec in TIMEFRAME_SPECS:
            # Binance's current universe bounds every lane to one index probe
            # per candidate symbol; materialize_configs still applies the
            # reviewed static floors and the special 4h intersection floor.
            eligible = active_spot_symbols
            symbol_ages_by_tf[spec.timeframe] = fetch_symbol_ages(spec.timeframe, eligible)
        configs, fresh_counts = materialize_configs(symbol_ages_by_tf, active_spot_symbols)
        alerts, rows = evaluate(configs, fresh_counts, baseline=baseline)
    except Exception as e:
        print("CANDLE PER-SYMBOL FRESHNESS — DEGRADED: probe error — %s" % str(e)[:200])
        sys.exit(1)

    # Ratchet baseline UP only and only when the timeframe is actually HEALTHY
    # (meets its floor). This prevents a degraded state from becoming the new
    # "normal" baseline (which would silence the soft-drop lane forever).
    new_baseline = dict(baseline)
    for config in configs:
        if not config.soft_drop:
            continue
        tf, floor = config.timeframe, config.floor
        cur = fresh_counts.get(tf, 0)
        if cur >= floor and (tf not in new_baseline or cur > new_baseline[tf]):
            new_baseline[tf] = cur
    write_state({"baseline": new_baseline, "updated_at": datetime.now(timezone.utc).isoformat()})

    print("CANDLE PER-SYMBOL FRESHNESS @ %s" % datetime.now(timezone.utc).isoformat())
    for config, (tf, fresh, floor, base, status) in zip(configs, rows):
        base_s = str(base) if base is not None else "n/a"
        universe_s = (" tradeable_universe=%d" % config.universe_size
                      if config.universe_size is not None else "")
        print("  [%s] %s: fresh=%d floor=%d baseline=%s%s" % (
            "OK" if status == "OK" else "XX", tf, fresh, floor, base_s, universe_s))

    if alerts:
        print("VERDICT: DEGRADED — %d timeframe(s) below coverage SLO" % len(alerts))
        print("RED-ALERT: candle coverage dropped (silent-writer-death check):")
        print("\n".join(alerts))
        print("Check the owning ingestion producer/subscription (CandleIngestionService + KlineFeed subscription set).")
        sys.exit(2)

    print("VERDICT: GREEN — all timeframes meet per-symbol freshness SLO")
    sys.exit(0)


if __name__ == "__main__":
    main()
