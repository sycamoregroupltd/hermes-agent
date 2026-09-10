#!/usr/bin/env python3
# SHIM — approved exec wrapper. Canonical source is ~/.hermes/scripts/route_models_kanban_pin.py.
"""CANONICAL-COPY RULE (t_bad6ee2e pattern, applied here per t_f2164cd2 Blocker 1):
profile-local cron exec shim.

cron/scheduler_script.py::_resolve_script_path resolves a job's relative
``script`` field against THAT JOB'S PROFILE HERMES_HOME
(``~/.hermes/profiles/<profile>/scripts/``), not the global
``~/.hermes/scripts/``. An absolute path does not work around this either —
the containment check (``path.relative_to(scripts_dir_resolved)``) rejects
anything outside the profile-scoped scripts dir. So a job registered on the
fleet-engineer profile with ``script: route_models_kanban_pin.py`` silently
resolves to this file's location and nowhere else.

Edit the canonical implementation at
``/home/frank/.hermes/scripts/route_models_kanban_pin.py``, not this wrapper.
"""
from __future__ import annotations

import os
import sys

SHARED = "/home/frank/.hermes/scripts/route_models_kanban_pin.py"
os.execv(sys.executable, [sys.executable, SHARED, *sys.argv[1:]])
