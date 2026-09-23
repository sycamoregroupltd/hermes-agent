#!/usr/bin/env python3
# SHIM — approved exec wrapper for the fleet config-validity watchdog (t_24480103).
# Canonical source is /home/frank/.hermes/scripts/check_config_validity.py.
#
# CANONICAL-COPY RULE (t_bad6ee2e pattern, applied here per t_24480103 Blocker D1/D2):
# cron/scheduler_script.py::_resolve_script_path resolves a job's relative ``script``
# field against THAT JOB'S PROFILE HERMES_HOME (~/.hermes/profiles/<profile>/scripts/),
# not the global ~/.hermes/scripts/. An absolute path does not work around this either
# — the containment check (path.relative_to(scripts_dir_resolved)) rejects anything
# outside the profile-scoped scripts dir. So a job registered on the jarvis profile
# with ``script: check_config_validity.py`` resolves to this file and nowhere else.
#
# This job lives on jarvis (a LIVE cron ticker, gateway-connected to Discord) so it
# actually fires every 15m and delivers failures to discord:#critical-alerts. Edit the
# canonical implementation at /home/frank/.hermes/scripts/check_config_validity.py,
# not this wrapper.
from __future__ import annotations

import os
import sys

SHARED = "/home/frank/.hermes/scripts/check_config_validity.py"
os.execv(sys.executable, [sys.executable, SHARED, *sys.argv[1:]])
