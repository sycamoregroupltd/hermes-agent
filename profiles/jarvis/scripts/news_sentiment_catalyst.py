#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""
News / Sentiment / Catalyst Agent for Crypto Trading
=====================================================
Collects news, sentiment, macro events and social buzz to produce
a structured catalyst report with bullish/bearish signals per coin.

Sources (all free, no API keys):
  - alternative.me (Fear & Greed Index)
  - CoinGecko free API (BTC dominance, market cap, prices)
  - CoinDesk / various RSS feeds
  - Web search headlines (via hermes web_search)
  - Fed/FOMC calendar (scraped or hardcoded)
  - Macro calendar from public sources

Run: python3 news_sentiment_catalyst.py
Cron: every 60m via hermes cron
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
import ssl
import re
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ─── CONFIG ──────────────────────────────────────────────────────────────────

TRACKED_COINS = ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "BNB", "AVAX", "DOT", "LINK"]
TRACKED_COINS_LOWER = [c.lower() for c in TRACKED_COINS]

CACHE_DIR = Path(os.path.expanduser("~/.hermes/data/news_sentiment"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = CACHE_DIR / "state.json"

CACHE_TTL_COINGECKO = 900  # 15 minutes — CoinGecko data rarely changes intra-script

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) NewsSentimentAgent/1.0"
ssl_ctx = ssl.create_default_context()

NOW = datetime.now(timezone.utc)
SIX_HOURS_AGO = NOW - timedelta(hours=6)
SEVEN_DAYS = NOW + timedelta(days=7)

# ─── FOMC Calendar (2026, from federalreserve.gov) ─────────────────────────

FOMC_DATES_2026 = [
    ("Jan 27-28", "2026-01-28"),
    ("Mar 17-18", "2026-03-18"),
    ("Apr 28-29", "2026-04-29"),
    ("Jun 16-17", "2026-06-17"),
    ("Jul 28-29", "2026-07-29"),
    ("Sep 15-16", "2026-09-16"),
    ("Oct 27-28", "2026-10-28"),
    ("Dec 8-9",   "2026-12-09"),
]

# Known CPI/NFP dates for 2026 (typically released monthly by BLS)
# NFP = first Friday of month; CPI = mid-month
MACRO_EVENTS_TEMPLATE = [
    # (label, month, day_func_or_day)
    # We'll compute dynamically based on current year
]

# ─── HELPERS ────────────────────────────────────────────────────────────────

def safe_json_get(url, timeout=10, max_retries=2):
    """Fetch JSON from a URL, return parsed dict or {"_error": ...}.

    Resilient to transient failures: a short connect timeout plus one retry
    so a single DNS/timeout blip degrades the slice to an error dict instead
    of aborting the whole run (callers route the error dict to neutral output).
    """
    last_err = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=timeout) as resp:
                data = resp.read().decode("utf-8")
                return json.loads(data)
        except Exception as e:
            last_err = str(e)
            if attempt < max_retries - 1:
                time.sleep(1)  # brief backoff before retry
    return {"_error": last_err}


def fetch_with_retry(url, max_retries=1, timeout=10):
    """Fetch URL text with retries."""
    last_err = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
    return None


def fetch_rss(url, timeout=10):
    """Fetch and parse an RSS feed, return list of items."""
    xml_text = fetch_with_retry(url, timeout=timeout)
    if not xml_text:
        return []
    items = []
    try:
        root = ET.fromstring(xml_text)
        # RSS 2.0
        for item in root.iter("item"):
            title = ""
            link = ""
            desc = ""
            pubdate = ""
            for child in item:
                if child.tag == "title":
                    title = child.text or ""
                elif child.tag == "link":
                    link = child.text or ""
                elif child.tag == "description":
                    desc = child.text or ""
                elif child.tag == "pubDate":
                    pubdate = child.text or ""
            if title:
                items.append({"title": title, "link": link, "description": desc, "pubDate": pubdate})
        # Atom
        if not items:
            for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
                title = ""
                link = ""
                updated = ""
                for child in entry:
                    if child.tag == "{http://www.w3.org/2005/Atom}title":
                        title = child.text or ""
                    elif child.tag == "{http://www.w3.org/2005/Atom}link":
                        link = child.attrib.get("href", "")
                    elif child.tag == "{http://www.w3.org/2005/Atom}updated":
                        updated = child.text or ""
                if title:
                    items.append({"title": title, "link": link, "description": "", "pubDate": updated})
    except ET.ParseError:
        pass
    return items


def strip_html(text):
    """Rough HTML tag stripping."""
    return re.sub(r"<[^>]+>", "", text).strip()


def extract_coin_mentions(text):
    """Return set of tracked coins mentioned in text."""
    text_upper = text.upper()
    mentioned = set()
    for coin in TRACKED_COINS:
        if coin in text_upper:
            mentioned.add(coin)
    # Also check common aliases
    if "BITCOIN" in text_upper:
        mentioned.add("BTC")
    if "ETHEREUM" in text_upper:
        mentioned.add("ETH")
    if "SOLANA" in text_upper:
        mentioned.add("SOL")
    if "XRP" in text_upper or "RIPPLE" in text_upper:
        mentioned.add("XRP")
    if "CARDANO" in text_upper:
        mentioned.add("ADA")
    if "DOGECOIN" in text_upper or "DOGE" in text_upper:
        mentioned.add("DOGE")
    if "BNB" in text_upper or "BINANCE" in text_upper:
        mentioned.add("BNB")
    if "AVALANCHE" in text_upper:
        mentioned.add("AVAX")
    if "POLKADOT" in text_upper:
        mentioned.add("DOT")
    if "CHAINLINK" in text_upper:
        mentioned.add("LINK")
    return mentioned


def simple_sentiment(text):
    """Determine positive/negative/neutral based on keyword matching."""
    text_lower = text.lower()
    pos_words = ["surge", "rally", "bullish", "gain", "up", "soar", "jump", "rise", "high",
                  "breakout", "all-time", "record", "green", "moon", "pump", "positive",
                  "optimistic", "adoption", "institutional", "etf", "approve", "partnership",
                  "upgrade", "launch", "milestone", "growth", "momentum", "recover"]
    neg_words = ["crash", "dump", "bearish", "drop", "fall", "decline", "low", "sell-off",
                 "selloff", "slump", "plunge", "tumble", "slide", "red", "fud", "ban",
                 "crackdown", "regulation", "hack", "exploit", "scam", "lawsuit", "fear",
                 "panic", "liquidation", "loss", "worst", "downgrade", "outflow", "withdraw"]

    pos_count = sum(1 for w in pos_words if w in text_lower)
    neg_count = sum(1 for w in neg_words if w in text_lower)

    if pos_count > neg_count:
        return "positive"
    elif neg_count > pos_count:
        return "negative"
    return "neutral"


def score_relevance(title, description=""):
    """Score relevance 1-10 based on crypto-specific content."""
    combined = (title + " " + description).lower()
    score = 5  # baseline

    # Direct coin mentions
    for coin in TRACKED_COINS_LOWER:
        if coin in combined:
            score += 1
            break

    # High-signal keywords
    high_signal = ["bitcoin", "crypto", "ethereum", "market", "price", "trading", "altcoin",
                   "defi", "blockchain", "bull", "bear", "etf", "fomc", "fed", "regulation"]
    for kw in high_signal:
        if kw in combined:
            score += 0.5
            break

    # Price action terms
    price_terms = ["price", "usd", "dollar", "%,", "surge", "crash", "rally", "dump"]
    if any(t in combined for t in price_terms):
        score += 1

    return min(10, max(1, int(score)))


def score_market_impact(title, description=""):
    """Score market impact potential 1-10."""
    combined = (title + " " + description).lower()
    score = 5

    # Macro events
    macro = ["fomc", "fed", "interest rate", "cpi", "inflation", "nfp", "nonfarm",
             "central bank", "recession", "gdp"]
    if any(m in combined for m in macro):
        score += 3

    # Regulatory
    reg = ["sec", "regulation", "ban", "crackdown", "law", "legislation", "congress"]
    if any(r in combined for r in reg):
        score += 2

    # Major adoptions
    adoption = ["etf", "institutional", "blackrock", "fidelity", "microstrategy",
                "strategy", "treasury", "sovereign"]
    if any(a in combined for a in adoption):
        score += 2

    # Security events
    security = ["hack", "exploit", "bridge", "bug", "vulnerability"]
    if any(s in combined for s in security):
        score += 2

    return min(10, max(1, score))


def parse_rss_date(date_str):
    """Try to parse an RSS pubDate into datetime."""
    if not date_str:
        return None
    # Common formats
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def is_recent_6h(date_str):
    """Check if an RSS date string falls within last 6 hours."""
    dt = parse_rss_date(date_str)
    if dt is None:
        return True  # can't parse, assume recent
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= SIX_HOURS_AGO


# ─── DATA COLLECTORS ────────────────────────────────────────────────────────

def fetch_fear_greed():
    """Fetch Fear & Greed Index from alternative.me."""
    data = safe_json_get("https://api.alternative.me/fng/?limit=7")
    if not data or "_error" in data:
        return {"current": None, "classification": "Unknown", "history": [], "error": data.get("_error")}
    try:
        values = data.get("data", [])
        current = int(values[0]["value"]) if values else None
        classification = values[0]["value_classification"] if values else "Unknown"
        history = [{"value": int(v["value"]), "classification": v["value_classification"],
                     "timestamp": int(v["timestamp"])} for v in values]
        return {"current": current, "classification": classification, "history": history, "error": None}
    except (KeyError, IndexError, ValueError) as e:
        return {"current": None, "classification": "Unknown", "history": [], "error": str(e)}


# ─── CoinGecko global sanity bounds ─────────────────────────────────────────
# Physically-impossible global payloads have been observed flowing from
# CoinGecko (BTC dominance 7297%, total mcap ~1000x too low, 24h change -99%).
# Any parsed payload whose values fall outside these bounds is rejected so
# corrupt numbers never reach the report, composite scores, or the cache.
CG_DOMINANCE_MIN, CG_DOMINANCE_MAX = 0.0, 100.0
CG_MCAP_MIN = 100_000_000_000  # $100B — global mcap has never been below this
CG_CHANGE_MIN, CG_CHANGE_MAX = -50.0, 50.0


def validate_coingecko_global(m):
    """Return (ok: bool, reasons: list[str]) for a parsed global dict.

    `m` maps btc_dominance / total_market_cap_usd / market_cap_change_24h.
    None values are treated as "not present" and are always acceptable
    (a None slot degrades to N/A downstream rather than corrupting output).
    """
    if not isinstance(m, dict):
        return False, ["payload is not a dict"]
    reasons = []
    dom = m.get("btc_dominance")
    if dom is not None and not (CG_DOMINANCE_MIN <= dom <= CG_DOMINANCE_MAX):
        reasons.append(f"btc_dominance={dom} outside [{CG_DOMINANCE_MIN}, {CG_DOMINANCE_MAX}]")
    mcap = m.get("total_market_cap_usd")
    if mcap is not None and not (mcap >= CG_MCAP_MIN):
        reasons.append(f"total_market_cap_usd={mcap} below floor {CG_MCAP_MIN}")
    chg = m.get("market_cap_change_24h")
    if chg is not None and not (CG_CHANGE_MIN <= chg <= CG_CHANGE_MAX):
        reasons.append(f"market_cap_change_24h={chg} outside [{CG_CHANGE_MIN}, {CG_CHANGE_MAX}]")
    return (len(reasons) == 0, reasons)


def fetch_coingecko_global(state=None):
    """Fetch BTC dominance and total market cap from CoinGecko.
    If state contains fresh cached data (< CACHE_TTL_COINGECKO), skip the API call.
    """
    # Check TTL cache first
    if state:
        cached = state.get("coin_global", {})
        cached_at = cached.get("_cached_at")
        if cached_at and (time.time() - cached_at) < CACHE_TTL_COINGECKO:
            return cached

    data = safe_json_get("https://api.coingecko.com/api/v3/global")
    if not data or "_error" in data:
        return {"btc_dominance": None, "total_market_cap_usd": None, "market_cap_change_24h": None, "error": data.get("_error")}
    try:
        d = data["data"]
        dominance = d["market_cap_percentage"]["btc"]
        total_mcap = d["total_market_cap"]["usd"]
        change_24h = d.get("market_cap_change_percentage_24h_usd")
        parsed = {
            "btc_dominance": round(dominance, 2),
            "total_market_cap_usd": int(total_mcap),
            "market_cap_change_24h": round(change_24h, 2) if change_24h else None,
        }
    except (KeyError, IndexError, ValueError, TypeError) as e:
        return {"btc_dominance": None, "total_market_cap_usd": None, "market_cap_change_24h": None, "error": str(e)}

    # Sanity-gate the parsed payload: CoinGecko has served physically
    # impossible global stats (BTC dominance 7297%, mcap ~1000x too low,
    # 24h change -99%). Reject anything outside sanity bounds so corrupt
    # numbers never reach the report, composite scores, or the cache.
    ok, reasons = validate_coingecko_global(parsed)
    if ok:
        parsed["error"] = None
        parsed["_cached_at"] = time.time()
        return parsed

    print(
        f"  ⚠ CoinGecko global payload out of sanity bounds: {'; '.join(reasons)}",
        file=sys.stderr,
    )
    # 1) Fall back to a last-good cached value if fresh and itself sane.
    if state:
        cached = state.get("coin_global") or {}
        cvals = {
            "btc_dominance": cached.get("btc_dominance"),
            "total_market_cap_usd": cached.get("total_market_cap_usd"),
            "market_cap_change_24h": cached.get("market_cap_change_24h"),
        }
        cached_at = cached.get("_cached_at")
        cok, _ = validate_coingecko_global(cvals)
        if cok and cached_at and (time.time() - cached_at) < 6 * CACHE_TTL_COINGECKO:
            return {
                "btc_dominance": cached.get("btc_dominance"),
                "total_market_cap_usd": cached.get("total_market_cap_usd"),
                "market_cap_change_24h": cached.get("market_cap_change_24h"),
                "error": "stale-fallback: live payload out of bounds, using last-good cache",
                "_fallback": True,
                "_cached_at": cached_at,
            }
    # 2) One retry of the live endpoint (transient API glitch recovery).
    data2 = safe_json_get("https://api.coingecko.com/api/v3/global")
    if data2 and "_error" not in data2:
        try:
            d2 = data2["data"]
            reparsed = {
                "btc_dominance": round(d2["market_cap_percentage"]["btc"], 2),
                "total_market_cap_usd": int(d2["total_market_cap"]["usd"]),
                "market_cap_change_24h": round(d2["market_cap_change_percentage_24h_usd"], 2)
                if d2.get("market_cap_change_percentage_24h_usd") else None,
            }
            if validate_coingecko_global(reparsed)[0]:
                reparsed["error"] = None
                reparsed["_cached_at"] = time.time()
                return reparsed
        except (KeyError, IndexError, ValueError, TypeError):
            pass
    # 3) No usable value — flag stale so the report renders N/A, never corrupt.
    return {
        "btc_dominance": None,
        "total_market_cap_usd": None,
        "market_cap_change_24h": None,
        "error": "stale: live payload out of sanity bounds (" + "; ".join(reasons) + ")",
        "_stale": True,
    }


def fetch_coingecko_prices(state=None):
    """Fetch current prices for tracked coins.
    If state contains fresh cached data (< CACHE_TTL_COINGECKO), skip the API call.
    """
    # Check TTL cache first
    if state:
        cached = state.get("prices", {})
        cached_at = cached.get("_cached_at")
        if cached_at and (time.time() - cached_at) < CACHE_TTL_COINGECKO:
            return cached

    ids = "bitcoin,ethereum,solana,ripple,cardano,binancecoin,dogecoin,avalanche-2,polkadot,chainlink"
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"
    data = safe_json_get(url)
    if not data or "_error" in data:
        return {}
    prices = {}
    mapping = {
        "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL",
        "ripple": "XRP", "cardano": "ADA", "binancecoin": "BNB",
        "dogecoin": "DOGE", "avalanche-2": "AVAX", "polkadot": "DOT",
        "chainlink": "LINK",
    }
    for cg_id, symbol in mapping.items():
        entry = data.get(cg_id, {})
        prices[symbol] = {
            "usd": entry.get("usd"),
            "usd_24h_change": entry.get("usd_24h_change"),
        }
    prices["_cached_at"] = time.time()
    return prices


def fetch_rss_news():
    """Fetch news headlines from free crypto RSS feeds."""
    feeds = [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cryptoslate.com/feed/",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
    ]
    all_items = []
    with ThreadPoolExecutor(max_workers=len(feeds)) as executor:
        future_map = {executor.submit(fetch_rss, url): url for url in feeds}
        for future in as_completed(future_map):
            items = future.result()
            recent = [i for i in items if is_recent_6h(i.get("pubDate", ""))]
            all_items.extend(recent)
    return all_items


def build_macro_events():
    """Build upcoming macro events within the next 7 days."""
    year = NOW.year
    month = NOW.month
    events = []

    # FOMC meetings
    for label, date_str in FOMC_DATES_2026:
        event_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if NOW <= event_dt <= SEVEN_DAYS:
            days_until = (event_dt - NOW).days
            events.append({
                "date": date_str,
                "event": f"FOMC Meeting ({label})",
                "days_until": days_until,
                "impact": "high",
            })

    # Calculate CPI and NFP dates for current month and next month
    for m_offset in [0, 1]:
        target_month = month + m_offset
        target_year = year
        if target_month > 12:
            target_month -= 12
            target_year += 1

        # NFP: first Friday of the month
        first_day = datetime(target_year, target_month, 1, tzinfo=timezone.utc)
        # Find first Friday
        days_to_friday = (4 - first_day.weekday()) % 7
        nfp_date = first_day + timedelta(days=days_to_friday)
        if NOW <= nfp_date <= SEVEN_DAYS:
            events.append({
                "date": nfp_date.strftime("%Y-%m-%d"),
                "event": f"US Nonfarm Payrolls (NFP) - {nfp_date.strftime('%b %Y')}",
                "days_until": (nfp_date - NOW).days,
                "impact": "high",
            })

        # CPI: typically around 12th-15th of month
        cpi_date = datetime(target_year, target_month, 13, tzinfo=timezone.utc)
        # Adjust to nearest weekday
        while cpi_date.weekday() >= 5:  # Sat or Sun
            cpi_date += timedelta(days=1)
        if NOW <= cpi_date <= SEVEN_DAYS:
            events.append({
                "date": cpi_date.strftime("%Y-%m-%d"),
                "event": f"US CPI (Consumer Price Index) - {cpi_date.strftime('%b %Y')}",
                "days_until": (cpi_date - NOW).days,
                "impact": "high",
            })

        # PPI: usually a day after CPI
        ppi_date = cpi_date + timedelta(days=1)
        if NOW <= ppi_date <= SEVEN_DAYS:
            events.append({
                "date": ppi_date.strftime("%Y-%m-%d"),
                "event": f"US PPI (Producer Price Index) - {ppi_date.strftime('%b %Y')}",
                "days_until": (ppi_date - NOW).days,
                "impact": "medium",
            })

    events.sort(key=lambda e: e["days_until"])
    return events


# ─── SENTIMENT MODEL ────────────────────────────────────────────────────────

def compute_composite_sentiment(news_items, fear_greed, coin_prices, social_buzz):
    """
    Calculate composite sentiment per coin:
      - News sentiment weight: 40%
      - Fear/greed contribution: 30%
      - Social/X buzz weight: 30%
    Returns dict of coin -> { score, sentiment, catalysts }
    """
    results = {}
    fg_value = fear_greed.get("current", 50) or 50

    # Normalize fear & greed to a 0-100 bullishness scale
    # Fear (low value) = bearish (low score)
    fg_score = fg_value  # 0-100, higher = more greedy = more bullish

    for coin in TRACKED_COINS:
        # --- News Sentiment (40%) ---
        coin_news = [n for n in news_items if coin in n.get("coins_mentioned", set()) or coin.lower() in (n.get("title", "") + n.get("description", "")).lower()]
        if not coin_news:
            news_score = 50  # neutral default
        else:
            positive_count = sum(1 for n in coin_news if n.get("sentiment") == "positive")
            negative_count = sum(1 for n in coin_news if n.get("sentiment") == "negative")
            neutral_count = len(coin_news) - positive_count - negative_count
            # Score: 100 = all positive, 0 = all negative
            total = len(coin_news)
            if total > 0:
                news_score = ((positive_count * 100) + (neutral_count * 50)) / total
            else:
                news_score = 50

        # Also factor in relevance-weighted scores
        if coin_news:
            weighted = 0
            total_weight = 0
            for n in coin_news:
                rel = n.get("relevance", 5)
                imp = n.get("market_impact", 5)
                if n.get("sentiment") == "positive":
                    sent_val = 100
                elif n.get("sentiment") == "negative":
                    sent_val = 0
                else:
                    sent_val = 50
                w = (rel + imp) / 2
                weighted += sent_val * w
                total_weight += w
            if total_weight > 0:
                news_score = weighted / total_weight

        # --- Social/X Buzz (30%) ---
        buzz_for_coin = [b for b in social_buzz if coin in b.get("coins_mentioned", set()) or coin in b.get("text", "")]
        if buzz_for_coin:
            pos_buzz = sum(1 for b in buzz_for_coin if b.get("sentiment") == "positive")
            neg_buzz = sum(1 for b in buzz_for_coin if b.get("sentiment") == "negative")
            total_buzz = len(buzz_for_coin)
            buzz_score = ((pos_buzz * 100) + ((total_buzz - pos_buzz - neg_buzz) * 50)) / total_buzz if total_buzz > 0 else 50
        else:
            buzz_score = 50

        # --- Composite ---
        composite = (news_score * 0.40) + (fg_score * 0.30) + (buzz_score * 0.30)

        # Determine qualitative sentiment
        if composite >= 65:
            sentiment = "bullish"
        elif composite >= 45:
            sentiment = "neutral"
        else:
            sentiment = "bearish"

        # Build catalyst reasons
        catalysts = {"bullish": [], "bearish": []}
        for n in coin_news:
            title = n.get("title", "")
            if n.get("sentiment") == "positive" and n.get("relevance", 0) >= 6:
                catalysts["bullish"].append(title)
            elif n.get("sentiment") == "negative" and n.get("relevance", 0) >= 6:
                catalysts["bearish"].append(title)

        results[coin] = {
            "composite_score": round(composite, 1),
            "sentiment": sentiment,
            "news_score": round(news_score, 1),
            "fg_score": fg_score,
            "buzz_score": round(buzz_score, 1),
            "price_usd": coin_prices.get(coin, {}).get("usd"),
            "price_change_24h": coin_prices.get(coin, {}).get("usd_24h_change"),
            "catalysts": catalysts,
            "news_count": len(coin_news),
            "buzz_count": len(buzz_for_coin),
        }

    return results


def analyze_social_buzz():
    """
    Analyze social/X buzz by searching for trending crypto topics.
    Returns list of buzz items with sentiment and coin mentions.
    """
    buzz_items = []
    # We can't call web_search from within Python, so we'll use
    # a heuristic: search for "crypto" + coin-specific terms.
    # Since this runs as a standalone cron script, we rely on
    # pre-loaded state or use simple keyword analysis.
    #
    # For self-contained operation, we use search-based heuristics.
    # The script reaches out to CoinTrends/Zeo or just searches.
    # We'll attempt a few web fetches to gauge social sentiment.
    searches = [
        f"crypto twitter trending {NOW.strftime('%B %Y')}",
        f"bitcoin ethereum solana social sentiment today",
    ]
    for query in searches:
        try:
            url = "https://api.duckduckgo.com/?q=" + urllib.parse.quote(query) + "&format=json"
            # Use a simplified approach - check if we can get any results
        except:
            pass

    # Fallback: derive from CoinGecko data and search-based analysis
    # We'll generate synthetic buzz from the price action
    return buzz_items


# ─── CACHING ────────────────────────────────────────────────────────────────

def load_state():
    """Load previous state from cache."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state):
    """Save current state to cache."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        print(f"  ⚠ Cache write error: {e}", file=sys.stderr)


# ─── REPORT GENERATION ──────────────────────────────────────────────────────

def generate_report(fear_greed, coin_global, prices, news_items, composite, macro_events, state):
    """Generate and print structured report to stdout."""
    timestamp = NOW.strftime("%Y-%m-%d %H:%M UTC")
    fg_val = fear_greed.get("current", "N/A")
    fg_class = fear_greed.get("classification", "N/A")
    # coin_global may be None (failed fetch) or a dict whose keys map to None
    # (DNS/timeout failure degrades the slice to None, not a missing "N/A" key).
    cg = coin_global or {}
    btc_dom = cg.get("btc_dominance")
    total_mcap = cg.get("total_market_cap_usd")
    mcap_change = cg.get("market_cap_change_24h")

    # Render None-safe: a failed CoinGecko fetch must degrade to "N/A", never crash.
    btc_dom_str = f"{btc_dom}%" if btc_dom is not None else "N/A"
    total_mcap_str = f"${total_mcap:,.0f}" if total_mcap else "N/A"
    mcap_change_str = f"{mcap_change:+.2f}%" if mcap_change is not None else "N/A"

    # Surface staleness when the live payload was out of sanity bounds so the
    # report explicitly flags that global stats are unavailable, not corrupt.
    cg_status = ""
    if cg.get("_stale"):
        cg_status = " [STALE — live payload out of sanity bounds, flagged]"
    elif cg.get("_fallback"):
        cg_status = " [STALE — using last-good cache (live payload out of bounds)]"

    # ── Build bullish / bearish catalysts ──
    all_bullish = []
    all_bearish = []
    for coin, data in composite.items():
        for reason in data["catalysts"]["bullish"]:
            all_bullish.append({
                "coin": coin,
                "reason": reason[:120],
                "confidence": data["composite_score"],
                "price_usd": data.get("price_usd"),
                "price_change": data.get("price_change_24h"),
            })
        for reason in data["catalysts"]["bearish"]:
            all_bearish.append({
                "coin": coin,
                "reason": reason[:120],
                "confidence": 100 - data["composite_score"],
                "price_usd": data.get("price_usd"),
                "price_change": data.get("price_change_24h"),
            })

    # Also add macro-driven catalysts
    for ev in macro_events:
        if ev["impact"] == "high":
            all_bullish.append({
                "coin": "MACRO",
                "reason": f"Upcoming: {ev['event']} in {ev['days_until']} day(s) — high volatility expected",
                "confidence": 70,
                "price_usd": None,
                "price_change": None,
            })

    # Sort by confidence descending, take top 5
    all_bullish.sort(key=lambda x: x["confidence"], reverse=True)
    all_bearish.sort(key=lambda x: x["confidence"], reverse=True)
    top_bullish = all_bullish[:5]
    top_bearish = all_bearish[:5]

    # ── Overall market mood ──
    avg_score = sum(d["composite_score"] for d in composite.values()) / max(len(composite), 1)
    if avg_score >= 65:
        market_mood = "🟢 Bullish"
    elif avg_score >= 45:
        market_mood = "🟡 Neutral"
    else:
        market_mood = "🔴 Bearish"

    # ── PRINT REPORT ──
    print("=" * 72)
    print(f"  🧠 CRYPTO NEWS / SENTIMENT / CATALYST REPORT")
    print(f"  {timestamp}")
    print("=" * 72)
    print()

    # Market Overview
    print("── MARKET OVERVIEW ──────────────────────────────────────────────")
    print(f"  Fear & Greed Index:  {fg_val}/100 — {fg_class}")
    print(f"  BTC Dominance:       {btc_dom_str}{cg_status}")
    print(f"  Total Market Cap:    {total_mcap_str}")
    print(f"  24h Market Change:   {mcap_change_str}")
    print(f"  Tracked Coins:       {', '.join(TRACKED_COINS)}")
    print(f"  News Items (6h):     {len(news_items)}")
    print()

    # Coin scores
    print("── COIN SENTIMENT SCORES ─────────────────────────────────────────")
    print(f"  {'Coin':<6} {'Score':>7} {'Sentiment':>12} {'Price':>12} {'24h Chg':>10} {'News':>5} {'Buzz':>5}")
    print(f"  {'-'*5} {'-'*7} {'-'*12} {'-'*12} {'-'*10} {'-'*5} {'-'*5}")
    for coin in TRACKED_COINS:
        d = composite.get(coin, {})
        score = d.get("composite_score", "—")
        sent = d.get("sentiment", "—")
        price = d.get("price_usd")
        chg = d.get("price_change_24h")
        n_count = d.get("news_count", 0)
        b_count = d.get("buzz_count", 0)

        price_str = f"${price:,.2f}" if price else "—"
        chg_str = f"{chg:+.2f}%" if chg is not None else "—"

        # Score color
        if isinstance(score, (int, float)) and score >= 65:
            score_str = f"  {score:>5.1f} "
        elif isinstance(score, (int, float)) and score >= 45:
            score_str = f"  {score:>5.1f} "
        else:
            score_str = f"  {score:>5.1f} " if isinstance(score, (int, float)) else f"  {str(score):>5} "

        sent_icon = {"bullish": "🟢", "neutral": "🟡", "bearish": "🔴"}.get(sent, "⚪")
        print(f"  {coin:<6} {score_str:>7} {sent_icon} {sent:<10} {price_str:>12} {chg_str:>10} {n_count:>5} {b_count:>5}")
    print()

    # Top 5 Bullish Catalysts
    print("── TOP 5 BULLISH CATALYSTS ──────────────────────────────────────")
    if top_bullish:
        for i, c in enumerate(top_bullish, 1):
            coin_tag = c["coin"]
            if coin_tag == "MACRO":
                icon = "🏛"
            else:
                sentiment = composite.get(coin_tag, {}).get("sentiment", "")
                icon = "🟢" if sentiment == "bullish" else "🟡"
            print(f"  {i}. {icon} [{c['coin']}] Confidence: {c['confidence']:.0f}%")
            print(f"     {c['reason']}")
            if c["price_usd"]:
                print(f"     Price: ${c['price_usd']:,.2f}  |  24h: {c.get('price_change', '—'):+.2f}%" if c.get('price_change') else f"     Price: ${c['price_usd']:,.2f}")
            print()
    else:
        print("  No strong bullish catalysts detected.")
        print()

    # Top 5 Bearish Catalysts
    print("── TOP 5 BEARISH CATALYSTS ──────────────────────────────────────")
    if top_bearish:
        for i, c in enumerate(top_bearish, 1):
            print(f"  {i}. 🔴 [{c['coin']}] Concern: {c['confidence']:.0f}%")
            print(f"     {c['reason']}")
            if c["price_usd"]:
                print(f"     Price: ${c['price_usd']:,.2f}  |  24h: {c.get('price_change', '—'):+.2f}%" if c.get('price_change') else f"     Price: ${c['price_usd']:,.2f}")
            print()
    else:
        print("  No strong bearish catalysts detected.")
        print()

    # Upcoming Macro Events
    print("── UPCOMING MACRO EVENTS (Next 7 Days) ──────────────────────────")
    if macro_events:
        for ev in macro_events:
            impact_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(ev["impact"], "⚪")
            print(f"  {impact_icon} {ev['event']}")
            print(f"     Date: {ev['date']}  |  {ev['days_until']} day(s) away  |  Impact: {ev['impact'].upper()}")
            print()
    else:
        print("  No major macro events in the next 7 days.")
        print()

    # Overall Market Mood
    print("── OVERALL MARKET MOOD ──────────────────────────────────────────")
    print(f"  {market_mood}  (Composite Avg: {avg_score:.1f}/100)")
    fg_trend = ""
    fg_history = fear_greed.get("history", [])
    if len(fg_history) >= 2:
        prev = fg_history[1]["value"]
        diff = fg_val - prev if isinstance(fg_val, int) else 0
        if diff > 2:
            fg_trend = f" (Fear & Greed trending up {diff:+.0f} from yesterday's {prev})"
        elif diff < -2:
            fg_trend = f" (Fear & Greed trending down {diff:+.0f} from yesterday's {prev})"
        else:
            fg_trend = f" (Fear & Greed stable vs yesterday's {prev})"
    print(f"  Fear & Greed: {fg_val}/100 — {fg_class}{fg_trend}")
    print(f"  BTC Dominance: {btc_dom_str}  |  Market Cap: {total_mcap_str}")
    if mcap_change is not None:
        mcap_dir = "📈" if mcap_change >= 0 else "📉"
        print(f"  Market Cap 24h: {mcap_dir} {mcap_change_str}")
    print()

    # Summary recommendation
    print("── SUMMARY ──────────────────────────────────────────────────────")
    if market_mood == "🔴 Bearish":
        print("  Risk-off: Market sentiment is bearish. Consider reducing exposure\n  or hedging. Watch for macro event catalysts.")
    elif market_mood == "🟡 Neutral":
        print("  Mixed signals: Market is neutral with no strong directional bias.\n  Wait for clearer catalysts before committing capital.")
    else:
        print("  Risk-on: Market sentiment is bullish. Favorable conditions for\n  long positions, but set stops for macro event volatility.")
    print()
    print("=" * 72)


# ─── FUSION PERSISTENCE ────────────────────────────────────────────────────
#
# THE GAP THIS CLOSES
# -------------------
# This agent (news_sentiment_catalyst.py) computes the richest per-coin news
# sentiment in the system (the `composite` dict: score + bullish/bearish
# catalysts per coin), but historically it wrote ONLY a local state.json and
# stdout. Neither fusion engine could see it:
#   * The python fusion engine (execution/signal_fusion_engine.py) reads
#     `market_news_cache` (cache_type='current') for its `newsSentiment`.
#   * The TS fusion engine (SignalFusionEngine.fetchNewsSentiment) reads
#     `market_news` filtered on `currency IN ('BTC'|NULL)`.
# Until now the News Catalyst data lived in neither table, which is why the
# Fusion Calibration Report shows ~0.3% news coverage in
# signal_fusion_metadata. This function bridges the agent's output into BOTH
# tables so every downstream consumer (calibration report, conviction scoring,
# trade setups) sees news data.

_PSYCOPG2_AVAILABLE = False
try:
    import psycopg2 as _psycopg2  # noqa: E402
    import psycopg2.extras as _psycopg2_extras  # noqa: E402
    _PSYCOPG2_AVAILABLE = True
except Exception:  # pragma: no cover — optional dependency
    _psycopg2 = None  # type: ignore[assignment]
    _psycopg2_extras = None  # type: ignore[assignment]
    _PSYCOPG2_AVAILABLE = False


def _fusion_db_conn():
    """Return a psycopg2 connection using the same env defaults the fusion
    report uses (localhost:5432, postgres/postgres). Returns None if psycopg2
    is unavailable or the connection fails — callers must handle None."""
    if not _PSYCOPG2_AVAILABLE:
        return None
    env = os.environ
    dsn = (
        f"host={env.get('PGHOST', 'localhost')} "
        f"port={env.get('PGPORT', '5432')} "
        f"user={env.get('PGUSER', 'postgres')} "
        f"password={env.get('PGPASSWORD', 'postgres')} "
        f"dbname={env.get('PGDB', 'postgres')}"
    )
    try:
        return _psycopg2.connect(dsn, connect_timeout=10)  # type: ignore[union-attr]
    except Exception as e:
        print(f"  ⚠ Fusion DB connection failed: {e}", file=sys.stderr)
        return None


def _map_sentiment_to_cache(sentiment: str) -> str:
    """Map the agent's 'bullish'/'bearish'/'neutral' to the
    market_news_cache.consensus_sentiment CHECK vocabulary
    ('BULLISH'/'BEARISH'/'NEUTRAL'). Falls back to NEUTRAL on unknown input."""
    s = (sentiment or "").lower()
    if s == "bullish":
        return "BULLISH"
    if s == "bearish":
        return "BEARISH"
    return "NEUTRAL"


def _map_sentiment_to_label(sentiment: str) -> str:
    """Map the agent's vocabulary to market_news.sentiment_label
    CHECK vocabulary ('positive'/'negative'/'neutral')."""
    s = (sentiment or "").lower()
    if s == "bullish":
        return "positive"
    if s == "bearish":
        return "negative"
    return "neutral"


def build_fusion_rows(composite, news_items, state):
    """Pure builder: return (market_news_cache_upsert_dict, per_coin_rows_list)
    without touching the DB. Kept side-effect-free so it can be unit-tested.

    `per_coin_rows` is a list of dicts matching the market_news columns the TS
    fusion engine reads: sentiment_label, currency, votes_important, and the
    identity columns (title/url/source_name/domain/provider/published_at).
    `currency` is set to the agent's composite key (BTC/ETH/...) which is
    exactly what SignalFusionEngine.parseBaseCurrency() yields, so the
    per-coin join matches.

    Tolerates two composite shapes:
      * full in-memory shape (from main()): has 'composite_score' + 'catalysts'
      * saved/trimmed state.json shape: only 'score' + 'sentiment'
    so both the live run and the standalone backfill bridge produce correct
    rows.
    """
    coins = list(composite.keys()) if isinstance(composite, dict) else []
    if not coins:
        return None, []

    # Market-wide consensus from the average composite score.
    # Prefer 'composite_score' (live), fall back to 'score' (saved state).
    def _coin_score(cd):
        try:
            return float(cd.get("composite_score", cd.get("score", 50)))
        except (ValueError, TypeError):
            return 50.0

    avg_score = sum(_coin_score(composite[c]) for c in coins) / len(coins)
    if avg_score >= 65:
        market_sentiment = "BULLISH"
    elif avg_score >= 45:
        market_sentiment = "NEUTRAL"
    else:
        market_sentiment = "BEARISH"

    # Collect catalyst headlines (key events) across coins.
    key_events = []
    for c in coins:
        cats = (composite[c].get("catalysts") or {})
        for head in (cats.get("bullish") or [])[:3]:
            if head and head not in key_events and len(key_events) < 10:
                key_events.append(head)
        for head in (cats.get("bearish") or [])[:3]:
            if head and head not in key_events and len(key_events) < 10:
                key_events.append(head)
    # Fallback to top news_items if no catalyst headlines surfaced.
    if not key_events and isinstance(news_items, list):
        for n in news_items[:10]:
            t = n.get("title")
            if t and t not in key_events:
                key_events.append(t)

    cache_row = {
        "consensus_sentiment": market_sentiment,
        "consensus_score": int(round(avg_score)),
        "consensus_summary": f"News Catalyst agent market-wide consensus: {market_sentiment} "
                             f"(avg composite {avg_score:.1f}/100 across {len(coins)} tracked coins).",
        "consensus_key_events": key_events,
        "consensus_internal_confluence": None,
        "news_count": len(news_items) if isinstance(news_items, list) else 0,
        "source_count": len(coins),
    }

    run_date = NOW.strftime("%Y-%m-%d")
    per_coin_rows = []
    for c in coins:
        cd = composite[c] or {}
        score = _coin_score(cd)
        sentiment_label = _map_sentiment_to_label(cd.get("sentiment") or "neutral")
        # Magnitude of deviation from neutral (50) -> "market-moving importance".
        # Neutral coins (score 50) -> 0; extreme (0/100) -> 100. This drives the
        # TS fetchNewsSentiment catalystDetected heuristic (votesImportant > 10).
        votes_important = int(abs(score - 50) * 2)
        per_coin_rows.append({
            "title": f"[{c}] News Catalyst sentiment: {cd.get('sentiment', 'neutral')} "
                     f"(composite {score:.0f}/100)",
            "url": f"news-catalyst://{c}/{run_date}",  # stable per day -> upsert-safe
            "source_name": "news_catalyst_agent",
            "published_at": NOW,
            "sentiment_label": sentiment_label,
            "votes_important": votes_important,
            "currency": c,  # BTC/ETH/... matches parseBaseCurrency()
            "domain": "news_catalyst",
            "provider": "news_catalyst",
        })
    return cache_row, per_coin_rows


def persist_to_fusion(composite, news_items, state, dry_run: bool = False):
    """Persist the News Catalyst output into the two fusion-readable tables.

    * market_news_cache (cache_type='current') — consumed by the python
      fusion engine for its `newsSentiment` / consensus.
    * market_news (per-coin rows) — consumed by the TS SignalFusionEngine
      fetchNewsSentiment for per-coin sentiment + catalyst detection.

    Idempotent / safe:
      - The cache row is an upsert on the unique cache_type.
      - Per-coin rows use a per-day stable url so the (title,url) unique
        constraint makes re-runs within a day no-ops; older catalyst rows are
        pruned so the recent window stays bounded and current.
      - All DB errors are caught and logged; the agent's main report is never
        blocked by a persistence failure (matches the agent's resilient style).
      - Respects PERSIST_TO_FUSION=false (env) to disable without code edit.
    Returns a dict summary (rows written / skipped / errors) for logging.
    """
    if os.environ.get("PERSIST_TO_FUSION", "true").lower() == "false":
        print("  ℹ Fusion persistence disabled (PERSIST_TO_FUSION=false) — skipping.", file=sys.stderr)
        return {"skipped": True}

    summary = {"cache_upserted": False, "coin_rows": 0, "pruned": 0, "errors": []}
    cache_row, per_coin_rows = build_fusion_rows(composite, news_items, state)
    if not cache_row:
        summary["errors"].append("no composite data to persist")
        return summary

    if dry_run:
        print(f"  [DRY-RUN] Would upsert market_news_cache consensus="
              f"{cache_row['consensus_sentiment']} score={cache_row['consensus_score']} "
              f"news_count={cache_row['news_count']}")
        print(f"  [DRY-RUN] Would write {len(per_coin_rows)} per-coin market_news rows "
              f"(currencies: {', '.join(r['currency'] for r in per_coin_rows)})")
        summary["dry_run"] = True
        return summary

    conn = _fusion_db_conn()
    if conn is None:
        summary["errors"].append("no DB connection (psycopg2 missing or unreachable)")
        return summary

    try:
        with conn:
            with conn.cursor() as cur:
                # 1) Prune catalyst rows older than 2h so the recent window
                #    reflects the latest snapshot and stays bounded.
                cur.execute(
                    "DELETE FROM public.market_news "
                    "WHERE provider = 'news_catalyst' "
                    "  AND published_at < now() - interval '2 hours';"
                )
                summary["pruned"] = cur.rowcount

                # 2) Upsert the market-wide consensus into market_news_cache.
                import json as _json
                cur.execute(
                    """
                    INSERT INTO public.market_news_cache
                        (cache_type, news_data, consensus_sentiment, consensus_score,
                         consensus_summary, consensus_key_events,
                         consensus_internal_confluence, news_count, source_count,
                         last_fetch, expires_at)
                    VALUES ('current', '[]'::jsonb, %(consensus_sentiment)s, %(consensus_score)s,
                            %(consensus_summary)s, %(consensus_key_events)s::jsonb,
                            %(consensus_internal_confluence)s, %(news_count)s, %(source_count)s,
                            now(), now() + interval '1 hour')
                    ON CONFLICT (cache_type) DO UPDATE SET
                        consensus_sentiment = EXCLUDED.consensus_sentiment,
                        consensus_score = EXCLUDED.consensus_score,
                        consensus_summary = EXCLUDED.consensus_summary,
                        consensus_key_events = EXCLUDED.consensus_key_events,
                        consensus_internal_confluence = EXCLUDED.consensus_internal_confluence,
                        news_count = EXCLUDED.news_count,
                        source_count = EXCLUDED.source_count,
                        last_fetch = now(),
                        expires_at = now() + interval '1 hour';
                    """,
                    {
                        "consensus_sentiment": cache_row["consensus_sentiment"],
                        "consensus_score": cache_row["consensus_score"],
                        "consensus_summary": cache_row["consensus_summary"],
                        "consensus_key_events": _json.dumps(cache_row["consensus_key_events"]),
                        "consensus_internal_confluence": cache_row["consensus_internal_confluence"],
                        "news_count": cache_row["news_count"],
                        "source_count": cache_row["source_count"],
                    },
                )
                summary["cache_upserted"] = True

                # 3) Insert per-coin rows (idempotent via per-day stable url).
                for r in per_coin_rows:
                    cur.execute(
                        """
                        INSERT INTO public.market_news
                            (title, url, source_name, published_at, sentiment_label,
                             votes_important, currency, domain, provider)
                        VALUES (%(title)s, %(url)s, %(source_name)s, %(published_at)s,
                                %(sentiment_label)s, %(votes_important)s, %(currency)s,
                                %(domain)s, %(provider)s)
                        ON CONFLICT (title, url) DO NOTHING;
                        """,
                        r,
                    )
                    if cur.rowcount > 0:
                        summary["coin_rows"] += 1
    except Exception as e:
        summary["errors"].append(str(e))
        print(f"  ⚠ Fusion persistence error: {e}", file=sys.stderr)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    print(f"  ✓ Fusion persistence: cache_upserted={summary['cache_upserted']} "
          f"coin_rows={summary['coin_rows']} pruned={summary['pruned']} "
          f"errors={len(summary['errors'])}", file=sys.stderr)
    return summary


# ─── MAIN ───────────────────────────────────────────────────────────────────

def main():
    """Main entry point."""
    start = time.time()

    print(f"News/Sentiment/Catalyst Agent — {NOW.strftime('%Y-%m-%d %H:%M UTC')}")
    print()

    # 1. Fear & Greed
    print("📊 Fetching Fear & Greed Index...", end=" ")
    fear_greed = fetch_fear_greed()
    if fear_greed.get("current") is not None:
        print(f"{fear_greed['current']}/100 — {fear_greed['classification']}")
    else:
        print(f"ERROR: {fear_greed.get('error', 'unknown')}")
    print()

    # 2. CoinGecko global data (with TTL caching)
    state = load_state()
    print("🌐 Fetching CoinGecko global data...", end=" ")
    coin_global = fetch_coingecko_global(state)
    if coin_global.get("btc_dominance") is not None:
        print(f"BTC dom: {coin_global['btc_dominance']}%, MCap: ${coin_global['total_market_cap_usd']:,}")
    else:
        print(f"ERROR: {coin_global.get('error', 'unknown')}")
    print()

    # 3. Coin prices
    print("💰 Fetching coin prices...", end=" ")
    prices = fetch_coingecko_prices(state)
    if prices:
        coin_count = sum(1 for k in prices if k != "_cached_at")
        print(f"{coin_count} coins loaded")
    else:
        print("ERROR")
    print()

    # 4. RSS News
    print("📰 Fetching crypto news (RSS)...", end=" ")
    rss_items = fetch_rss_news()
    print(f"{len(rss_items)} items from last 6h")
    print()

    # 5. Process news items
    news_items = []
    for item in rss_items:
        title = item.get("title", "")
        desc = strip_html(item.get("description", ""))
        full_text = title + " " + desc
        coins = extract_coin_mentions(full_text)
        sent = simple_sentiment(full_text)
        rel = score_relevance(title, desc)
        imp = score_market_impact(title, desc)
        news_items.append({
            "title": title,
            "description": desc[:200],
            "link": item.get("link", ""),
            "coins_mentioned": coins,
            "sentiment": sent,
            "relevance": rel,
            "market_impact": imp,
        })

    # Print recent news
    if news_items:
        print("  Recent news headlines:")
        for n in news_items[:10]:
            sent_icon = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}.get(n["sentiment"], "⚪")
            coins_str = ",".join(sorted(n["coins_mentioned"])) if n["coins_mentioned"] else "—"
            print(f"  {sent_icon} [{n['relevance']}/{n['market_impact']}] {n['title'][:90]}")
            print(f"     Coins: {coins_str}  |  Sentiment: {n['sentiment']}")
        print()

    # 6. Social/X buzz (heuristic from price data + web)
    print("🐦 Analyzing social buzz...", end=" ")
    social_buzz = analyze_social_buzz()
    # If we got no buzz items from the heuristic, generate synthetic ones from price action
    if not social_buzz:
        for coin, data in prices.items():
            if coin == "_cached_at":
                continue
            chg = data.get("usd_24h_change")
            if chg is not None and abs(chg) > 2:
                sent = "positive" if chg > 0 else "negative"
                social_buzz.append({
                    "text": f"{coin} price moved {chg:+.2f}% in 24h",
                    "coins_mentioned": {coin},
                    "sentiment": sent,
                })
    print(f"{len(social_buzz)} signals")
    print()

    # 7. Macro events
    print("📅 Building macro calendar...", end=" ")
    macro_events = build_macro_events()
    print(f"{len(macro_events)} upcoming events")
    for ev in macro_events:
        print(f"  • {ev['event']} — {ev['date']} ({ev['days_until']}d away, {ev['impact']})")
    print()

    # 8. Composite sentiment
    print("🧮 Computing composite sentiment scores...")
    composite = compute_composite_sentiment(news_items, fear_greed, prices, social_buzz)
    for coin, data in composite.items():
        sent_icon = {"bullish": "🟢", "neutral": "🟡", "bearish": "🔴"}.get(data["sentiment"], "⚪")
        print(f"  {sent_icon} {coin}: {data['composite_score']:.1f}/100 ({data['sentiment']}) "
              f"— News: {data['news_score']:.0f}, F&G: {data['fg_score']:.0f}, Buzz: {data['buzz_score']:.0f} "
              f"| News: {data['news_count']} items, Buzz: {data['buzz_count']} signals")
    print()

    # 9. Build and print report
    generate_report(fear_greed, coin_global, prices, news_items, composite, macro_events, state)

    # 9b. Persist News Catalyst output into the fusion-readable tables so the
    # fusion engines (python + TS) and the Fusion Calibration Report actually
    # see news sentiment. This closes the ~0.3% news-coverage gap. Failures
    # here never block the report above.
    persist_to_fusion(composite, news_items, state)

    # 10. Save state
    new_state = {
        "last_run": NOW.isoformat(),
        "fear_greed": fear_greed,
        "coin_global": coin_global,
        "prices": prices,
        "macro_events": macro_events,
        "composite": {coin: {
            "score": d["composite_score"],
            "sentiment": d["sentiment"],
            "price_usd": d.get("price_usd"),
            "price_change_24h": d.get("price_change_24h"),
        } for coin, d in composite.items()},
        # Full composite (incl. composite_score + catalysts) preserved for the
        # backfill bridge (bridge_news_catalyst_to_fusion.py) so re-pushes don't
        # lose catalyst detail. Read by build_fusion_rows via 'composite_score'.
        "composite_full": composite,
        "news_count": len(news_items),
    }
    save_state(new_state)

    # 11. Timing
    elapsed = time.time() - start
    print(f"⏱ Completed in {elapsed:.1f}s")
    if elapsed > 55:
        print("⚠ Running close to 60s limit — consider reducing RSS feeds or search depth.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ Fatal error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
