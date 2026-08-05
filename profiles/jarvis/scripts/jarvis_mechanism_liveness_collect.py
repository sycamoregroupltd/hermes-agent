#!/usr/bin/env python3
# CANONICAL-COPY RULE: This is a thin exec shim only.
# The canonical source lives at /home/frank/.hermes/scripts/jarvis_mechanism_liveness_collect.py
# Edit the canonical copy there; do NOT duplicate logic here.
# cron/scheduler.py resolves --script relative to the running profile's $HERMES_HOME/scripts.
import subprocess, sys
subprocess.run([sys.executable, "/home/frank/.hermes/scripts/jarvis_mechanism_liveness_collect.py"] + sys.argv[1:])
