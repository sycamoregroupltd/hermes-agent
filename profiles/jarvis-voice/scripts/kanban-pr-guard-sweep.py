#!/usr/bin/env python3
"""Approved exec shim for the canonical kanban-pr-guard-sweep writer (t_9799c507).

The devops profile has NO live gateway ticker, so the sweep is scheduled on the
live jarvis-voice profile (same profile that runs the deterministic
verdict-router). This real file under HERMES_HOME/scripts/ execs the canonical
script so there is exactly one source of truth.
"""
from __future__ import annotations

import os
import sys

SHARED = "/home/frank/.hermes/scripts/kanban-pr-guard-sweep.py"
os.execv(sys.executable, [sys.executable, SHARED, *sys.argv[1:]])
