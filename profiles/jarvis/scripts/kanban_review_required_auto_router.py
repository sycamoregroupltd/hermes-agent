#!/usr/bin/env python3
# SHIM — approved exec wrapper. Canonical source is ~/.hermes/scripts/kanban_review_required_auto_router.py.
"""CANONICAL-COPY RULE (t_bad6ee2e): profile-local cron exec shim.

Scheduler resolves scripts under $HERMES_HOME/scripts; the canonical implementation
lives at /home/frank/.hermes/scripts/kanban_review_required_auto_router.py. Edit the
canonical file, not this wrapper.

Repaired t_742d0c86 (2026-08-05): job 7ffdf1f12d4e was dead-pinned because an
untracked bulk copy vanished; this tracked shim replaces the bulk copy so the
scheduler always resolves the canonical implementation.
"""
from __future__ import annotations

import os
import sys

SHARED = "/home/frank/.hermes/scripts/kanban_review_required_auto_router.py"
os.execv(sys.executable, [sys.executable, SHARED, *sys.argv[1:]])
