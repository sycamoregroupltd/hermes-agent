#!/usr/bin/env python3
# SHIM — approved exec wrapper. Canonical source is ~/.hermes/scripts/alert-spool-drain-digest.py.
# Canonical-copy rule t_7fec9a7c: edit the canonical file, not this shim.
# Cron resolves script names PROFILE-LOCALLY first, so a job on this profile
# cannot see a global-only script without this wrapper.
from __future__ import annotations
import os, sys
SHARED = "/home/frank/.hermes/scripts/alert-spool-drain-digest.py"
os.execv(sys.executable, [sys.executable, SHARED, *sys.argv[1:]])
