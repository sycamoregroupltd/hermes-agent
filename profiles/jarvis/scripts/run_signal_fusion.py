#!/usr/bin/env python3
"""Cron wrapper for Signal Fusion Engine — runs every 15min (paper-only).

REPOINTED WRAPPER (t_7ef51c23): thin exec-shim that hands off to the canonical
entrypoint execution/run_signal_fusion_cron.py so every write flows through
persist_trade_setup()'s fail-closed calibration gate and the honest
suppressed_by_gate / persist_failures counter contract.

The previous wrapper inlined its own INSERT path and printed
"! persist refused/failed" for every signal — conflating fail-closed gate
suppressions with real write failures. Do NOT regress: this shim must exec the
canonical script, not re-implement the pipeline.
"""
from __future__ import annotations

import os
import sys

# ── Environment seams for the fail-closed calibration gate ────────────────
# These MUST match the FUSION_GATE_* constants in execution/fusion_calibration_gate.py.
# setdefault keeps the prod cron behavior intact but lets a reviewer dry-run with
# overrides. An unset/missing seam stays BLOCKED (fail-closed), so these defaults
# are the canonical production report directories.
os.environ.setdefault('FUSION_GATE_QUANT_REPORT_DIR',
    '/home/frank/.hermes/profiles/jarvis/cron/output/13c1f9279025')
os.environ.setdefault('FUSION_GATE_F052_REPORT_DIR',
    '/home/frank/.hermes/profiles/jarvis/cron/output/f05227128ac2')
os.environ.setdefault('FUSION_GATE_QUANT_MAX_AGE_MINUTES', '720')
os.environ.setdefault('FUSION_GATE_F052_MAX_AGE_MINUTES', '720')

# Must be set BEFORE the engine import: the engine reads WRITE_TRADE_SETUPS at
# import time (execution/signal_fusion_engine.py:55). setdefault keeps cron
# behavior (writes on) but lets a reviewer dry-run this shim with
# WRITE_TRADE_SETUPS=false.
os.environ.setdefault('WRITE_TRADE_SETUPS', 'true')

# ── Hand off to the canonical entrypoint ────────────────────────────────────
CANONICAL = '/home/frank/sycode-trading/execution/run_signal_fusion_cron.py'

# Add the repo root so the engine's package-relative import shims resolve when
# the script is run directly (it uses try/except import shims but needs the
# parent on sys.path for `from execution.signal_fusion_engine import ...`).
sys.path.insert(0, '/home/frank/sycode-trading')

if not os.path.isfile(CANONICAL):
    print(f'FATAL: canonical signal-fusion entrypoint missing: {CANONICAL}',
          file=sys.stderr)
    print('Expected on origin/main at execution/run_signal_fusion_cron.py',
          file=sys.stderr)
    raise SystemExit(1)

# Exec the canonical script with the original interpreter + argv. stdout and the
# exit code pass through verbatim, satisfying the no_agent cron contract.
os.execv(sys.executable, [sys.executable, CANONICAL, *sys.argv[1:]])
