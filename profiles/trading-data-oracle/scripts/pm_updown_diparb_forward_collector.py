#!/usr/bin/env python3
"""pm_updown_diparb_forward_collector.py — no_agent wrapper for job cd56040ea942.

WHY THIS WRAPPER EXISTS: the live job was an LLM prompt that shelled out to
run_continuous.py. Every 11m fire spent openai-codex quota and failed with
`RuntimeError: HTTP 429: The usage limit has been reached` (failure_streak 18
on 2026-09-21, jarvis-os/t_fc43cf05 / t_aa9eaaa1) BEFORE the collector ran.
JSONL under wallet-intel/pm-updown-diparb/data/ last wrote 15:10Z.

cron_untracked_script_guard / the scheduler both resolve a relative `script`
against THIS profile's scripts/ dir. The collector lives outside that dir, so
this tracked wrapper is the cron entry point; it execs the existing paper
collector unchanged.

PAPER ONLY. Read-only public Polymarket/CLOB/RTDS endpoints. Zero credentials.
No trades. Reversible: `hermes -p trading-data-oracle cron --accept-hooks edit
cd56040ea942 --agent` restores the LLM wrapper (script can stay).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

COLLECTOR_DIR = Path("/home/frank/wallet-intel/pm-updown-diparb")
PYTHON = COLLECTOR_DIR / ".venv" / "bin" / "python"
SCRIPT = COLLECTOR_DIR / "run_continuous.py"
ARGS = ["--assets", "btc,eth,sol,xrp", "--tfs", "5m,15m", "--minutes", "12"]


def main() -> int:
    if not PYTHON.is_file():
        print(f"FATAL: collector venv python missing at {PYTHON}", file=sys.stderr)
        return 1
    if not SCRIPT.is_file():
        print(f"FATAL: collector script missing at {SCRIPT}", file=sys.stderr)
        return 1
    os.chdir(COLLECTOR_DIR)
    os.execv(str(PYTHON), [str(PYTHON), str(SCRIPT), *ARGS])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
