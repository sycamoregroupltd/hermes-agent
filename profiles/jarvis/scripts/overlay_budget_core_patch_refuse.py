#!/usr/bin/env python3
# Profile-local 15m guard-bundle adapter for overlay-budget core-patch refuse.
# Real file (not a symlink). Never writes overlay-budget receipts.
import os
import sys

os.execv(
    sys.executable,
    [
        sys.executable,
        "/home/frank/.hermes/scripts/overlay_budget_core_patch_refuse.py",
        *sys.argv[1:],
    ],
)
