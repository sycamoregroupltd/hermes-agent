#!/usr/bin/env python3
"""
Unit test for news_sentiment_catalyst.build_fusion_rows.

Verifies the pure row-builder (no DB) produces correct:
  * market-wide consensus mapping (avg score -> BULLISH/NEUTRAL/BEARISH)
  * sentiment vocabulary mapping (bullish->positive, bearish->negative)
  * votes_important magnitude calc
  * per-coin currency = composite key (matches TS parseBaseCurrency)
  * tolerance of trimmed saved-state shape (score instead of composite_score)
Run:  python3 /home/frank/.hermes/scripts/test_build_fusion_rows.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import news_sentiment_catalyst as nsc

passed = 0
failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


# --- Full in-memory shape (live run) ---
live_composite = {
    "BTC": {"composite_score": 70.0, "sentiment": "bullish",
            "catalysts": {"bullish": ["BTC ETF inflows surge"], "bearish": []}},
    "ETH": {"composite_score": 30.0, "sentiment": "bearish",
            "catalysts": {"bullish": [], "bearish": ["ETH unlock dump"]}},
}
cache, rows = nsc.build_fusion_rows(live_composite, [], {})
check("live: consensus_score avg = 50", cache["consensus_score"] == 50, cache)
check("live: neutral consensus (avg 50)", cache["consensus_sentiment"] == "NEUTRAL", cache)
check("live: news_count 0", cache["news_count"] == 0, cache)
check("live: 2 per-coin rows", len(rows) == 2, rows)
btc = next(r for r in rows if r["currency"] == "BTC")
eth = next(r for r in rows if r["currency"] == "ETH")
check("live: BTC label positive", btc["sentiment_label"] == "positive", btc)
check("live: ETH label negative", eth["sentiment_label"] == "negative", eth)
check("live: BTC votes_important = 40", btc["votes_important"] == 40, btc)
check("live: ETH votes_important = 40", eth["votes_important"] == 40, eth)
check("live: BTC url stable per day", btc["url"].startswith("news-catalyst://BTC/"), btc["url"])
check("live: provider news_catalyst", btc["provider"] == "news_catalyst", btc)

# --- Trimmed saved-state shape (backfill bridge) ---
saved_composite = {
    "BTC": {"score": 70.0, "sentiment": "bullish"},
    "ETH": {"score": 30.0, "sentiment": "bearish"},
}
cache2, rows2 = nsc.build_fusion_rows(saved_composite, [], {})
check("saved: score fallback works", cache2["consensus_score"] == 50, cache2)
check("saved: 2 rows", len(rows2) == 2, rows2)
btc2 = next(r for r in rows2 if r["currency"] == "BTC")
check("saved: BTC votes_important = 40", btc2["votes_important"] == 40, btc2)

# --- Empty composite -> no rows ---
c3, r3 = nsc.build_fusion_rows({}, [], {})
check("empty: None cache", c3 is None, c3)
check("empty: no rows", r3 == [], r3)

# --- Strongly bullish -> BULLISH ---
bully = {"BTC": {"composite_score": 90.0, "sentiment": "bullish", "catalysts": {"bullish": [], "bearish": []}}}
c4, _ = nsc.build_fusion_rows(bully, [], {})
check("bullish avg -> BULLISH", c4["consensus_sentiment"] == "BULLISH", c4)

# --- Vocabulary mappers ---
check("map bullish->BULLISH", nsc._map_sentiment_to_cache("bullish") == "BULLISH")
check("map bearish->BEARISH", nsc._map_sentiment_to_cache("bearish") == "BEARISH")
check("map neutral->NEUTRAL", nsc._map_sentiment_to_cache("neutral") == "NEUTRAL")
check("map foo->NEUTRAL", nsc._map_sentiment_to_cache("foo") == "NEUTRAL")
check("map bullish->positive", nsc._map_sentiment_to_label("bullish") == "positive")
check("map bearish->negative", nsc._map_sentiment_to_label("bearish") == "negative")
check("map None->neutral", nsc._map_sentiment_to_label(None) == "neutral")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
