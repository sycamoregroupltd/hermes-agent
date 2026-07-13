#!/usr/bin/env python3
# CANONICAL SOURCE — see the canonical-copy rule (t_41acb465). Profile-local
# shims MUST os.execv() into this file; never copy the body.
"""Data Stream Integrity Sentinel — news + macro context freshness/integrity/coverage.

Companion to dgx_data_freshness_probe.py (which watches the high-volume price/signal
pipelines). This sentinel covers the THREE data streams the fusion calibration report
flagged as the weakest track:

  1. NEWS CACHE (market_news_cache cache_type='current')
       -> read by the PYTHON fusion engine (execution/signal_fusion_engine.py) for
          newsSentiment. If it goes stale or expires, fusion runs blind to news.
       Check: last_fetch freshness AND expires_at not in the past (live cache row).
  2. NEWS FRESHNESS / COVERAGE (market_news per-coin table)
       -> read by the TS SignalFusionEngine.fetchNewsSentiment. Low coverage = the
          0.3-0.5% news-coverage problem called out in the Fusion Calibration report.
       Check: collected_at freshness AND recent-row coverage of the asset universe.
  3. MACRO CONTEXT (macro_context_daily, VIX + DXY)
       -> read by macro_regime_adaptor. The latest row as_of 2026-07-11 00:30 had
          NULL vix AND NULL dollar_index (real integrity gap). Freshness alone misses
          this; we add a NULL-integrity check on the latest row.
       Check: as_of_ts freshness AND vix/dxy non-null on the latest row.

Behaviour mirrors the parent probe:
  * Read-only (SELECT max(...)/latest row only — no count(*) on huge tables).
  * SILENT on the all-clear (empty stdout = no delivery); emits an alert block on the
    falling edge of a new condition, then stays silent while the same fingerprint
    persists (with a slow re-remind).
  * Writes a unified health-canary JSONL record for the fleet health picture.
  * SILENT when STALE wins (exit 0) — never exits non-zero for a data condition; the
    cron delivery wrapper only alerts on non-zero for watchdog-style scripts. This
    sentinel is an alert-EMITTER, so it deliberately returns 0 and prints to stdout.

Safety gates: PAPER-MODE only. This script only READS and emits alerts; it never mutates
trading state, never restarts ingesters, never touches credentials.
"""
import datetime
import json
import os
import re
import subprocess
import time
from pathlib import Path

PG = "sycodetrading-supabase-db"

# --- Budgets (hours) -----------------------------------------------------------
NEWS_CACHE_FRESH_H = 35          # cache refreshes ~every 30m; 35h = stale + near-miss
NEWS_CACHE_GRACE_MIN = 10        # allow up to 10m past expires_at before "expired"
MARKET_NEWS_FRESH_H = 2          # per-coin news should land at least every 2h
MACRO_FRESH_H = 26               # macro is daily-ish; 26h catches a missed day

# Coverage gate: fraction of the watched asset universe that must have a news row in
# the last COVERAGE_WINDOW_H for the feed to count as "covering". Target >80% (AC#2).
COVERAGE_WINDOW_H = 24
COVERAGE_MIN_FRACTION = 0.80

# Fusion-metadata news coverage gate (AC#2): fraction of recent signal_journeys whose
# signal_fusion_metadata carries a news/catalyst key. Fusion Calibration report flagged
# this at 0.3-0.5%; target >80%.
FUSION_COVERAGE_WINDOW_H = 24
FUSION_COVERAGE_MIN_FRACTION = 0.80

# Watched asset universe (coins the fusion engine scores). If market_news carries
# fewer than COVERAGE_MIN_FRACTION of these with recent rows, coverage is degraded.
WATCHED_ASSETS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT"]

# Health canary integration (same path the parent probe uses).
HEALTH_CANARY_LOG = Path(
    os.getenv("HEALTH_CANARY_LOG",
              "/home/frank/.hermes/profiles/jarvis/cron/output/health_canary.jsonl")
)
STATE = Path(
    os.getenv("NEWS_MACRO_SENTINEL_STATE",
              "/home/frank/.hermes/profiles/devops/cron/state/dgx_news_macro_integrity_sentinel.first_seen.json")
)
REMIND_SECONDS = int(os.getenv("NEWS_MACRO_SENTINEL_REMIND_SECONDS", str(24 * 3600)))


# --- psql helpers (read-only) --------------------------------------------------
def psql_scalar(q):
    r = subprocess.run(
        ["docker", "exec", PG, "psql", "-U", "postgres", "-d", "postgres", "-Atc", q],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        raise RuntimeError(msg.splitlines()[-1][:90] if msg else "rc=%d" % r.returncode)
    return r.stdout.strip()


def probe_news_cache():
    """Return dict: status, age_h, expired(bool), news_count, error."""
    q = (
        "SELECT news_count, source_count, last_fetch, expires_at, now() "
        "FROM market_news_cache WHERE cache_type='current' ORDER BY last_fetch DESC LIMIT 1"
    )
    try:
        row = psql_scalar(q)
        if not row:
            return {"status": "empty", "age_h": None, "expired": False,
                    "news_count": None, "error": None}
        news_count, source_count, last_fetch, expires_at, now_iso = row.split("|")
        last = datetime.datetime.fromisoformat(last_fetch)
        now = datetime.datetime.fromisoformat(now_iso.replace("Z", "+00:00")) if now_iso.endswith("Z") else datetime.datetime.now(datetime.timezone.utc)
        age_h = (now - last).total_seconds() / 3600.0
        expired = False
        if expires_at and expires_at.lower() != "none":
            exp = datetime.datetime.fromisoformat(expires_at)
            if (now - exp).total_seconds() > NEWS_CACHE_GRACE_MIN * 60:
                expired = True
        status = "stale" if (age_h > NEWS_CACHE_FRESH_H or expired) else "fresh"
        return {"status": status, "age_h": round(age_h, 2), "expired": expired,
                "news_count": int(news_count) if news_count else 0, "error": None}
    except Exception as e:
        return {"status": "error", "age_h": None, "expired": False,
                "news_count": None, "error": str(e)[:90]}


def probe_market_news():
    """Freshness + coverage of the per-coin news table."""
    out = {"status": "fresh", "age_h": None, "coverage": None,
           "covered": 0, "total": len(WATCHED_ASSETS), "error": None}
    try:
        # freshness
        age = psql_scalar(
            "SELECT COALESCE(EXTRACT(EPOCH FROM (now()-max(collected_at)))/3600.0, -999)::numeric(12,2) "
            "FROM market_news"
        )
        age = float(age)
        if age <= -999:
            out["status"] = "empty"
            return out
        out["age_h"] = round(age, 2)
        # coverage: how many watched assets have a row in the last window
        covered = 0
        for coin in WATCHED_ASSETS:
            n = psql_scalar(
                "SELECT count(*) FROM market_news "
                "WHERE currency='%s' AND collected_at > now()-interval '%d hours'"
                % (coin, COVERAGE_WINDOW_H)
            )
            if n and int(n) > 0:
                covered += 1
        out["covered"] = covered
        frac = covered / len(WATCHED_ASSETS)
        out["coverage"] = round(frac, 3)
        # degrade if stale OR coverage below gate
        if age > MARKET_NEWS_FRESH_H:
            out["status"] = "stale"
        elif frac < COVERAGE_MIN_FRACTION:
            out["status"] = "low_coverage"
        return out
    except Exception as e:
        out["status"] = "error"
        out["error"] = str(e)[:90]
        return out


def probe_fusion_news_coverage():
    """AC#2: fraction of recent signal_journeys whose signal_fusion_metadata carries
    a news/catalyst key. Read-only single aggregate query."""
    out = {"status": "fresh", "coverage": None, "with_news": 0, "total": 0, "error": None}
    try:
        row = psql_scalar(
            "SELECT count(*), count(*) FILTER (WHERE signal_fusion_metadata ? 'newsSentiment' "
            "OR signal_fusion_metadata ? 'news' OR signal_fusion_metadata ? 'catalyst') "
            "FROM signal_journeys WHERE created_at > now()-interval '%d hours'"
            % FUSION_COVERAGE_WINDOW_H
        )
        if not row or "|" not in row:
            out["status"] = "empty"
            return out
        total, with_news = row.split("|")
        total = int(total) if total else 0
        with_news = int(with_news) if with_news else 0
        out["total"] = total
        out["with_news"] = with_news
        if total == 0:
            out["status"] = "empty"
            return out
        frac = with_news / total
        out["coverage"] = round(frac, 3)
        out["status"] = "low_coverage" if frac < FUSION_COVERAGE_MIN_FRACTION else "fresh"
        return out
    except Exception as e:
        out["status"] = "error"
        out["error"] = str(e)[:90]
        return out


def probe_macro():
    "Freshness + NULL-integrity (VIX, DXY) of the latest macro_context_daily row."""
    out = {"status": "fresh", "age_h": None, "vix": None, "dxy": None,
           "null_fields": [], "error": None}
    try:
        row = psql_scalar(
            "SELECT as_of_ts, vix, dollar_index, now() FROM macro_context_daily "
            "ORDER BY as_of_ts DESC LIMIT 1"
        )
        if not row:
            out["status"] = "empty"
            return out
        as_of, vix, dxy, now_iso = row.split("|")
        as_of_dt = datetime.datetime.fromisoformat(as_of)
        now = datetime.datetime.fromisoformat(now_iso.replace("Z", "+00:00")) if now_iso.endswith("Z") else datetime.datetime.now(datetime.timezone.utc)
        age_h = (now - as_of_dt).total_seconds() / 3600.0
        out["age_h"] = round(age_h, 2)
        out["vix"] = float(vix) if vix and vix.lower() != "none" else None
        out["dxy"] = float(dxy) if dxy and dxy.lower() != "none" else None
        null_fields = []
        if out["vix"] is None:
            null_fields.append("vix")
        if out["dxy"] is None:
            null_fields.append("dollar_index")
        out["null_fields"] = null_fields
        if age_h > MACRO_FRESH_H:
            out["status"] = "stale"
        elif null_fields:
            out["status"] = "null_integrity"
        return out
    except Exception as e:
        out["status"] = "error"
        out["error"] = str(e)[:90]
        return out


# --- dedup contract (mirrors parent probe) -------------------------------------
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


def normalize_alert(a):
    return re.sub(r"[-0-9.]+h", "N", re.sub(r"=[-0-9.]+", "=N", a))


def should_emit(alerts):
    if not alerts:
        # all clear — reset so the next incident re-fires immediately
        try:
            STATE.unlink()
        except FileNotFoundError:
            pass
        return False
    now = int(time.time())
    fp = "\n".join(normalize_alert(a) for a in alerts)
    st = read_state()
    if st.get("fingerprint") != fp:
        write_state({"fingerprint": fp, "first_seen": now, "last_alert": now})
        return True
    last = int(st.get("last_alert") or 0)
    if now - last >= REMIND_SECONDS:
        write_state({**st, "last_alert": now, "last_seen": now})
        return True
    write_state({**st, "last_seen": now})
    return False


def write_health_canary(streams, overall):
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rec = {
        "ts": now_iso,
        "source": "news-macro-integrity-sentinel",
        "data_integrity": {
            "overall": overall,
            "streams": streams,
        },
    }
    try:
        HEALTH_CANARY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(HEALTH_CANARY_LOG, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def main():
    news_cache = probe_news_cache()
    market_news = probe_market_news()
    macro = probe_macro()
    fusion_cov = probe_fusion_news_coverage()

    streams = {
        "news_cache": news_cache,
        "market_news": market_news,
        "macro_context_daily": macro,
        "fusion_news_coverage": fusion_cov,
    }

    alerts = []
    # News cache
    if news_cache["status"] == "error":
        alerts.append("  ⚠ news_cache: probe error — %s" % news_cache["error"])
    elif news_cache["status"] == "empty":
        alerts.append("  🔴 news_cache: EMPTY (no cache_type='current' row) — fusion newsSentiment source missing")
    elif news_cache["status"] == "stale":
        if news_cache["expired"]:
            alerts.append("  🔴 news_cache: EXPIRED (expires_at in past, age %.1fh) — live news cache row is dead, fusion runs blind to news" % news_cache["age_h"])
        else:
            alerts.append("  🔴 news_cache: STALE %.1fh (budget %dh) — news cache not refreshed" % (news_cache["age_h"], NEWS_CACHE_FRESH_H))
    # Market news
    if market_news["status"] == "error":
        alerts.append("  ⚠ market_news: probe error — %s" % market_news["error"])
    elif market_news["status"] == "empty":
        alerts.append("  🔴 market_news: EMPTY (0 rows) — no per-coin news landing")
    elif market_news["status"] == "stale":
        alerts.append("  🔴 market_news: STALE %.1fh (budget %dh) — per-coin news ingestion paused" % (market_news["age_h"], MARKET_NEWS_FRESH_H))
    elif market_news["status"] == "low_coverage":
        alerts.append("  🔴 market_news: LOW COVERAGE %d/%d assets (%.0f%%) in last %dh — below %.0f%% gate (Fusion Calibration news-coverage gap)"
                      % (market_news["covered"], market_news["total"], market_news["coverage"] * 100, COVERAGE_WINDOW_H, COVERAGE_MIN_FRACTION * 100))
    # Macro
    if macro["status"] == "error":
        alerts.append("  ⚠ macro_context_daily: probe error — %s" % macro["error"])
    elif macro["status"] == "empty":
        alerts.append("  🔴 macro_context_daily: EMPTY — VIX/DXY source missing")
    elif macro["status"] == "stale":
        alerts.append("  🔴 macro_context_daily: STALE %.1fh (budget %dh) — VIX/DXY not updated (regime adaptor reads stale macro)" % (macro["age_h"], MACRO_FRESH_H))
    elif macro["status"] == "null_integrity":
        alerts.append("  🔴 macro_context_daily: NULL INTEGRITY latest row as_of %s missing %s — VIX/DXY collection partial (regime adaptor would read NULL)"
                      % (macro.get("age_h"), ", ".join(macro["null_fields"])))
    # Fusion news coverage (AC#2)
    if fusion_cov["status"] == "error":
        alerts.append("  ⚠ fusion_news_coverage: probe error — %s" % fusion_cov["error"])
    elif fusion_cov["status"] == "empty":
        alerts.append("  🔴 fusion_news_coverage: EMPTY (no recent signal_journeys) — cannot assess news coverage")
    elif fusion_cov["status"] == "low_coverage":
        alerts.append("  🔴 fusion_news_coverage: LOW %.0f%% (%d/%d) of recent journeys carry news metadata in last %dh — below %.0f%% gate (Fusion Calibration news-coverage gap)"
                      % (fusion_cov["coverage"] * 100, fusion_cov["with_news"], fusion_cov["total"], FUSION_COVERAGE_WINDOW_H, FUSION_COVERAGE_MIN_FRACTION * 100))

    overall = "ok" if not alerts else "degraded"
    write_health_canary(streams, overall)

    if should_emit(alerts):
        print("🔴 NEWS/MACRO DATA STREAM INTEGRITY — issue(s) detected (now):")
        print("\n".join(alerts))
        print("Paper-mode alert only — no trading impact. Investigate the owning ingester/collector.")


if __name__ == "__main__":
    main()
