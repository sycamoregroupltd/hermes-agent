#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""
Funding Rate Signal Collector — Hyperliquid
Captures funding rates, calculates z-scores, OI changes, and squeeze risk.
Caches 24h history for z-score calculation. Cron-ready (<15s).
"""

import json
import math
import os
import time
import statistics
from collections import defaultdict
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_URL = "https://api.hyperliquid.xyz/info"
DATA_DIR = os.path.expanduser("/home/frank/.hermes/data/funding_rate")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
MAX_AGE_HOURS = 24
REQUEST_TIMEOUT = 10  # seconds
HOURS_PER_YEAR = 8760
FUNDING_THRESHOLD = 0.0001  # 0.01% per hour → crowded

os.makedirs(DATA_DIR, exist_ok=True)


def fetch_json(payload):
    """POST JSON to Hyperliquid API and return parsed response."""
    resp = requests.post(API_URL, json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def annualize(hourly_rate: float) -> float:
    """Convert hourly funding rate to annualized %."""
    # ((1 + hourly_rate) ^ 8760 - 1) * 100
    try:
        return (math.exp(HOURS_PER_YEAR * math.log1p(hourly_rate)) - 1) * 100
    except (ValueError, OverflowError):
        return hourly_rate * HOURS_PER_YEAR * 100  # linear approximation


def score_funding(annualized_pct: float) -> tuple[int, str]:
    """
    Score 1-10 and recommended action.
    1 = negligible, 10 = extreme (crowded).
    """
    abs_val = abs(annualized_pct)
    if abs_val > 200:
        s = 10
    elif abs_val > 100:
        s = 9
    elif abs_val > 50:
        s = 8
    elif abs_val > 25:
        s = 7
    elif abs_val > 10:
        s = 6
    elif abs_val > 5:
        s = 5
    elif abs_val > 2:
        s = 4
    elif abs_val > 1:
        s = 3
    elif abs_val > 0.5:
        s = 2
    else:
        s = 1

    if annualized_pct > 10:
        action = "SHORT_SQUEEZE_RISK"
    elif annualized_pct < -10:
        action = "LONG_SQUEEZE_RISK"
    elif abs_val > 2:
        action = "WATCH"
    else:
        action = "NEUTRAL"

    return s, action


def z_score(value: float, values: list[float]) -> float:
    """Z-score of value relative to history."""
    if len(values) < 3:
        return 0.0
    mu = statistics.mean(values)
    sigma = statistics.stdev(values) or 1e-12
    return (value - mu) / sigma


def load_history() -> dict:
    """Load cached 24h history of funding snapshots."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                data = json.load(f)
            cutoff = time.time() - MAX_AGE_HOURS * 3600
            # prune old entries
            data["snapshots"] = [s for s in data.get("snapshots", [])
                                 if s.get("ts", 0) >= cutoff]
            return data
        except (json.JSONDecodeError, KeyError):
            pass
    return {"snapshots": []}


def save_history(history: dict):
    """Persist history."""
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def main():
    t0 = time.time()
    report_lines = []

    try:
        # ------------------------------------------------------------------
        # Fetch
        # ------------------------------------------------------------------
        report_lines.append("--- Fetching Hyperliquid data ---")

        prices_raw = fetch_json({"type": "allMids"})
        fundings_raw = fetch_json({"type": "predictedFundings"})
        meta_raw = fetch_json({"type": "metaAndAssetCtxs"})

        meta = meta_raw[0] if isinstance(meta_raw, list) else meta_raw
        ctxs = meta_raw[1] if isinstance(meta_raw, list) else meta_raw.get("assetCtxs", [])

        assets = meta.get("universe", [])
        if not assets:
            assets = meta.get("assets", [])

        # Map coin name -> data
        coin_data = {}
        for i, asset in enumerate(assets):
            name = asset.get("name", "")
            if not name:
                continue
            coin_data[name] = {
                "mid": float(prices_raw.get(name, 0)),
                "funding_predicted": float(fundings_raw.get(name, 0)) if isinstance(fundings_raw, dict) else 0,
                "ctx": ctxs[i] if i < len(ctxs) else {},
            }

        # ------------------------------------------------------------------
        # Calculate
        # ------------------------------------------------------------------
        # Load history for z-score
        history = load_history()
        snapshots = history.get("snapshots", [])
        # Build per-coin historical funding rates
        hist_funding: dict[str, list[float]] = defaultdict(list)
        for snap in snapshots:
            for coin, rate in snap.get("fundings", {}).items():
                hist_funding[coin].append(rate)

        current_ts = time.time()
        current_snapshot = {"ts": current_ts, "fundings": {}, "oi": {}}

        results = []
        for coin, data in coin_data.items():
            mid = data["mid"]
            predicted = data["funding_predicted"]
            ctx = data["ctx"] if data["ctx"] else {}

            # Current funding rate (from context or predicted)
            current_funding = float(ctx.get("funding", predicted))
            current_snapshot["fundings"][coin] = current_funding

            # OI from context
            oi_str = ctx.get("openInterest", "0")
            try:
                oi = float(oi_str)
            except (ValueError, TypeError):
                oi = 0.0
            current_snapshot["fundings"][coin] = current_funding
            current_snapshot["oi"][coin] = oi
            # Annualized %
            annual_pct = annualize(current_funding)

            # Z-score vs 24h
            hist_rates = hist_funding.get(coin, [])
            z = z_score(current_funding, hist_rates)

            # Score & action
            sc, action = score_funding(annual_pct)

            results.append({
                "coin": coin,
                "mid": round(mid, 8),
                "funding_hourly": round(current_funding, 8),
                "funding_annual_pct": round(annual_pct, 2),
                "z_score": round(z, 2),
                "oi": oi,
                "score": sc,
                "action": action,
                "direction": "LONG" if current_funding > 0 else "SHORT",
            })

        # ------------------------------------------------------------------
        # Store snapshot to history
        # ------------------------------------------------------------------
        snapshots.append(current_snapshot)
        # Prune
        cutoff = current_ts - MAX_AGE_HOURS * 3600
        snapshots = [s for s in snapshots if s.get("ts", 0) >= cutoff]
        # Keep max 200 snapshots (every ~7min for 24h)
        if len(snapshots) > 200:
            snapshots = snapshots[-200:]
        history["snapshots"] = snapshots
        save_history(history)

        # ------------------------------------------------------------------
        # Top 10 by extreme funding
        # ------------------------------------------------------------------
        sorted_abs = sorted(results, key=lambda r: abs(r["funding_annual_pct"]), reverse=True)
        top10 = sorted_abs[:10]

        # Top OI movers — use OI from snapshots
        oi_change = []
        if len(snapshots) >= 2:
            prev_oi_map = snapshots[-2].get("oi", {})
            for r in results:
                prev_oi = prev_oi_map.get(r["coin"], None)
                curr_oi = r["oi"]
                if prev_oi is not None and prev_oi > 0 and curr_oi > 0:
                    change = ((curr_oi - prev_oi) / prev_oi) * 100
                    oi_change.append({
                        "coin": r["coin"],
                        "oi_change_pct": round(change, 2),
                        "prev_oi": prev_oi,
                        "curr_oi": curr_oi,
                    })
            oi_change.sort(key=lambda x: abs(x["oi_change_pct"]), reverse=True)

        # ------------------------------------------------------------------
        # State
        # ------------------------------------------------------------------
        state = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ts": current_ts,
            "top_10": [
                {
                    "coin": r["coin"],
                    "funding_annual_pct": r["funding_annual_pct"],
                    "z_score": r["z_score"],
                    "score": r["score"],
                    "action": r["action"],
                    "direction": r["direction"],
                }
                for r in top10
            ],
            "summary": {
                "total_coins": len(results),
                "squeeze_risks": [r["coin"] for r in results if r["action"] in ("SHORT_SQUEEZE_RISK", "LONG_SQUEEZE_RISK")],
                "watch_list": [r["coin"] for r in results if r["action"] == "WATCH"],
            },
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

        elapsed = time.time() - t0
        report_lines.append(f"✅ Collected {len(results)} coins in {elapsed:.1f}s")
        report_lines.append("")

        # ------------------------------------------------------------------
        # Report — Top 5 most extreme funding rates
        # ------------------------------------------------------------------
        report_lines.append("═══ TOP 5 MOST EXTREME FUNDING RATES ═══")
        report_lines.append(f"{'Coin':<10} {'Annualized %':>14} {'Dir':<7} {'Score':<6} {'Action'}")
        report_lines.append("-" * 60)
        for r in top10[:5]:
            report_lines.append(
                f"{r['coin']:<10} {r['funding_annual_pct']:>+10.2f}%  "
                f"{r['direction']:<7} {r['score']:<6} {r['action']}"
            )

        # ------------------------------------------------------------------
        # Report — Top 5 OI movers
        # ------------------------------------------------------------------
        report_lines.append("")
        report_lines.append("═══ TOP 5 OI MOVERS (change from last snapshot) ═══")
        report_lines.append(f"{'Coin':<10} {'OI Δ%':>14}")
        report_lines.append("-" * 30)
        for m in oi_change[:5]:
            report_lines.append(
                f"{m['coin']:<10} {m['oi_change_pct']:>+10.2f}%"
            )

        # ------------------------------------------------------------------
        # Report — Squeeze risk alerts
        # ------------------------------------------------------------------
        squeeze_coins = [r for r in results if r["action"] in ("SHORT_SQUEEZE_RISK", "LONG_SQUEEZE_RISK")]
        if squeeze_coins:
            report_lines.append("")
            report_lines.append("⚠️  SQUEEZE RISK ALERTS")
            for r in sorted(squeeze_coins, key=lambda x: abs(x["funding_annual_pct"]), reverse=True):
                direction_label = "📈 Short squeeze (crowded long)" if r["action"] == "SHORT_SQUEEZE_RISK" else "📉 Long squeeze (crowded short)"
                report_lines.append(
                    f"  {r['coin']:<8} {r['funding_annual_pct']:>+8.2f}%  z={r['z_score']:>+.2f}  "
                    f"{direction_label}"
                )
        else:
            report_lines.append("")
            report_lines.append("✅ No squeeze risk alerts — funding rates benign")

        report_lines.append("")
        report_lines.append(f"📊 Full state written to {STATE_FILE}")

        return "\n".join(report_lines)

    except requests.exceptions.RequestException as e:
        return f"❌ Network error: {e}"
    except json.JSONDecodeError as e:
        return f"❌ JSON parse error: {e}"
    except Exception as e:
        return f"❌ Unexpected error: {e}"


if __name__ == "__main__":
    print(main())
