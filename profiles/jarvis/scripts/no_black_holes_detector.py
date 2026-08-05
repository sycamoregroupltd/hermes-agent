#!/usr/bin/env python3
# SHIM — approved exec wrapper. Canonical source is ~/.hermes/scripts/no_black_holes_detector.py.
"""CANONICAL-COPY RULE (t_635a3c9b): profile-local cron exec shim.

Scheduler resolves scripts under $HERMES_HOME/scripts; the canonical implementation
lives at /home/frank/.hermes/scripts/no_black_holes_detector.py. Edit the canonical file, not this wrapper.
"""
from __future__ import annotations

import os
import sys

SHARED = "/home/frank/.hermes/scripts/no_black_holes_detector.py"
os.execv(sys.executable, [sys.executable, SHARED, *sys.argv[1:]])
