#!/usr/bin/env python3
# CANONICAL-COPY RULE: Jarvis cron exec shim for swap-after-comfy-watch.
# The scheduler requires a real in-profile file; the implementation is the
# reviewed central source under ~/.hermes/scripts/. No process or service
# mutation is performed by the implementation.
from __future__ import annotations

import os
import sys

CENTRAL = "/home/frank/.hermes/scripts/swap-after-comfy-watch.py"
os.execv("/usr/bin/python3", ["/usr/bin/python3", CENTRAL, *sys.argv[1:]])
