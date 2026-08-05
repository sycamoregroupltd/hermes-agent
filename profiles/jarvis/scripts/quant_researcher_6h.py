#!/usr/bin/env python3
# SHIM — approved exec wrapper. Canonical source is ~/.hermes/scripts/quant_researcher_6h.py.
"""CANONICAL-COPY RULE (t_bad6ee2e): profile-local cron exec shim.

Scheduler resolves scripts under $HERMES_HOME/scripts; the canonical implementation
lives at /home/frank/.hermes/scripts/quant_researcher_6h.py. Edit the canonical file,
not this wrapper.

NOTE: This script requires trading-ml venv (polars + duckdb). Cron will use the
gateway's bundled python, but that venv also ships these deps — verify at runtime.
"""
from __future__ import annotations

import os
import sys


SHARED = "/home/frank/.hermes/scripts/quant_researcher_6h.py"
os.execv(sys.executable, [sys.executable, SHARED, *sys.argv[1:]])
