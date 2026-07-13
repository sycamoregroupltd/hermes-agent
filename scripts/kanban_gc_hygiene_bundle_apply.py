#!/usr/bin/env python3
"""Approved apply wrapper for kanban_gc_hygiene_bundle.py.

Canonical copy — installed at ~/.hermes/scripts/ so the cron scheduler
can reach it via no_agent script resolution.

Do not schedule or run this wrapper until t_1177e620 has PM sign-off after the
dry-run comment. The implementation lives in kanban_gc_hygiene_bundle.py, which
remains dry-run by default.
"""

from __future__ import annotations

import os
import runpy
from pathlib import Path

os.environ["KANBAN_GC_HYGIENE_APPLY"] = "1"
runpy.run_path(
    str(Path("/home/frank/.hermes/profiles/jarvis/scripts/kanban_gc_hygiene_bundle.py")),
    run_name="__main__",
)
