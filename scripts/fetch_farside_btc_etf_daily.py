#!/usr/bin/env python3
# CANONICAL SOURCE — keep ~/.hermes/scripts/ and
# ~/.hermes/profiles/jarvis/scripts/ byte-identical (cron loader rejects symlinks).
"""Fetch US spot BTC ETF daily net flows from Farside via Firecrawl.

Direct curl to farside.co.uk is Cloudflare 403 from this host.
Firecrawl CLI (stored user creds) is the sanctioned bypass.

Writes (never invents numbers):
  ~/.hermes/data/now-regime/farside_btc_spot_etf_daily.jsonl
  ~/.hermes/data/now-regime/farside_btc_spot_etf_daily.audit.json
  ~/.hermes/data/now-regime/firecrawl/farside-btc-all.json   (raw scrape)

Does NOT overwrite btc_spot_etf_daily.jsonl (Bitbo; NOW-3 scored file).

Usage:
  python3 ~/.hermes/scripts/fetch_farside_btc_etf_daily.py
  python3 ~/.hermes/scripts/fetch_farside_btc_etf_daily.py --from-json ~/.hermes/data/now-regime/firecrawl/farside-btc-all.json
  python3 ~/.hermes/scripts/fetch_farside_btc_etf_daily.py --self-test

NEVER: fusion boost, trade_intents, live orders, DB writes, new API spend,
       typing numbers from tweets, silent F1 invert.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(os.path.expanduser("~/.hermes/data/now-regime"))
RAW_DIR = DATA / "firecrawl"
OUT_JSONL = DATA / "farside_btc_spot_etf_daily.jsonl"
OUT_AUDIT = DATA / "farside_btc_spot_etf_daily.audit.json"
RAW_JSON = RAW_DIR / "farside-btc-all.json"
BITBO = DATA / "btc_spot_etf_daily.jsonl"

URL = "https://farside.co.uk/bitcoin-etf-flow-all-data/"
SOURCE = (
    "farside.co.uk/bitcoin-etf-flow-all-data (Firecrawl scrape; "
    "direct curl is Cloudflare 403). net_usd = Total column US$m * 1e6."
)
TICKERS = [
    "IBIT", "FBTC", "BITB", "ARKB", "BTCO", "EZBC",
    "BRRR", "HODL", "BTCW", "MSBT", "GBTC", "BTC",
]
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
DATE_RE = re.compile(
    r"^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(20\d{2})$"
)
FIRECRAWL_CANDIDATES = [
    os.environ.get("FIRECRAWL_BIN"),
    str(Path.home() / ".npm-global/bin/firecrawl"),
    "/usr/local/bin/firecrawl",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_num(cell: str) -> float | None:
    s = (cell or "").strip()
    if s in ("", "-", "—", "–", "n/a", "N/A"):
        return None
    s = s.replace(",", "")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse_markdown(md: str) -> list[dict]:
    rows: list[dict] = []
    for line in md.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        m = DATE_RE.match(cells[0])
        if not m:
            continue
        day, mon, year = int(m.group(1)), MONTHS[m.group(2)], int(m.group(3))
        date = f"{year:04d}-{mon:02d}-{day:02d}"
        nums = [parse_num(x) for x in cells[1:]]
        if not nums:
            continue
        total = nums[-1]
        tickers = nums[:-1]
        while len(tickers) < 12:
            tickers.append(None)
        tickers = tickers[:12]
        if total is None:
            filled = [t for t in tickers if t is not None]
            total = sum(filled) if filled else 0.0
        rows.append({
            "date": date,
            "net_usd": float(total) * 1_000_000.0,
            "tickers_usd_m": dict(zip(TICKERS, tickers)),
        })
    # last-write-wins if a date appears twice
    by: dict[str, dict] = {}
    for r in rows:
        by[r["date"]] = r
    return [by[k] for k in sorted(by)]


def find_firecrawl() -> str:
    for c in FIRECRAWL_CANDIDATES:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    which = shutil.which("firecrawl")
    if which:
        return which
    raise FileNotFoundError(
        "firecrawl CLI not found. Install/auth: firecrawl --status. "
        "Do not invent ETF numbers."
    )


def scrape(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    exe = find_firecrawl()
    cmd = [
        exe, "scrape", url,
        "--format", "html,markdown,links",
        "--wait-for", "10000",
        "--proxy", "auto",
        "--country", "GB",
        "--json",
        "-o", str(dest),
    ]
    env = os.environ.copy()
    env.setdefault("FIRECRAWL_NO_ENDPOINT_FEEDBACK", "1")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"firecrawl scrape failed rc={proc.returncode}: "
            f"{(proc.stderr or proc.stdout)[-800:]}"
        )
    if not dest.is_file() or dest.stat().st_size < 1000:
        raise RuntimeError(f"firecrawl wrote empty/tiny file: {dest}")
    return dest


def load_existing() -> dict[str, dict]:
    if not OUT_JSONL.is_file():
        return {}
    out: dict[str, dict] = {}
    for line in OUT_JSONL.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        out[r["date"]] = r
    return out


def merge_pit(parsed: list[dict], seen_at: str) -> tuple[list[dict], dict]:
    prev = load_existing()
    revisions = 0
    rows = []
    for r in parsed:
        old = prev.get(r["date"])
        first = (old or {}).get("first_seen_at") or seen_at
        rec = {
            "date": r["date"],
            "net_usd": r["net_usd"],
            "source": SOURCE,
            "first_seen_at": first,
            "tickers_usd_m": r["tickers_usd_m"],
        }
        if old is not None and abs(float(old.get("net_usd", 0)) - r["net_usd"]) > 0.5:
            rec["revised_at"] = seen_at
            rec["previous_net_usd"] = old.get("net_usd")
            revisions += 1
        rows.append(rec)
    stats = {
        "n": len(rows),
        "new": sum(1 for r in rows if r["date"] not in prev),
        "revisions": revisions,
        "first": rows[0]["date"] if rows else None,
        "last": rows[-1]["date"] if rows else None,
    }
    return rows, stats


def write_outputs(rows: list[dict], extra_audit: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows)
    OUT_JSONL.write_text(body)
    sha = hashlib.sha256(OUT_JSONL.read_bytes()).hexdigest()
    outflow = sum(1 for r in rows if r["net_usd"] < 0)
    audit = {
        "n": len(rows),
        "first": rows[0]["date"] if rows else None,
        "last": rows[-1]["date"] if rows else None,
        "outflow_days": outflow,
        "source": SOURCE,
        "url": URL,
        "sha256": sha,
        "did_not_overwrite": str(BITBO),
        **extra_audit,
    }
    OUT_AUDIT.write_text(json.dumps(audit, indent=2) + "\n")
    print(
        f"FARSIDE-ETF n={len(rows)} {audit['first']}..{audit['last']} "
        f"outflow={outflow} sha={sha[:16]} -> {OUT_JSONL}"
    )


def self_test() -> int:
    md = """
| Date | IBIT | FBTC | BITB | ARKB | BTCO | EZBC | BRRR | HODL | BTCW | MSBT | GBTC | BTC | Total |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 11 Jan 2024 | 111.7 | 227.0 | 237.9 | 65.3 | 17.4 | 50.1 | 29.4 | 10.6 | 1.0 | - | (95.1) | - | 655.3 |
| 13 Aug 2026 | (5.7) | (55.1) | (9.3) | (58.8) | (7.9) | 0.0 | 0.0 | 0.0 | (4.0) | 7.1 | (36.3) | 38.9 | (131.1) |
| Total | 61,151 | 9,899 | 1,994 | 1,283 | 145 | 308 | 329 | 1,082 | 87 | 467 | (27,549) | 2,717 | 51,914 |
"""
    rows = parse_markdown(md)
    assert len(rows) == 2, rows
    assert rows[0]["date"] == "2024-01-11"
    assert abs(rows[0]["net_usd"] - 655_300_000.0) < 1
    assert rows[1]["date"] == "2026-08-13"
    assert abs(rows[1]["net_usd"] - (-131_100_000.0)) < 1
    assert rows[1]["tickers_usd_m"]["IBIT"] == -5.7
    print("self-test OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-json", type=Path, help="parse an existing Firecrawl JSON")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    seen = now_iso()
    if args.from_json:
        raw_path = args.from_json
    else:
        raw_path = scrape(URL, RAW_JSON)

    blob = json.loads(Path(raw_path).read_text())
    md = blob.get("markdown") or ""
    if isinstance(blob.get("data"), dict):
        md = md or blob["data"].get("markdown") or ""
    parsed = parse_markdown(md)
    if len(parsed) < 50:
        raise RuntimeError(
            f"parsed only {len(parsed)} dated rows from {raw_path}; "
            "refusing to write (need the all-data table)."
        )
    rows, stats = merge_pit(parsed, seen)
    write_outputs(rows, {
        "fetched_at": seen,
        "raw": str(raw_path),
        "raw_bytes": Path(raw_path).stat().st_size,
        "merge": stats,
        "paper_only": True,
    })
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"FARSIDE-ETF FAIL: {e}", file=sys.stderr)
        raise SystemExit(2)
