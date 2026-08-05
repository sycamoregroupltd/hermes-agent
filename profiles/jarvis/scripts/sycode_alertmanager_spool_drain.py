#!/usr/bin/env python3
# SHIM — approved exec wrapper. Canonical source is ~/.hermes/scripts/sycode_alertmanager_spool_drain.py.
"""CANONICAL-COPY RULE (t_bad6ee2e): profile-local cron exec shim.

Scheduler resolves scripts under $HERMES_HOME/scripts; the canonical implementation
lives at /home/frank/.hermes/scripts/sycode_alertmanager_spool_drain.py. Edit the
canonical file, not this wrapper.
"""
from __future__ import annotations

import os
import sys


SHARED = "/home/frank/.hermes/scripts/sycode_alertmanager_spool_drain.py"
os.execv(sys.executable, [sys.executable, SHARED, *sys.argv[1:]])
