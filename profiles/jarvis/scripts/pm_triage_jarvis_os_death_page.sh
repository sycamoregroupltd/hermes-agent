#!/usr/bin/env bash
# SHIM — jarvis cron resolver for pm-triage-jarvis-os death pager (t_e61d37c8 AC3).
# Canonical: /home/frank/.hermes/scripts/pm_triage_jarvis_os_death_page.py
# cron/scheduler_script.py::_resolve_script_path is profile-scoped; this file
# must be a real exec wrapper, never a symlink.
exec /usr/bin/env python3 /home/frank/.hermes/scripts/pm_triage_jarvis_os_death_page.py "$@"
