#!/usr/bin/env python3
# CANONICAL SOURCE — keep ~/.hermes/scripts/ and
# ~/.hermes/profiles/jarvis/scripts/ byte-identical (cron loader rejects symlinks).
"""NOW-regime tape stamp — read-only context for current-climate hunts.

Assembles what is happening NOW from sources we already run:
  - cycle nowcast (ACCUM_NEAR_200 etc.)
  - news_sentiment_catalyst state (F&G, prices, composite)
  - trading /ready (paper/live gate)

Writes:
  ~/.hermes/data/now-regime/latest.json
  ~/.hermes/data/now-regime/stamps.jsonl
  obsidian/sycode-trading/research/now-regime/LATEST.md

NEVER: fusion boost, trade_intents, conviction, live orders, DB writes
       except the two files above + the vault stamp.
This is a label for analogue/event hunts, not a trading signal.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NOW = datetime.now(timezone.utc)
DATA = Path(os.path.expanduser("~/.hermes/data/now-regime"))
NEWS = Path(os.path.expanduser("~/.hermes/data/news_sentiment/state.json"))
NOWCAST = Path(
    "/home/frank/obsidian/sycode-trading/research/cycle-framework/cycle_nowcast_summary.json"
)
VAULT = Path("/home/frank/obsidian/sycode-trading/research/now-regime")
READY_URL = os.environ.get("SYCODE_READY_URL", "http://127.0.0.1:3001/ready")


def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def load_etf_series() -> dict | None:
    """Prefer Farside issuer-USD jsonl; fall back to Bitbo. Never invent."""
    candidates = [
        (DATA / "farside_btc_spot_etf_daily.jsonl", "farside"),
        (DATA / "btc_spot_etf_daily.jsonl", "bitbo"),
    ]
    for path, name in candidates:
        if not path.is_file():
            continue
        rows = []
        try:
            for line in path.read_text().splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        except Exception:
            continue
        if not rows:
            continue
        rows.sort(key=lambda r: r.get("date") or "")
        streak = 0
        for r in reversed(rows):
            try:
                v = float(r.get("net_usd"))
            except (TypeError, ValueError):
                break
            if v < 0:
                streak += 1
            else:
                break
        last = rows[-1]
        return {
            "status": "landed",
            "source": name,
            "path": str(path),
            "n": len(rows),
            "first": rows[0].get("date"),
            "last": last.get("date"),
            "last_net_usd": last.get("net_usd"),
            "streak_outflow": streak,
        }
    return None


def ready() -> dict:
    try:
        with urllib.request.urlopen(READY_URL, timeout=4) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"error": str(e)[:160]}


def fg_streak(history: list, threshold: int = 31) -> int:
    n = 0
    for row in history or []:
        try:
            v = int(row.get("value"))
        except Exception:
            break
        if v <= threshold:
            n += 1
        else:
            break
    return n


def hard_events(news: dict, nowcast: dict, ready_d: dict) -> list[dict]:
    ev = []
    fg = (news or {}).get("fear_greed") or {}
    hist = fg.get("history") or []
    cur = fg.get("current")
    streak = fg_streak(hist)
    if cur is not None and int(cur) <= 31 and streak >= 7:
        ev.append({
            "kind": "fear_streak",
            "value": int(cur),
            "days": streak,
            "note": "Fear<=31 for >=7 printed days — do not treat as a long signal",
        })
    current = (nowcast or {}).get("current") or {}
    dist = current.get("dist_200_pct")
    if dist is not None and abs(float(dist)) <= 3.0:
        ev.append({
            "kind": "at_200w_sma",
            "dist_200_pct": float(dist),
            "phase": current.get("phase"),
            "note": "Price hugging 200w SMA — NOW-1 already REJECTED fade/ride here",
        })
    prices = (news or {}).get("prices") or {}
    btc = prices.get("BTC") or {}
    chg = btc.get("usd_24h_change")
    if chg is not None and float(chg) <= -3.0:
        ev.append({
            "kind": "btc_down_day",
            "usd_24h_change": float(chg),
            "note": ">=3% BTC down day inside current climate",
        })
    proof = (ready_d or {}).get("proof") or {}
    if ready_d.get("status") and ready_d.get("status") != "ready":
        ev.append({"kind": "runtime_not_ready", "status": ready_d.get("status")})
    if proof.get("proofModeEnabled") is False:
        ev.append({
            "kind": "proof_mode_off",
            "note": "Runtime ready but proof invalidated/disabled — not a go-live",
        })
    etf = load_etf_series()
    if etf and etf.get("streak_outflow", 0) >= 2:
        ev.append({
            "kind": "etf_outflow_streak",
            "days": etf["streak_outflow"],
            "last": etf.get("last"),
            "last_net_usd": etf.get("last_net_usd"),
            "note": (
                f"{etf['streak_outflow']} printed Farside outflow days ending {etf.get('last')} "
                "— not a long signal (NOW-3 fade REJECTED)"
            ),
        })
    return ev


def research_posture(events: list[dict], phase: str | None) -> str:
    kinds = {e["kind"] for e in events}
    if "at_200w_sma" in kinds and "fear_streak" in kinds:
        return (
            "ACCUM + Fear streak + 200w test. Do not hunt SMA/momentum longs. "
            "Next hunts: hard flow/events (ETF create/redeem, liq, unlock), not trend."
        )
    if phase == "ACCUM_NEAR_200":
        return "ACCUM climate. Analogue-conditioned L2/L1 only. No multi-year generalist grids."
    return "Stamp only. No trade. Read hard_events before opening a new cell."


def render_md(stamp: dict) -> str:
    ev_lines = "\n".join(
        f"- **{e['kind']}**: {e.get('note') or e}" for e in stamp["hard_events"]
    ) or "- (none)"
    c = stamp.get("cycle") or {}
    fg = stamp.get("fear_greed") or {}
    px = stamp.get("btc") or {}
    return f"""---
title: NOW-regime tape stamp (latest)
type: research
status: active
created: '{stamp["asof"][:10]}'
updated: '{stamp["asof"][:10]}'
confidence: medium
tags: [sycode-trading, now-regime, tape-stamp, paper-only]
project: sycode-trading
generated: true
generator: now_regime_tape_stamp.py
---

# NOW-regime tape stamp

**asof:** {stamp["asof"]}
**NOT A TRADE.** Label for analogue/event hunts. Never a fusion boost.

| Field | Value |
|---|---|
| Phase | `{c.get("phase")}` |
| Dist 200w | {c.get("dist_200_pct")} % |
| DD from ATH | {c.get("dd_from_ath_pct")} % |
| Fear & Greed | {fg.get("current")} {fg.get("classification")} (streak≤31: {fg.get("streak_le_31")}d) |
| BTC | ${px.get("usd")} ({px.get("usd_24h_change")}%) |
| Runtime | {stamp.get("runtime", {}).get("status")} |

## Hard events

{ev_lines}

## Research posture

{stamp["research_posture"]}

## Sources

cycle nowcast · news_sentiment state · /ready
ETF daily series: {stamp.get("etf_flow_note") or "absent"}
"""


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    VAULT.mkdir(parents=True, exist_ok=True)
    news = load_json(NEWS) or {}
    nowcast = load_json(NOWCAST) or {}
    ready_d = ready()
    etf = load_etf_series()
    current = (nowcast.get("current") or {}) if isinstance(nowcast, dict) else {}
    fg = news.get("fear_greed") or {}
    prices = news.get("prices") or {}
    events = hard_events(news, nowcast, ready_d)
    stamp = {
        "asof": NOW.isoformat(),
        "paper_only": True,
        "not_a_trade": True,
        "not_a_fusion_input": True,
        "cycle": {
            "phase": current.get("phase"),
            "dist_200_pct": current.get("dist_200_pct"),
            "dd_from_ath_pct": current.get("dd_from_ath_pct"),
            "weeks_since_halving": current.get("weeks_since_halving"),
            "price": current.get("price"),
            "sma200": current.get("sma200"),
            "asof": current.get("asof"),
        },
        "fear_greed": {
            "current": fg.get("current"),
            "classification": fg.get("classification"),
            "streak_le_31": fg_streak(fg.get("history") or []),
        },
        "btc": prices.get("BTC") or {},
        "eth": prices.get("ETH") or {},
        "btc_dominance": ((news.get("coin_global") or {}).get("btc_dominance")),
        "composite_btc": ((news.get("composite") or {}).get("BTC") or {}),
        "runtime": {
            "status": ready_d.get("status"),
            "proofModeEnabled": ((ready_d.get("proof") or {}).get("proofModeEnabled")),
            "error": ready_d.get("error"),
        },
        "hard_events": events,
        "research_posture": research_posture(events, current.get("phase")),
        "etf_flow_series": (etf or {}).get("status") or "absent",
        "etf_flow": etf,
        "etf_flow_note": (
            f"**landed** {etf['source']} n={etf['n']} {etf['first']}..{etf['last']}"
            if etf
            else "absent. Run `python3 ~/.hermes/scripts/fetch_farside_btc_etf_daily.py` (Firecrawl). Do not invent flow numbers."
        ),
    }
    (DATA / "latest.json").write_text(json.dumps(stamp, indent=2, default=str) + "\n")
    with (DATA / "stamps.jsonl").open("a") as f:
        f.write(json.dumps(stamp, default=str) + "\n")
    (VAULT / "LATEST.md").write_text(render_md(stamp))

    # Compact stdout for cron history. Always print (we want an audit trail).
    ev = ",".join(e["kind"] for e in events) or "none"
    print(
        f"NOW-STAMP {stamp['asof']} phase={current.get('phase')} "
        f"fg={fg.get('current')} dist200={current.get('dist_200_pct')} "
        f"btc_24h={((prices.get('BTC') or {}).get('usd_24h_change'))} "
        f"events={ev}"
    )
    print(stamp["research_posture"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
