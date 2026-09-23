#!/usr/bin/env python3
# SHIM — approved exec wrapper. Canonical source is ~/.hermes/scripts/verdict_router.py.
"""CANONICAL-COPY RULE (t_748549b7): profile-local cron exec shim.

Scheduler resolves scripts under $HERMES_HOME/scripts; the canonical
implementation lives at /home/frank/.hermes/scripts/verdict_router.py. Edit
the canonical file, not this wrapper.

Repair history: this file was previously a STALE full byte copy (last synced
2026-08-28, diverged from the canonical file by 5,794 bytes / the entire
RiskClassification module added since) with no cron job ever referencing it —
the verdict-router mechanism was never durably scheduled (kanban t_748549b7,
mechanism-liveness key "verdict-router", chronic RED). Replaced with a thin
os.execv shim so the scheduler always resolves the maintained canonical
implementation, matching the kanban_review_required_auto_router.py pattern.
"""
from __future__ import annotations

import os
import sys

SHARED = "/home/frank/.hermes/scripts/verdict_router.py"
os.execv(sys.executable, [sys.executable, SHARED, *sys.argv[1:]])
