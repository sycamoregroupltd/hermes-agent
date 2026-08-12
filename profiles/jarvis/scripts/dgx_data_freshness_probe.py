#!/usr/bin/env python3
# SHIM — approved exec wrapper. Canonical source is ~/.hermes/scripts/dgx_data_freshness_probe.py.
# Canonical-copy rule t_7fec9a7c: edit this canonical file, not profile-local shims.
from __future__ import annotations
import os, sys
SHARED = "/home/frank/.hermes/scripts/dgx_data_freshness_probe.py"
os.execv(sys.executable, [sys.executable, SHARED, *sys.argv[1:]])
