#!/usr/bin/env python3
# SHIM — approved exec wrapper. Canonical source is ~/.hermes/scripts/sycode_strategy_quarantine_invariant.py.
"""CANONICAL-COPY RULE (t_bad6ee2e): profile-local cron exec shim.

Scheduler resolves scripts under $HERMES_HOME/scripts; the canonical implementation
lives at /home/frank/.hermes/scripts/sycode_strategy_quarantine_invariant.py. Edit
the canonical file, not this wrapper.

Repair card t_9d710783 (2026-08-05): the previous untracked full copy was wiped by
a shared-checkout branch swap, producing "Script not found" at 07:15 local and an
auto-pause. This shim is git-tracked so the same wipe cannot recur.
"""
from __future__ import annotations

import os
import sys

SHARED = "/home/frank/.hermes/scripts/sycode_strategy_quarantine_invariant.py"
os.execv(sys.executable, [sys.executable, SHARED, *sys.argv[1:]])
