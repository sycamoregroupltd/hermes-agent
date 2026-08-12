#!/usr/bin/env python3
"""HIP-3 builder-dex (trade.xyz) funding/OI observation capture — Wave-5 H31 (OBSERVE phase).

Appends hourly funding/OI/mark/premium rows for all xyz: assets with OI>0 to a JSONL
artifact, plus 4x-daily l2Book snapshots for the liquid roll candidates. NO database
access, NO strategy computation — pure observation capture feeding a future prereg
(2 observed roll windows required before any scoring; see mission anchor Wave 5 / H31).

Artifacts: ~/obsidian/sycode-trading/research/artifacts/w5-hip3-observe-2026-08-05/
Liveness contract: non-zero exit on ANY failure (no_agent cron: exit code is the only
liveness signal — do not swallow exceptions). Freshness lives in status.json.
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ART = Path.home() / "obsidian/sycode-trading/research/artifacts/w5-hip3-observe-2026-08-05"
API = "https://api.hyperliquid.xyz/info"
BOOK_ASSETS = ["xyz:CL", "xyz:BRENTOIL", "xyz:GOLD", "xyz:SILVER", "xyz:NATGAS"]
BOOK_HOURS = {0, 6, 12, 18}


def post(payload: dict) -> object:
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt + 1))


def main() -> None:
    now = datetime.now(timezone.utc)
    ART.mkdir(parents=True, exist_ok=True)

    meta, ctxs = post({"type": "metaAndAssetCtxs", "dex": "xyz"})
    universe = meta["universe"]
    if len(universe) != len(ctxs):
        raise RuntimeError(f"universe/ctx length mismatch {len(universe)} vs {len(ctxs)}")

    rows = []
    for asset, ctx in zip(universe, ctxs):
        oi = float(ctx.get("openInterest") or 0)
        if oi <= 0:
            continue
        rows.append(
            {
                "ts": now.isoformat(timespec="seconds"),
                "name": asset["name"],  # universe names already carry the xyz: prefix
                "funding": ctx.get("funding"),
                "openInterest": ctx.get("openInterest"),
                "markPx": ctx.get("markPx"),
                "oraclePx": ctx.get("oraclePx"),
                "premium": ctx.get("premium"),
                "impactPxs": ctx.get("impactPxs"),
                "dayNtlVlm": ctx.get("dayNtlVlm"),
            }
        )
    if not rows:
        raise RuntimeError("0 assets with OI>0 parsed — API shape changed or dex empty")

    with open(ART / "funding_oi_hourly.jsonl", "a") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    books_taken = 0
    if now.hour in BOOK_HOURS:
        for coin in BOOK_ASSETS:
            book = post({"type": "l2Book", "coin": coin})
            day_dir = ART / "l2book" / now.strftime("%Y-%m-%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{coin.replace(':', '_')}_{now.strftime('%H%M')}.json"
            (day_dir / fname).write_text(json.dumps(book))
            books_taken += 1
            time.sleep(1.2)  # HL rate limit: 429s at ~3 req/s

    status = {
        "last_run": now.isoformat(timespec="seconds"),
        "assets_captured": len(rows),
        "l2books_taken": books_taken,
        "purpose": "W5-H31 OBSERVE phase — no ledger row, no scoring; prereg requires 2 observed rolls",
        "next_roll_windows": "CLU26 expiry 2026-08-20 (window ~Aug 6-14); CLV26 2026-09-22 (~Sep 8-16)",
    }
    (ART / "status.json").write_text(json.dumps(status, indent=1))


if __name__ == "__main__":
    # Watchdog contract: silent stdout on success; failures print to stdout
    # (delivered) AND exit non-zero (the only liveness signal for no_agent crons).
    try:
        main()
    except Exception as exc:
        import traceback

        print(f"HIP3-OBSERVE FAILURE: {exc}")
        traceback.print_exc(file=sys.stdout)
        sys.exit(1)
