#!/usr/bin/env python3
"""
hl-funding-carry-oos-daily — OOS accrual monitor for prereg
strategies/pre-registrations/2026-08-01-hl-funding-carry-oos-v1-prereg-fable.md
(kanban t_7ad59788, MULTIPLE-TESTING-LEDGER row 18).

Recomputes the ENTIRE OOS window from 2026-08-01 on every run (idempotent) from
immutable inputs (prod venue_funding_rates, SELECT-only) and rewrites
oos_ledger.csv. Appends one HL mark/oracle basis snapshot per run (append-only).

no_agent cron contract: stdout EMPTY on a clean day; ALERT/KILL lines otherwise;
any operational failure reaches the exit code. Never fabricate a green row.

Frozen protocol (do not edit without a superseding prereg):
  Universe: BTC ETH SOL XRP ADA DOGE LINK AVAX DOT (HL *USDT rows)
  Short-perp receive-funding top-3, monthly rebalance @ 00:00 UTC on the 1st,
  trailing-30d PIT mean-funding ranking, positive filter, weight 1/3,
  data-gap guard >=80% of 720 expected hourly events.
  Costs: primary 14 bps per leg change per side-equivalent (28 bps RT double-leg,
  W1 rt28 basis) scaled by 1/3 slot weight; diagnostic single-leg 7 bps.
  Kill/alert: K1 trail60 < +2%/yr (2 consecutive, after day 60);
  K2 |basis_pnl| > 1% notional/day; K3 held hourly funding <= -0.5%/hr;
  A1 HL/Bybit BTC 30d premium < 1.2x for 30 consecutive rows; A2 feed stale >26h.
"""
import csv
import io
import json
import math
import os
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, "/home/frank/.hermes/scripts")
from second_brain_writer import write_text_atomic

ART = "/home/frank/obsidian/sycode-trading/research/artifacts/hl-funding-carry-oos-2026-08-01"
OOS_START = date(2026, 8, 1)
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
        "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT"]
COIN = {s: s[:-4] for s in SYMS}
K = 3
TRAIL_D = 30
GAP_GUARD = 0.8 * TRAIL_D * 24          # min hourly events to be rankable
COST_PRIMARY_BPS = 14.0                 # per leg change (28 bps RT double-leg)
COST_DIAG_BPS = 7.0                     # W1 single-leg primary (diagnostic)
K1_FLOOR = 0.02                         # trailing-60d annualized kill floor
K2_BASIS_LIMIT = 0.01                   # 1% of notional in a single day
K3_HOURLY = -0.005                      # -0.5%/hr violent negative funding
A1_CANARY = 1.2                         # HL/CEX premium compression floor
A1_DAYS = 30
FRESH_HOURS = 26

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


def load_funding(venue: str, symbols, since: datetime):
    rows = psql(
        "SELECT symbol, funding_time, funding_rate FROM venue_funding_rates "
        f"WHERE venue='{venue}' AND symbol IN ({','.join(chr(39)+s+chr(39) for s in symbols)}) "
        f"AND funding_time >= '{since.isoformat()}' ORDER BY funding_time")
    ev = defaultdict(list)
    for sym, ts, rate in rows:
        ts = ts.replace(" ", "T")
        if ts.endswith("+00"):
            ts += ":00"
        ev[sym].append((datetime.fromisoformat(ts), float(rate)))
    return ev


def trailing_mean(events, t: datetime, days: int):
    lo = t - timedelta(days=days)
    vals = [r for ts, r in events if lo < ts <= t]
    return (sum(vals) / len(vals), len(vals)) if vals else (None, 0)


def fetch_basis_snapshot():
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        meta, ctxs = json.load(resp)
    now = datetime.now(timezone.utc)
    wanted = set(COIN.values())
    out = []
    for asset, ctx in zip(meta["universe"], ctxs):
        if asset["name"] in wanted:
            mark, oracle = float(ctx["markPx"]), float(ctx["oraclePx"])
            out.append({"ts": now.isoformat(), "coin": asset["name"],
                        "mark": mark, "oracle": oracle,
                        "basis": (mark - oracle) / oracle})
    if len(out) < len(wanted):
        missing = wanted - {r["coin"] for r in out}
        alerts.append(f"ALERT basis-snapshot missing coins: {sorted(missing)}")
    return out


def main() -> int:
    os.makedirs(ART, exist_ok=True)
    now = datetime.now(timezone.utc)
    last_day = now.date() - timedelta(days=1)   # last complete UTC day

    # --- A2 freshness (fail loud, never fabricate) -------------------------
    fresh = psql("SELECT max(funding_time) FROM venue_funding_rates WHERE venue='hyperliquid'")
    ts = fresh[0][0].replace(" ", "T")
    ts += ":00" if ts.endswith("+00") else ""
    age_h = (now - datetime.fromisoformat(ts)).total_seconds() / 3600
    if age_h > FRESH_HOURS:
        print(f"ALERT A2 venue_funding_rates HL stale {age_h:.1f}h (> {FRESH_HOURS}h) — "
              "OOS day is MISSING, not zero. Fix the feed (PR #813 ingester).")
        return 1

    # --- basis snapshot (append-only, read-modify-write atomic) -------------
    snap_path = os.path.join(ART, "basis_snapshots.csv")
    snaps = fetch_basis_snapshot()
    buf = io.StringIO()
    if os.path.exists(snap_path):
        with open(snap_path, "r", newline="", encoding="utf-8") as f:
            existing = f.read()
        buf.write(existing)
        if existing and not existing.endswith("\n"):
            buf.write("\n")
    else:
        w = csv.DictWriter(buf, fieldnames=["ts", "coin", "mark", "oracle", "basis"])
        w.writeheader()
    w = csv.DictWriter(buf, fieldnames=["ts", "coin", "mark", "oracle", "basis"])
    for row in snaps:
        w.writerow(row)
    write_text_atomic(snap_path, buf.getvalue())

    # basis lookup: latest snapshot <= day+1 06:00Z per coin
    by_coin = defaultdict(list)
    with open(snap_path) as f:
        for row in csv.DictReader(f):
            by_coin[row["coin"]].append(
                (datetime.fromisoformat(row["ts"]), float(row["basis"])))
    for c in by_coin:
        by_coin[c].sort()

    def basis_at(coin, day: date):
        cutoff = datetime(day.year, day.month, day.day, 6, tzinfo=timezone.utc) + timedelta(days=1)
        prior = [b for ts, b in by_coin.get(coin, []) if ts <= cutoff]
        return prior[-1] if prior else None

    # --- funding data (PIT, settled only) ----------------------------------
    since = datetime.combine(OOS_START - timedelta(days=TRAIL_D + 2), datetime.min.time(), timezone.utc)
    hl = load_funding("hyperliquid", SYMS, since)
    bybit = load_funding("bybit", ["BTCUSDT"], since)

    # --- candles 1D diagnostic ---------------------------------------------
    closes = defaultdict(dict)
    for sym, d, close in psql(
            "SELECT symbol, timestamp::date, close FROM candles WHERE timeframe='1D' "
            f"AND symbol IN ({','.join(chr(39)+s+chr(39) for s in SYMS)}) "
            f"AND timestamp >= '{(OOS_START - timedelta(days=3)).isoformat()}'"):
        closes[sym][date.fromisoformat(d)] = float(close)

    # --- recompute the OOS window ------------------------------------------
    held = []
    rows_out = []
    day = OOS_START
    canary_breach = 0
    k3_events = []
    while day <= last_day:
        t0 = datetime.combine(day, datetime.min.time(), timezone.utc)
        t1 = t0 + timedelta(days=1)
        legs = 0
        if day.day == 1:  # monthly rebalance decided at 00:00 UTC
            ranked = []
            for s in SYMS:
                mu, n = trailing_mean(hl.get(s, []), t0, TRAIL_D)
                if mu is not None and n >= GAP_GUARD and mu > 0:
                    ranked.append((mu, s))
            ranked.sort(reverse=True)
            new_held = [s for _, s in ranked[:K]]
            legs = len(set(held) ^ set(new_held))
            held = new_held
        gross = 0.0
        k3_flag = ""
        for s in held:
            for ts, r in hl.get(s, []):
                if t0 < ts <= t1:
                    gross += r / K
                    if r <= K3_HOURLY:
                        k3_flag = f"K3:{s}@{r:.5f}"
                        k3_events.append((day, s, r))
        cost = legs * (COST_PRIMARY_BPS / 1e4) / K
        cost_diag = legs * (COST_DIAG_BPS / 1e4) / K
        # hedge-basis MTM proxy (NULL until two snapshots straddle the day)
        basis_pnl = None
        parts = []
        for s in held:
            b1 = basis_at(COIN[s], day)
            b0 = basis_at(COIN[s], day - timedelta(days=1))
            if b0 is not None and b1 is not None:
                parts.append((b0 - b1) / K)   # short perp gains as mark falls vs oracle
        if parts and len(parts) == len(held):
            basis_pnl = sum(parts)
        net = gross - cost + (basis_pnl or 0.0)
        # canary: HL vs Bybit BTC trailing-30d annualized
        mu_hl, _ = trailing_mean(hl.get("BTCUSDT", []), t1, TRAIL_D)
        mu_by, _ = trailing_mean(bybit.get("BTCUSDT", []), t1, TRAIL_D)
        canary = None
        if mu_hl is not None and mu_by and mu_by > 0:
            canary = (mu_hl * 24 * 365) / (mu_by * 3 * 365)
        canary_breach = canary_breach + 1 if (canary is not None and canary < A1_CANARY) else 0
        # candles diagnostic
        rets = []
        for s in held:
            c1, c0 = closes[s].get(day), closes[s].get(day - timedelta(days=1))
            if c1 and c0:
                rets.append(abs(c1 / c0 - 1))
        rows_out.append({
            "date": day.isoformat(), "held": ";".join(held), "legs": legs,
            "gross": f"{gross:.8f}", "cost": f"{cost:.8f}",
            "basis_pnl": "" if basis_pnl is None else f"{basis_pnl:.8f}",
            "net": f"{net:.8f}", "cost_diag_single_leg": f"{cost_diag:.8f}",
            "hl_cex_premium_30d": "" if canary is None else f"{canary:.3f}",
            "held_max_abs_ret1d": f"{max(rets):.5f}" if rets else "",
            "flags": k3_flag,
        })
        day += timedelta(days=1)

    # --- window stats + kill evaluation ------------------------------------
    nets = [float(r["net"]) for r in rows_out]
    n_days = len(nets)
    ann = sum(nets) / n_days * 365 if n_days else 0.0
    cum = 0.0
    for r, x in zip(rows_out, nets):
        cum += x
        r["net_cum"] = f"{cum:.8f}"
        r["ann_since_start"] = f"{cum / (rows_out.index(r) + 1) * 365:.5f}"
    trail60 = sum(nets[-60:]) / min(60, n_days) * 365 if n_days else 0.0
    k1_breaches = 0
    if n_days >= 60:
        for i in range(60, n_days + 1):
            t = sum(nets[i - 60:i]) / 60 * 365
            k1_breaches = k1_breaches + 1 if t < K1_FLOOR else 0
        if k1_breaches >= 2:
            alerts.append(f"KILL K1 trailing-60d net annualized {trail60:+.2%} < +2% "
                          f"for {k1_breaches} consecutive days — prereg kill rule hit.")
    for r in rows_out:
        if r["basis_pnl"] and abs(float(r["basis_pnl"])) > K2_BASIS_LIMIT:
            alerts.append(f"KILL K2 hedge-basis event {r['basis_pnl']} on {r['date']} "
                          "(> 1% notional single day).")
    recent_k3 = [e for e in k3_events if e[0] >= last_day - timedelta(days=30)]
    if k3_events and k3_events[-1][0] == last_day:
        d, s, r = k3_events[-1]
        alerts.append(f"ALERT K3 violent negative funding {s} {r:.5f}/hr on {d} "
                      f"({len(recent_k3)} events in 30d; 3+ escalates to kill review).")
    if len(recent_k3) >= 3:
        alerts.append(f"ALERT K3-ESCALATE {len(recent_k3)} violent-negative-funding events "
                      "in 30d — open kill review on t_7ad59788.")
    if canary_breach >= A1_DAYS:
        alerts.append(f"ALERT A1 crowding canary: HL/Bybit 30d premium < {A1_CANARY}x for "
                      f"{canary_breach} consecutive days — the HL premium is compressing; "
                      "trigger early review per prereg §5.")

    # --- write outputs ------------------------------------------------------
    fields = ["date", "held", "legs", "gross", "cost", "basis_pnl", "net",
              "net_cum", "ann_since_start", "cost_diag_single_leg",
              "hl_cex_premium_30d", "held_max_abs_ret1d", "flags"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    for r in rows_out:
        w.writerow(r)
    write_text_atomic(os.path.join(ART, "oos_ledger.csv"), buf.getvalue())
    write_text_atomic(os.path.join(ART, "status.json"), json.dumps({
        "last_run": now.isoformat(), "last_data_day": last_day.isoformat(),
        "oos_days": n_days, "held": rows_out[-1]["held"] if rows_out else "",
        "ann_since_start": round(ann, 5), "trail60_ann": round(trail60, 5),
        "canary_last": rows_out[-1]["hl_cex_premium_30d"] if rows_out else "",
        "alerts": alerts, "review_date": "2026-11-02",
        "prereg": "strategies/pre-registrations/2026-08-01-hl-funding-carry-oos-v1-prereg-fable.md",
    }, indent=1))

    for line in alerts:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
