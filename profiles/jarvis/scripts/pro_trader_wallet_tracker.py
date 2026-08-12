#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""
Hyperliquid Pro Trader Wallet Tracker
-------------------------------------
Tracks known profitable Hyperliquid wallets, market OI shifts, and funding
changes to produce actionable trading signals.  Runs in <30s, no API keys,
fires-and-forgets gracefully.

Schedule: every 30m via hermès cron
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
STATE_DIR = Path(os.environ.get("HOME", "/home/frank")) / ".hermes" / "data" / "pro_trader_wallet_tracker"
STATE_FILE = STATE_DIR / "state.json"
STATE_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Known pro-trader wallet addresses.
# These are real Hyperliquid ecosystem addresses sourced from the HL
# documentation, Dexly whale explorer trending list, and the HL chain.
# ---------------------------------------------------------------------------
PRO_TRADER_WALLETS: list[dict[str, Any]] = [
    {
        "address": "0x677d831aef5328190852e24f13c46cac05f984e7",
        "label": "VaultLeader-MM",
        "type": "market_maker",
        "notes": "HL vault leader, large multi-strategy operator",
    },
    {
        "address": "0x8c967e73e6b15087c42a10d344cff4c96d877f1d",
        "label": "SubAcctMaster",
        "type": "institutional",
        "notes": "Multi-subaccount master, large portfolio",
    },
    {
        "address": "0xb65822a30bbaaa68942d6f4c43d78704faeabbbb",
        "label": "ActiveTrader-APT",
        "type": "active_trader",
        "notes": "Active APT trader with consistent volume",
    },
    {
        "address": "0x5e89b26d8d66da9888c835c9bfcc2aa51813e152",
        "label": "DexDeployer",
        "type": "builder",
        "notes": "Perpetual dex deployer / ecosystem participant",
    },
    {
        "address": "0x005844b2ffb2e122cf4244be7dbcb4f84924907c",
        "label": "VaultFollower-1",
        "type": "vault_follower",
        "notes": "Large vault follower (>700k equity)",
    },
    {
        "address": "0x035605fc2f24d65300227189025e90a0d947f16c",
        "label": "SubAccount-S1",
        "type": "subaccount",
        "notes": "Sub-account active test trader",
    },
    {
        "address": "0x11af2b93dcb3568b7bf2b6bd6182d260a9495728",
        "label": "Referral-Whale",
        "type": "whale",
        "notes": "High-volume referral partner (>960k vol)",
    },
    {
        "address": "0x3f69d170055913103a034a418953b8695e4e42fa",
        "label": "Referral-Trader",
        "type": "trader",
        "notes": "Active referral program trader (>438k vol)",
    },
]

HYPERLIQUID_API = "https://api.hyperliquid.xyz/info"
TIMEOUT = 10  # seconds per request
MAX_WORKERS = 8

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def hl_post(body: dict) -> Any:
    """POST to the Hyperliquid public info endpoint."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        HYPERLIQUID_API,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        raise RuntimeError(f"hl_post({body.get('type','?')}): {exc}")


def safe_hl(body: dict) -> Any:
    try:
        return hl_post(body)
    except Exception:
        return None


def fmt_ts(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def fmt_dollar(val: str | float) -> str:
    v = float(val)
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:,.2f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:,.1f}K"
    if abs(v) >= 1:
        return f"${v:,.2f}"
    return f"${v:.4f}"


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------


def fetch_market_state() -> dict[str, Any]:
    """Fetch allMids, metaAndAssetCtxs, predictedFundings, perpsAtOpenInterestCap."""
    state: dict[str, Any] = {}

    mids_raw = safe_hl({"type": "allMids"})
    state["allMids"] = mids_raw or {}

    meta_ctxs = safe_hl({"type": "metaAndAssetCtxs"})
    if meta_ctxs and isinstance(meta_ctxs, list) and len(meta_ctxs) == 2:
        state["universe"] = meta_ctxs[0].get("universe", [])
        state["assetCtxs"] = meta_ctxs[1]
    else:
        state["universe"] = []
        state["assetCtxs"] = []

    pred_funding = safe_hl({"type": "predictedFundings"})
    state["predictedFundings"] = pred_funding or []

    oi_cap = safe_hl({"type": "perpsAtOpenInterestCap"})
    state["oiCapCoins"] = oi_cap if isinstance(oi_cap, list) else []

    return state


def fetch_wallet_data(address: str) -> dict:
    """Fetch fills + orders + clearinghouse state for one wallet concurrently."""
    results: dict[str, Any] = {"fills": [], "orders": [], "clearinghouse": None}

    def _fills():
        r = safe_hl({"type": "userFills", "user": address})
        results["fills"] = r if isinstance(r, list) else []

    def _orders():
        r = safe_hl({"type": "historicalOrders", "user": address})
        results["orders"] = r if isinstance(r, list) else []

    def _state():
        results["clearinghouse"] = safe_hl({"type": "clearinghouseState", "user": address})

    threads = [_fills, _orders, _state]
    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(lambda fn: fn(), threads))

    return results


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------


def build_coin_map(universe: list, ctxs: list) -> dict[str, dict]:
    coin_map: dict[str, dict] = {}
    for i, coin_info in enumerate(universe):
        name = coin_info.get("name", f"UNKNOWN_{i}")
        ctx = ctxs[i] if i < len(ctxs) else {}
        coin_map[name] = {
            "name": name,
            "funding": float(ctx.get("funding", 0)),
            "openInterest": float(ctx.get("openInterest", 0)),
            "markPx": float(ctx.get("markPx", 0)),
            "oraclePx": float(ctx.get("oraclePx", 0)),
            "prevDayPx": float(ctx.get("prevDayPx", 0)),
            "dayNtlVlm": float(ctx.get("dayNtlVlm", 0)),
            "premium": float(ctx.get("premium", 0)) if ctx.get("premium") is not None else 0,
        }
    return coin_map


def compute_oi_shifts(
    coin_map: dict[str, dict],
    previous_coin_map: dict[str, dict],
) -> list[dict]:
    shifts: list[dict] = []
    for name, cur in coin_map.items():
        prev = previous_coin_map.get(name)
        if prev is None:
            continue
        oi_change = cur["openInterest"] - prev["openInterest"]
        oi_change_pct = (
            (oi_change / prev["openInterest"] * 100) if prev["openInterest"] > 0 else 0
        )
        if abs(oi_change_pct) >= 0.5:
            shifts.append({
                "coin": name,
                "oi_old": prev["openInterest"],
                "oi_new": cur["openInterest"],
                "oi_change": oi_change,
                "oi_change_pct": round(oi_change_pct, 2),
                "markPx": cur["markPx"],
                "funding": cur["funding"],
                "direction": "LONG_ADD" if oi_change > 0 else "LONG_REDUCE",
            })
    shifts.sort(key=lambda s: abs(s["oi_change"] * s["markPx"]), reverse=True)
    return shifts[:15]


def analyze_wallet_activity(
    wallet: dict,
    data: dict,
) -> dict:
    fills = data.get("fills", [])
    orders = data.get("orders", [])
    clearinghouse = data.get("clearinghouse")

    address = wallet["address"]
    label = wallet["label"]
    now_ms = int(time.time() * 1000)
    all_fills_24h = [f for f in fills if f.get("time", 0) > now_ms - 86400_000]
    recent_fills = [f for f in fills if f.get("time", 0) > now_ms - 3600_000]

    coin_direction: dict[str, dict] = {}
    for f in all_fills_24h:
        coin = f.get("coin", "?")
        side = f.get("side", "A")
        sz = float(f.get("sz", 0))
        px = float(f.get("px", 0))
        notional = sz * px
        if coin not in coin_direction:
            coin_direction[coin] = {"buy_vol": 0.0, "sell_vol": 0.0, "count": 0, "dirs": []}
        cd = coin_direction[coin]
        cd["count"] += 1
        if side == "B":
            cd["buy_vol"] += notional
        else:
            cd["sell_vol"] += notional
        cd["dirs"].append(f.get("dir", ""))

    positions: list[dict] = []
    if clearinghouse:
        for ap in clearinghouse.get("assetPositions", []):
            pos = ap.get("position", {})
            positions.append({
                "coin": pos.get("coin", "?"),
                "szi": float(pos.get("szi", 0)),
                "entryPx": float(pos.get("entryPx", 0)),
                "unrealizedPnl": float(pos.get("unrealizedPnl", 0)),
                "positionValue": float(pos.get("positionValue", 0)),
                "leverage": pos.get("leverage", {}).get("value", 1),
            })

    account_value = 0.0
    if clearinghouse:
        ms = clearinghouse.get("marginSummary", {})
        account_value = float(ms.get("accountValue", 0))

    max_coin = max(
        coin_direction.items(), key=lambda kv: kv[1]["buy_vol"] + kv[1]["sell_vol"]
    ) if coin_direction else ("NONE", {})
    total_vol_24h = sum(cd["buy_vol"] + cd["sell_vol"] for cd in coin_direction.values())
    net_buy = sum(cd["buy_vol"] - cd["sell_vol"] for cd in coin_direction.values())
    trade_count_1h = len(recent_fills)

    has_position = len(positions) > 0
    position_value = sum(p["positionValue"] for p in positions)

    score = 0.0
    score += min(30, account_value / 10000) * 0.3
    score += min(30, trade_count_1h * 3)
    if total_vol_24h > 0:
        direction_bias = abs(net_buy) / total_vol_24h
        score += direction_bias * 25
    if account_value > 0:
        pos_ratio = position_value / account_value
        score += min(15, pos_ratio * 30)
    score = round(min(100, score), 1)

    recent_dirs = [f.get("dir", "") for f in recent_fills]
    opening = sum(1 for d in recent_dirs if "Open" in d)
    closing = sum(1 for d in recent_dirs if "Close" in d or "Reduce" in d)

    action = "HOLDING"
    if opening > closing and opening > 2:
        action = "ACCUMULATING"
    elif closing > opening:
        action = "REDUCING"

    signal_coin = max_coin[0]
    signal_dir = "NEUTRAL"
    if signal_coin != "NONE":
        cd = max_coin[1]
        if cd["buy_vol"] > cd["sell_vol"] * 1.5:
            signal_dir = "LONG"
        elif cd["sell_vol"] > cd["buy_vol"] * 1.5:
            signal_dir = "SHORT"

    return {
        "address": address,
        "label": label,
        "account_value": round(account_value, 2),
        "trade_count_1h": trade_count_1h,
        "trade_count_24h": len(all_fills_24h),
        "total_vol_24h": round(total_vol_24h, 2),
        "net_buy_vol_24h": round(net_buy, 2),
        "positions": positions,
        "position_value": round(position_value, 2),
        "primary_coin": signal_coin,
        "primary_direction": signal_dir,
        "action": action,
        "conviction_score": score,
        "coin_direction": {
            c: {
                "buy_vol": round(v["buy_vol"], 2),
                "sell_vol": round(v["sell_vol"], 2),
                "net": round(v["buy_vol"] - v["sell_vol"], 2),
            }
            for c, v in sorted(
                coin_direction.items(),
                key=lambda kv: kv[1]["buy_vol"] + kv[1]["sell_vol"],
                reverse=True,
            )[:5]
        },
    }


# ---------------------------------------------------------------------------
# Fear & Greed (derived from funding rates)
# ---------------------------------------------------------------------------


def compute_fear_greed(coin_map: dict[str, dict]) -> tuple[str, int]:
    fundings = [c["funding"] for c in coin_map.values() if c.get("funding") is not None]
    if not fundings:
        return "UNKNOWN", 50

    avg_funding = sum(fundings) / len(fundings)
    funding_score = 50 + (avg_funding / 0.001) * 50
    funding_score = max(0, min(100, funding_score))

    pos_funding = sum(1 for c in fundings if c > 0)
    neg_funding = sum(1 for c in fundings if c < 0)
    total = pos_funding + neg_funding
    ratio_score = (pos_funding / total * 100) if total > 0 else 50

    blended = funding_score * 0.6 + ratio_score * 0.4
    blended = max(0, min(100, blended))

    if blended >= 75:
        label = "Extreme Greed"
    elif blended >= 55:
        label = "Greed"
    elif blended >= 45:
        label = "Neutral"
    elif blended >= 25:
        label = "Fear"
    else:
        label = "Extreme Fear"

    return label, int(blended)


# ---------------------------------------------------------------------------
# Generate signals
# ---------------------------------------------------------------------------


def generate_signals(
    oi_shifts: list[dict],
    wallet_analyses: list[dict],
    coin_map: dict[str, dict],
) -> list[dict]:
    signals: list[dict] = []

    for shift in oi_shifts[:8]:
        coin = shift["coin"]
        ci = coin_map.get(coin)
        if not ci:
            continue
        funding = ci["funding"]
        oi_pct = shift["oi_change_pct"]
        direction = shift["direction"]

        confidence = 0
        rationale = []

        if oi_pct > 5:
            confidence += 25
            rationale.append(f"OI surged {oi_pct}%")
        elif oi_pct > 2:
            confidence += 15
            rationale.append(f"OI up {oi_pct}%")
        if oi_pct < -5:
            confidence += 20
            rationale.append(f"OI dropped {oi_pct}%")
        elif oi_pct < -2:
            confidence += 12
            rationale.append(f"OI down {oi_pct}%")

        direction_signal = "NEUTRAL"
        if funding > 0.0005 and direction == "LONG_ADD":
            confidence += 20
            rationale.append("high funding + OI add (crowded long → contrarian short)")
            direction_signal = "SHORT"
        elif funding < -0.0005 and direction == "LONG_ADD":
            confidence += 20
            rationale.append("neg funding + OI add (smart money long)")
            direction_signal = "LONG"
        elif funding > 0.0005 and direction == "LONG_REDUCE":
            confidence += 15
            rationale.append("high funding + OI reduce (smart money exiting)")
            direction_signal = "SHORT"
        elif funding < -0.0005 and direction == "LONG_REDUCE":
            confidence += 15
            rationale.append("neg funding + OI reduce (short squeeze potential)")
            direction_signal = "LONG"
        else:
            direction_signal = "LONG" if oi_pct > 3 else ("SHORT" if oi_pct < -3 else "NEUTRAL")

        wallet_aligned = 0
        wallet_opposing = 0
        for wa in wallet_analyses:
            if wa["primary_coin"] == coin:
                if wa["primary_direction"] == direction_signal:
                    wallet_aligned += 1
                elif direction_signal != "NEUTRAL" and wa["primary_direction"] in ("LONG", "SHORT"):
                    wallet_opposing += 1

        if wallet_aligned >= 2:
            confidence += 15
            rationale.append(f"{wallet_aligned} pro wallets aligned")
        if wallet_opposing >= 2:
            confidence -= 10
            rationale.append(f"{wallet_opposing} pro wallets opposing")

        confidence = max(0, min(100, confidence))

        if confidence >= 25:
            signals.append({
                "coin": coin,
                "direction": direction_signal if direction_signal != "NEUTRAL" else (
                    "SHORT" if funding > 0.0003 else "LONG"
                ),
                "confidence": confidence,
                "oi_change_pct": oi_pct,
                "funding": funding,
                "markPx": ci.get("markPx", 0),
                "rationale": "; ".join(rationale),
            })

    # Wallet consensus signals
    wallet_by_coin: dict[str, list[dict]] = {}
    for wa in wallet_analyses:
        coin = wa["primary_coin"]
        if coin and coin != "NONE":
            wallet_by_coin.setdefault(coin, []).append(wa)

    for coin, ww in wallet_by_coin.items():
        if len(ww) < 2:
            continue
        longs = sum(1 for w in ww if w["primary_direction"] == "LONG")
        shorts = sum(1 for w in ww if w["primary_direction"] == "SHORT")
        total = len(ww)
        if longs >= 2 and longs > shorts:
            consensus = "LONG"
            strength = longs / total
        elif shorts >= 2 and shorts > longs:
            consensus = "SHORT"
            strength = shorts / total
        else:
            continue

        if any(s["coin"] == coin for s in signals):
            continue

        confidence = int(strength * 70 + 20)
        ci = coin_map.get(coin, {})
        signals.append({
            "coin": coin,
            "direction": consensus,
            "confidence": min(95, confidence),
            "oi_change_pct": 0,
            "funding": ci.get("funding", 0),
            "markPx": ci.get("markPx", 0),
            "rationale": f"Wallet consensus: {longs}/{total} on {consensus}",
        })

    signals.sort(key=lambda s: s["confidence"], reverse=True)
    return signals


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def generate_report(
    coin_map: dict[str, dict],
    oi_shifts: list[dict],
    wallet_analyses: list[dict],
    signals: list[dict],
    fear_greed_label: str,
    fg_score: int,
    run_duration: float,
) -> str:
    now = fmt_ts()
    btc_info = coin_map.get("BTC", {})
    eth_info = coin_map.get("ETH", {})
    btc_price = btc_info.get("markPx", 0)
    eth_price = eth_info.get("markPx", 0)

    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("  HYPERLIQUID PRO TRADER WALLET TRACKER")
    lines.append("=" * 78)
    lines.append(f"  Timestamp:   {now}")
    lines.append(f"  BTC:         ${btc_price:,.2f}  |  ETH: ${eth_price:,.2f}")
    lines.append(f"  Fear/Greed:  {fear_greed_label} ({fg_score}/100)")
    lines.append(f"  Run time:    {run_duration:.2f}s")
    lines.append("-" * 78)

    # OI Shifts
    lines.append("\n  📊 TOP COINS BY OPEN INTEREST CHANGE")
    lines.append("  " + "-" * 74)
    if oi_shifts:
        lines.append(f"  {'Coin':<10} {'OI Chg%':<9} {'OI (Old)':<14} {'OI (New)':<14} {'Funding':<10} {'Signal'}")
        lines.append("  " + "-" * 74)
        for s in oi_shifts[:10]:
            oi_old = f"{s['oi_old']:.1f}"
            oi_new = f"{s['oi_new']:.1f}"
            arrow = "🟢 ↑" if s["direction"] == "LONG_ADD" else "🔴 ↓"
            lines.append(
                f"  {s['coin']:<10} {s['oi_change_pct']:>+7.2f}%  {oi_old:<14} {oi_new:<14} "
                f"{s['funding']:<+10.6f} {arrow}"
            )
    else:
        lines.append("  No OI shifts (need 2 runs for baseline).")

    # Wallet activity
    lines.append("\n\n  🐋 DETECTED WALLET ACTIVITY")
    lines.append("  " + "-" * 74)
    active = [w for w in wallet_analyses if w["trade_count_24h"] > 0 or w["positions"]]
    if active:
        active.sort(key=lambda w: w["conviction_score"], reverse=True)
        lines.append(f"  {'Label':<22} {'Value':<12} {'Trades':<10} {'Vol/24h':<14} {'Action':<12} {'Score'}")
        lines.append("  " + "-" * 74)
        for w in active:
            action_icon = "📈" if w["action"] == "ACCUMULATING" else "📉" if w["action"] == "REDUCING" else "⏸️"
            lines.append(
                f"  {w['label']:<22} {fmt_dollar(w['account_value']):<12} "
                f"{w['trade_count_1h']:<10} {fmt_dollar(w['total_vol_24h']):<14} "
                f"{action_icon} {w['action']:<10} {w['conviction_score']:>5.1f}"
            )
        top = active[0]
        lines.append(f"\n  → Top: {top['label']} ({top['address'][:10]}...{top['address'][-6:]})")
        lines.append(f"    Primary coin: {top['primary_coin']} | Dir: {top['primary_direction']}")
        if top['coin_direction']:
            parts = []
            for c, cd in list(top['coin_direction'].items())[:3]:
                if cd['buy_vol'] > 0 or cd['sell_vol'] > 0:
                    ns = "+" if cd['net'] >= 0 else ""
                    parts.append(f"{c}: B={fmt_dollar(cd['buy_vol'])} S={fmt_dollar(cd['sell_vol'])} Net={ns}{fmt_dollar(cd['net'])}")
            lines.append(f"    Detail: {' | '.join(parts)}")
    else:
        lines.append("  No recent wallet activity detected.")
        # Show wallet states anyway
        for w in wallet_analyses:
            val = fmt_dollar(w['account_value'])
            pos_c = len(w['positions'])
            lines.append(f"    {w['label']:<22} value={val:<12} positions={pos_c}")

    # Signals
    lines.append("\n\n  💡 ACTIONABLE SIGNAL RECOMMENDATIONS")
    lines.append("  " + "-" * 74)
    if signals:
        lines.append(f"  {'Coin':<12} {'Dir':<8} {'Confidence':<12} {'OI Chg':<10} {'Funding':<12} {'Rationale'}")
        lines.append("  " + "-" * 74)
        for s in signals[:5]:
            stars = "⭐" * min(5, max(1, int(s["confidence"] / 20)))
            lines.append(
                f"  {s['coin']:<12} {s['direction']:<8} {s['confidence']:>3d}% {stars:<6} "
                f"{s['oi_change_pct']:>+6.2f}%  {s['funding']:<+10.6f}  {s['rationale'][:50]}"
            )
    else:
        lines.append("  No high-confidence signals this run.")

    if signals:
        best = signals[0]
        lines.append(f"\n  🏆 TOP SIGNAL: {best['coin']} {best['direction']} @ {best['confidence']}% confidence")
        lines.append(f"     {best['rationale']}")

    lines.append("\n" + "=" * 78)
    lines.append(f"  State saved to: {STATE_FILE}")
    lines.append("=" * 78)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"previous_coin_map": {}, "last_run": None}


def save_state(coin_map: dict, wallet_analyses: list[dict]):
    state = load_state()
    state["previous_coin_map"] = {
        k: {"openInterest": v["openInterest"], "funding": v["funding"], "markPx": v["markPx"]}
        for k, v in coin_map.items()
    }
    state["wallet_history"] = {
        wa["address"]: {
            "label": wa["label"],
            "account_value": wa["account_value"],
            "last_seen": int(time.time()),
            "primary_coin": wa["primary_coin"],
            "primary_direction": wa["primary_direction"],
            "conviction_score": wa["conviction_score"],
        }
        for wa in wallet_analyses
    }
    state["last_run"] = int(time.time())
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    t0 = time.time()

    print("🚀 Hyperliquid Pro Trader Wallet Tracker", file=sys.stderr)
    print(f"📡 Tracking {len(PRO_TRADER_WALLETS)} pro trader wallets...", file=sys.stderr)

    # 1) Fetch market state (2-3 API calls)
    market_state = fetch_market_state()
    universe = market_state.get("universe", [])
    ctxs = market_state.get("assetCtxs", [])
    coin_map = build_coin_map(universe, ctxs)

    if not coin_map:
        print("❌ Failed to fetch market state from Hyperliquid API.", file=sys.stderr)
        print(json.dumps({"error": "API unavailable", "timestamp": fmt_ts(), "status": "FAILED"}, indent=2))
        sys.exit(1)

    # 2) Load previous state
    state = load_state()
    prev_coin_map = state.get("previous_coin_map", {})

    # 3) OI shifts
    oi_shifts = compute_oi_shifts(coin_map, prev_coin_map)

    # 4) Fetch wallet data in parallel (8 wallets × 3 calls each, but parallelized)
    wallet_data_map: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {
            executor.submit(fetch_wallet_data, w["address"]): w
            for w in PRO_TRADER_WALLETS
        }
        try:
            for future in as_completed(future_map, timeout=25):
                w = future_map[future]
                try:
                    wallet_data_map[w["address"]] = future.result()
                    print(
                        f"  ✓ {w['label']:<22} → data fetched",
                        file=sys.stderr,
                    )
                except Exception as exc:
                    wallet_data_map[w["address"]] = {"fills": [], "orders": [], "clearinghouse": None}
                    print(
                        f"  ✗ {w['label']:<22} → error: {exc}",
                        file=sys.stderr,
                    )
        except FuturesTimeoutError:
            # Hyperliquid public calls occasionally leave one wallet slow. This
            # report is advisory; keep the cron alive with empty data for the
            # laggards instead of crashing the whole run.
            for future, w in future_map.items():
                if not future.done():
                    future.cancel()
                    wallet_data_map[w["address"]] = {"fills": [], "orders": [], "clearinghouse": None}
                    print(
                        f"  ! {w['label']:<22} → timed out; using empty data",
                        file=sys.stderr,
                    )

    # 5) Analyze wallets
    wallet_analyses: list[dict] = []
    for w in PRO_TRADER_WALLETS:
        data = wallet_data_map.get(w["address"], {"fills": [], "orders": [], "clearinghouse": None})
        analysis = analyze_wallet_activity(w, data)
        wallet_analyses.append(analysis)

    # 6) Fear/greed
    fg_label, fg_score = compute_fear_greed(coin_map)

    # 7) Signals
    signals = generate_signals(oi_shifts, wallet_analyses, coin_map)

    # 8) Save state
    save_state(coin_map, wallet_analyses)

    # 9) Generate and print report
    duration = time.time() - t0
    report = generate_report(
        coin_map, oi_shifts, wallet_analyses,
        signals, fg_label, fg_score, duration,
    )
    print(report)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        print(
            json.dumps(
                {
                    "error": "Unhandled exception",
                    "traceback": traceback.format_exc(),
                    "timestamp": fmt_ts(),
                    "status": "CRASHED",
                },
                indent=2,
            )
        )
        sys.exit(1)
