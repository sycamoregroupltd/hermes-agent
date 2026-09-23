#!/usr/bin/env bash
# SHIM — jarvis cron resolver for U6 S5 dual-dispatch page (t_c9146573).
# Canonical: /home/frank/.hermes/scripts/dispatcher-singleton-page.sh
# cron/scheduler_script.py::_resolve_script_path is profile-scoped; this file
# must be a real exec wrapper, never a symlink.
exec bash /home/frank/.hermes/scripts/dispatcher-singleton-page.sh "$@"
