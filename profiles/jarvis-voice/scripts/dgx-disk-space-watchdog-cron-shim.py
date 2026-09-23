#!/usr/bin/env python3
# Exec-shim: resolves to canonical board-bound watchdog in /home/frank/.hermes/scripts/.
# Proven pattern (t_bad6ee2e) — scheduler resolves relative script against this profile's
# scripts/ dir; absolute paths are rejected, so the shim bridges the two.
import os, sys
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/rtb-dgx-disk-space-watchdog.py"])
