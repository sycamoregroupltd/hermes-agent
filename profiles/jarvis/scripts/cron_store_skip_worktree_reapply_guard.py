#!/usr/bin/env python3
# SHIM — approved exec wrapper. Canonical source is ~/.hermes/scripts/cron_store_skip_worktree_reapply_guard.py.
"""CANONICAL-COPY RULE (t_a3024641): profile-local cron exec shim.

Scheduler resolves scripts under $HERMES_HOME/scripts for profile-scoped jobs;
the canonical implementation lives at
/home/frank/.hermes/scripts/cron_store_skip_worktree_reapply_guard.py. Edit the
canonical file, not this wrapper.
"""
from __future__ import annotations

import os
import sys

SHARED = '/home/frank/.hermes/scripts/cron_store_skip_worktree_reapply_guard.py'
os.execv(sys.executable, [sys.executable, SHARED, *sys.argv[1:]])
