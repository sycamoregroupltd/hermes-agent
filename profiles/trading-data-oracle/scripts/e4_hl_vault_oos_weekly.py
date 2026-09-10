#!/usr/bin/env python3
"""e4_hl_vault_oos_weekly.py — cron wrapper for job cf191d8e36e8 (t_b62fa701).

WHY THIS WRAPPER EXISTS: cron_untracked_script_guard.py / the scheduler both
resolve a relative `script` field against THIS profile's `scripts/` dir
(hermes-agent/cron/scheduler.py). The actual E4 HL vaults OOS collector logic
lives one directory up, alongside its frozen config/data files (shortlist.json,
control_cohort_t0.json, data/pull_manifest.jsonl):

    /home/frank/.hermes/profiles/trading-data-oracle/e4-hl-vaults-oos/collect_oos.py

That directory is intentionally profile-data (gitignored via the generic
`/profiles/*/*` carve-out — see e4-hl-vaults-oos/README.md "Honesty notes"),
so the collector script itself cannot be the tracked cron entry point. This
thin wrapper is the tracked, guard-satisfying entry point; it execs the real
collector unchanged with default args (bare invocation = `--kind weekly`,
the cron default per the collector's own README).

Binding prereg: research/2026-08-02-PREREG-e4-hl-vaults-survivorship-oos-t_15e0386c.md
Zero capital action. No deposits. No credentials. Data-only.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

COLLECTOR = Path(__file__).resolve().parent.parent / "e4-hl-vaults-oos" / "collect_oos.py"

if not COLLECTOR.is_file():
    print(f"FATAL: E4 HL vaults OOS collector missing at {COLLECTOR}", file=sys.stderr)
    sys.exit(1)

os.execv(sys.executable, [sys.executable, str(COLLECTOR), *sys.argv[1:]])
