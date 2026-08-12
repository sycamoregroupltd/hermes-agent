#!/usr/bin/env python3
"""No-agent cron wrapper for kanban_transient_recovery cooldown mode."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import kanban_transient_recovery  # noqa: E402

if __name__ == "__main__":
    sys.argv = [
        sys.argv[0],
        "--mode",
        "cooldown",
        "--cooldown-seconds",
        "3600",
        "--max-rounds",
        "3",
    ]
    raise SystemExit(kanban_transient_recovery.main())
