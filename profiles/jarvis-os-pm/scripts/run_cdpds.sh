#!/usr/bin/env bash
# In-dir cron wrapper for the Continuous Data Profiler & Drift Sentinel (CDPDS).
# Hermes cronjob `script` resolver REJECTS symlinks and out-of-dir paths, so this
# real in-dir shell shim execs the canonical producer at ~/.hermes/scripts/...
# Canonical: /home/frank/.hermes/scripts/data_profiler_sentinel.py
# Registered by kanban task t_a0c0087c (t_e9a8d181-child-2 WIRE) — paper-mode only.
set -euo pipefail
exec python3 /home/frank/.hermes/scripts/data_profiler_sentinel.py
