#!/usr/bin/env python3
"""Profile-local shim for the T6 triage-monitor snapshot (kanban t_0e0bcda9).

Migrated from fleet-analyst gateway to jarvis scheduler (t_cbdd35f4, 2026-08-29).
Canonical logic lives at /home/frank/.hermes/scripts/sycode-triage-snapshot.py;
this shim execs it so there is exactly one source of truth (same pattern as
board_pm_triage_sycode_trading.sh -> board_pm_triage_visibility.py).
"""
import runpy, sys

sys.path.insert(0, '/home/frank/.hermes/scripts')
runpy.run_path('/home/frank/.hermes/scripts/sycode-triage-snapshot.py', run_name='__main__')
