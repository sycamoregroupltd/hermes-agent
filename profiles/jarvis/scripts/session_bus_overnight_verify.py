#!/usr/bin/env python3
# Exec shim — jarvis profile cron entry point. Runs the CANONICAL script at
# /home/frank/.hermes/scripts/session_bus_overnight_verify.py (CANONICAL-COPY
# RULE: edit the canonical file, never this shim). kanban t_09c75a14.
import runpy
import sys

runpy.run_path(
    "/home/frank/.hermes/scripts/session_bus_overnight_verify.py",
    run_name="__main__",
)
