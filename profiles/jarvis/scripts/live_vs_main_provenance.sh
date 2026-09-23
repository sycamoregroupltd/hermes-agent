#!/usr/bin/env bash
# SHIM — jarvis cron resolver for weekly live-vs-main provenance (t_403eb2aa).
# Canonical: /home/frank/.hermes/scripts/live_vs_main_provenance.py
# cron/scheduler_script.py::_resolve_script_path is profile-scoped; this file
# must be a real exec wrapper, never a symlink.
exec /usr/bin/env python3 /home/frank/.hermes/scripts/live_vs_main_provenance.py "$@"
