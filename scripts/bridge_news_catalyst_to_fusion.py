#!/usr/bin/env python3
"""
bridge_news_catalyst_to_fusion.py

Standalone re-runnable bridge that pushes the News Catalyst agent's latest
local output (state.json) into the two fusion-readable Postgres tables:

  1. public.market_news_cache  (cache_type='current')  -> read by the PYTHON
     fusion engine (execution/signal_fusion_engine.py) for newsSentiment.
  2. public.market_news        (per-coin rows)          -> read by the TS
     SignalFusionEngine.fetchNewsSentiment for per-coin sentiment + catalysts.

WHY THIS EXISTS
---------------
The News Catalyst agent (news_sentiment_catalyst.py) now calls
persist_to_fusion() at the end of every run, so going forward every live run
lands data in both tables. This script is the *backfill / on-demand* counterpart:
it reads the last-run state.json and (re)pushes it without re-fetching news or
recomputing sentiment, which is useful to:
  * backfill the 2.4M historical journeys that never had news metadata,
  * re-push after a DB reset,
  * manually trigger persistence from cron without a full news crawl.

It is idempotent: cache row is an upsert; per-coin rows use a per-day stable
url so re-runs are no-ops (ON CONFLICT DO NOTHING), and stale catalyst rows
older than 2h are pruned.

Usage:
    python3 bridge_news_catalyst_to_fusion.py            # live push
    python3 bridge_news_catalyst_to_fusion.py --dry-run  # print what would happen
    PERSIST_TO_FUSION=false python3 ...                  # disable

Exit code 0 on success (or dry-run). Non-zero only on unexpected failure.
"""
from __future__ import annotations

import argparse
import os
import sys

# Reuse the agent's persistence logic so there is a single source of truth.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import news_sentiment_catalyst as nsc  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the rows that would be written without touching the DB.",
    )
    parser.add_argument(
        "--state", default=nsc.STATE_FILE,
        help=f"Path to the catalyst state.json (default: {nsc.STATE_FILE})",
    )
    args = parser.parse_args()

    import json
    try:
        with open(args.state, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        print(f"[bridge] state file not found: {args.state}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"[bridge] failed to read state: {e}", file=sys.stderr)
        return 2

    composite = state.get("composite_full") or state.get("composite", {})
    news_items = state.get("news_items", [])
    if not composite:
        print("[bridge] no composite data in state — nothing to push.", file=sys.stderr)
        return 0

    summary = nsc.persist_to_fusion(composite, news_items, state, dry_run=args.dry_run)
    if not args.dry_run and summary.get("errors"):
        print(f"[bridge] completed with {len(summary['errors'])} error(s): "
              f"{summary['errors']}", file=sys.stderr)
        # Still exit 0 — the agent tolerates persistence errors by design.
    return 0


if __name__ == "__main__":
    sys.exit(main())
