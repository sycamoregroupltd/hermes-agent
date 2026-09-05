#!/usr/bin/env python3
"""Profile-local exec shim for the canonical board-health monitor."""
from __future__ import annotations

import os
import sys

CANONICAL = "/home/frank/.hermes/scripts/board_health_14d_monitor.py"
os.execv(sys.executable, [sys.executable, CANONICAL, *sys.argv[1:]])
