#!/usr/bin/env bash
# in-dir wrapper for the tier1-sample-gate monitor (cron id 6c7d9976ffc3).
# The cron resolver only resolves `script` relative to this profile's scripts/
# directory and rejects symlinks / out-of-dir paths. The canonical script lives
# at ~/.hermes/scripts/tier1_sample_gate.py (shared host location); this shim
# execs it verbatim so window/gate behavior is owned by that single source.
#
# WIDENED 2026-08-13 (t_053d6f6d): window 30d->60d on the canonical script is
# driven by its TIER1_GATE_WINDOW_DAYS default (now 60). No schedule/provider/
# credential mutation here — re-registration only of the previously-paused cron.
set -euo pipefail
exec /usr/bin/env python3 /home/frank/.hermes/scripts/tier1_sample_gate.py "$@"
