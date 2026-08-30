#!/usr/bin/env python3
"""Thin shim: runs the canonical board-health 14d monitor.

Canonical logic lives at /home/frank/.hermes/scripts/board_health_14d_monitor.py;
this shim exists because hermes cron --script resolves relative names under the
owning profile's scripts dir (t_0e0bcda9 fleet pattern).
"""
import runpy
import sys

sys.exit(runpy.run_path("/home/frank/.hermes/scripts/board_health_14d_monitor.py", run_name="__main__") or 0)
