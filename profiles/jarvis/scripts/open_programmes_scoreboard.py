#!/usr/bin/env python3
# SHIM — approved exec wrapper. Canonical source is ~/.hermes/scripts/open_programmes_scoreboard.py.
# Canonical-copy rule (t_86f9de5d / parent t_44a7cb63): edit the canonical file, not this shim.
# Cron resolves script names PROFILE-LOCALLY first, so a job on this profile
# cannot see a global-only script without this wrapper.
#
# hermes cron --no-agent --script invokes this file with NO extra CLI args,
# so this shim hard-codes --write (the canonical script's own CLI still
# defaults to --dry-run for safety when a human runs it directly).
from __future__ import annotations
import os, sys
SHARED = "/home/frank/.hermes/scripts/open_programmes_scoreboard.py"
extra_args = sys.argv[1:] or ["--write"]
os.execv(sys.executable, [sys.executable, SHARED, *extra_args])
