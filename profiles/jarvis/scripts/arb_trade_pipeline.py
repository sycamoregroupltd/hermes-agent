#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Arb-to-trade pipeline. Runs every 5m via cron.

Token/timeout hygiene: OpenClaw auth is attached inside urllib Request objects,
not subprocess argv. Error output is sanitized and fail-closed before trade-open
side effects.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

DB = ["docker", "exec", "-i", "sycodetrading-supabase-db", "psql", "-U", "postgres", "-d", "postgres"]
# Credential loading: shared env file (defaults to sycode-credential.env); env vars override.
_CRED_ENV_FILE = os.environ.get("SYCODE_CREDENTIAL_ENV_FILE", "/home/frank/.hermes/secrets/sycode-credential.env")
if os.path.exists(_CRED_ENV_FILE):
    from dotenv import load_dotenv
    load_dotenv(_CRED_ENV_FILE, override=False)

TOKEN_NAMES_MAP = {"SYCODE_TRADE_TOKEN": "OPENCLAW_TRADE_TOKEN", "SYCODE_READ_TOKEN": "OPENCLAW_READ_TOKEN"}
TRADE_TOKEN = os.environ.get("SYCODE_TRADE_TOKEN") or os.environ.get("OPENCLAW_TRADE_TOKEN")
READ_TOKEN = os.environ.get("SYCODE_READ_TOKEN") or os.environ.get("OPENCLAW_READ_TOKEN")
if not TRADE_TOKEN or not READ_TOKEN:
    print("[FATAL] Missing OpenClaw credentials. Set SYCODE_TRADE_TOKEN + SYCODE_READ_TOKEN\n"
          f"       (or OPENCLAW_TRADE_TOKEN + OPENCLAW_READ_TOKEN) in env or in {_CRED_ENV_FILE}.",
          flush=True)
    sys.exit(3)
assert TRADE_TOKEN is not None and READ_TOKEN is not None, "fail-closed guard above guarantees this"

AUTH_HEADER_NAME = "X-" + "Sycode-Token"
OC = os.environ.get("OPENCLAW_BASE_URL", "http://localhost:3001/api/openclaw").rstrip("/")
HTTP_TIMEOUT = float(os.environ.get("ARB_PIPELINE_HTTP_TIMEOUT", "10"))
DB_TIMEOUT = float(os.environ.get("ARB_PIPELINE_DB_TIMEOUT", "10"))
SKIP_SIDE_EFFECTS = os.environ.get("ARB_PIPELINE_SKIP_SIDE_EFFECTS", "").lower() in {"1", "true", "yes"}
DISABLE_TRADES = os.environ.get("ARB_PIPELINE_DISABLE_TRADES", "").lower() in {"1", "true", "yes"}

SECRET_VALUES = tuple(v for v in {TRADE_TOKEN, READ_TOKEN} if v)


def sanitize(value: object) -> str:
    text = str(value)
    for secret in SECRET_VALUES:
        if secret:
            text = text.replace(secret, "[redacted-token]")
    text = text.replace(AUTH_HEADER_NAME, "[redacted-auth-header]")
    return text


def log(level: str, msg: str) -> None:
    print(f"  [{level}] {sanitize(msg)}", flush=True)


def write_db(src: str, data: dict[str, Any]) -> bool:
    if SKIP_SIDE_EFFECTS:
        log("DB", f"Skipped write for {src}")
        return True
    try:
        payload = json.dumps({"source": src, "data": data})
        sql = f"INSERT INTO n8n_market_data (source, payload) VALUES ('{src}', $TAG${payload}$TAG$::jsonb);"
        result = subprocess.run(DB, input=sql.encode(), capture_output=True, timeout=DB_TIMEOUT)
        if result.returncode != 0:
            log("WARN", f"DB write failed for {src}: rc={result.returncode}")
            return False
        return True
    except subprocess.TimeoutExpired as exc:
        log("WARN", f"DB write timed out for {src} after {exc.timeout}s")
        return False
    except Exception as exc:
        log("WARN", f"DB write failed for {src}: {type(exc).__name__}: {sanitize(exc)}")
        return False


def fetch(url: str, method: str = "GET", body: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any] | list[Any] | None:
    try:
        req = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None
    except TimeoutError as exc:
        log("WARN", f"HTTP timeout for {url}: {type(exc).__name__}: {sanitize(exc)}")
    except urllib.error.URLError as exc:
        log("WARN", f"HTTP error for {url}: {type(exc.reason).__name__}: {sanitize(exc.reason)}")
    except Exception as exc:
        log("WARN", f"HTTP failed for {url}: {type(exc).__name__}: {sanitize(exc)}")
    return None


def openclaw_request(path: str, token: str, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{OC}{path}"
    try:
        headers = {AUTH_HEADER_NAME: token, "Content-Type": "application/json"}
        req = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}
    except TimeoutError as exc:
        return {"error": f"timeout: {sanitize(exc)}"}
    except urllib.error.URLError as exc:
        return {"error": f"url_error: {type(exc.reason).__name__}: {sanitize(exc.reason)}"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {sanitize(exc)}"}


def trade(sym: str, direction: str, size: int = 100) -> dict[str, Any]:
    if SKIP_SIDE_EFFECTS or DISABLE_TRADES:
        return {"skipped": "trade side effects disabled"}
    return openclaw_request(
        "/trade/open",
        TRADE_TOKEN,
        method="POST",
        body={"symbol": sym, "direction": direction, "sizeUsd": size, "leverage": 1},
    )


def signals() -> list[dict[str, Any]]:
    result = openclaw_request("/signals/live?limit=5", READ_TOKEN)
    if result.get("error"):
        log("WARN", f"Signals unavailable: {result['error']}")
        return []
    signals_obj = result.get("signals", [])
    return signals_obj if isinstance(signals_obj, list) else []


def status() -> dict[str, Any]:
    result = openclaw_request("/status", READ_TOKEN)
    if result.get("error"):
        log("WARN", f"OpenClaw status unavailable: {result['error']}")
        return {}
    return result


def main() -> int:
    # 1. COLLECT: Fear/Greed
    fg = fetch("https://api.alternative.me/fng/?limit=1")
    if isinstance(fg, dict) and fg.get("data"):
        d = fg["data"][0]
        log("MARKET", f"Fear/Greed: {d['value_classification']} ({d['value']})")
        write_db("fear-greed", {"value": d["value"], "classification": d["value_classification"]})

    # 2. COLLECT: Funding rates + ARB check
    binance = fetch("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1")
    bybit = fetch("https://api.bybit.com/v5/market/funding/history?category=linear&symbol=BTCUSDT&limit=1")

    b_rate = binance[0].get("fundingRate", "0") if isinstance(binance, list) and binance else "0"
    by_rate = bybit.get("result", {}).get("list", [{}])[0].get("fundingRate", "0") if isinstance(bybit, dict) else "0"
    spread = abs(float(b_rate) - float(by_rate))
    log("ARB", f"Spread: {spread * 10000:.2f}bps")
    write_db("funding-arb", {"binance": b_rate, "bybit": by_rate, "spread": spread})

    # 3. CHECK: OpenClaw status before any trade-open side effect.
    stats = status()
    openclaw_ready = bool(stats)
    pos_count = stats.get("openPositions", 0)
    balance = stats.get("balance", {}).get("total", 0)
    log("STATUS", f"Balance: ${balance:.2f} Positions: {pos_count}")

    # 4. TRADE: If arb > 0.05%, open paper arb positions only when OpenClaw is reachable.
    if spread >= 0.0005:
        if not openclaw_ready:
            log("TRADE", f"Arb opportunity {spread * 10000:.2f}bps skipped: OpenClaw status unavailable")
        else:
            log("TRADE", f"Arb opportunity {spread * 10000:.2f}bps - opening position")
            result = trade("BTCUSDT", "LONG")
            if result.get("skipped"):
                log("TRADE", result["skipped"])
            elif "orderId" in result:
                log("EXECUTED", f"BTC LONG {result['symbol']} @ ${result['fillPrice']:.2f}")
                write_db("arb-trade", {"type": "opened", "symbol": "BTCUSDT", "fill": result["fillPrice"]})
            else:
                log("ERROR", f"Trade failed: {result.get('error', '?')}")

    # 5. CHECK: Signals
    sig = signals()
    log("SIGNALS", f"{len(sig)} live signals")
    for s in sig[:3]:
        log("SIGNAL", f"{s['symbol']} {s['direction']} conf={s.get('confidence', '?')}")

    if SKIP_SIDE_EFFECTS:
        print("Side effects skipped", flush=True)
    print("\nNotification bus retired: durable records are in n8n_market_data and cron output", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"arb-trade-pipeline failed safely: {sanitize(type(exc).__name__)}: {sanitize(exc)}", flush=True)
        raise SystemExit(2)
