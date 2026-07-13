#!/usr/bin/env python3
"""Cron wrapper for Signal Fusion Engine — runs every 15min."""
import os, sys
sys.path.insert(0, '/home/frank/sycode-trading')
# Must be set BEFORE the engine import: the engine reads WRITE_TRADE_SETUPS at
# import time. setdefault keeps cron behavior (writes on) but lets a reviewer
# dry-run this script with WRITE_TRADE_SETUPS=false.
os.environ.setdefault('WRITE_TRADE_SETUPS', 'true')

from execution.signal_fusion_engine import (
    load_latest_signals, load_funding_signal, load_macro_context,
    load_news_sentiment, load_oi_signal, load_price_context,
    build_trade_setup, compute_conviction, persist_trade_setup
)

signals = load_latest_signals(hours=6, limit=50)
print(f"Loaded {len(signals)} signals")

setups = []
for s in signals:
    ts = build_trade_setup(s)
    if ts:
        # Engine-path persist (PR #372): advisory-locked upsert keyed on
        # signal_id — UPDATE first, INSERT only when absent; refuses setups
        # with a missing signal_id in write mode. Replaces the old raw INSERT
        # that created signal_id duplicates.
        if persist_trade_setup(ts):
            setups.append(ts)
        else:
            print(f"  ! persist refused/failed: {ts.get('symbol')} {ts.get('timeframe')} signal_id={ts.get('signal_id')!r}")

high = [ts for ts in setups if ts['conviction'] >= 0.65]
print(f"Written: {len(setups)} trade setups | High conviction: {len(high)}")
for ts in sorted(high, key=lambda x: x['conviction'], reverse=True)[:5]:
    print(f"  ★ {ts['symbol']} {ts['timeframe']} {ts['direction']} — conviction={ts['conviction']}")
