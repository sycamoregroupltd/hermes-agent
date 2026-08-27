#!/usr/bin/env python3
# SHIM — approved exec wrapper. Canonical: ~/.hermes/scripts/fleet-daily-digest-to-board.py
from __future__ import annotations
import os, sys
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/fleet-daily-digest-to-board.py", *sys.argv[1:]])
