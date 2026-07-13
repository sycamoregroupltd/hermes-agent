#!/usr/bin/env bash
# CANONICAL SOURCE — cross-pm-fanout.sh
# Part of the cross-PM learning fabric (kanban t_65a992ed).
#
# Hourly cron wrapper (jarvis-os-pm profile) that:
#   1. cross-pm-fanout.py  — propagate 'new' lessons onto target PM boards as
#      lightweight pointer comments (idempotent; non-free model pinned on the job
#      to avoid competing with interactive free-tier traffic — error-learner 429 guard).
#   2. lesson-archive.py   — rotate superseded + oldest lessons to keep <=100 active.
# Reversible: delete the job or repoint 'script' to either step individually.
set -uo pipefail
SCRIPTS="/home/frank/.hermes/scripts"
python3 "${SCRIPTS}/cross-pm-fanout.py" --once
python3 "${SCRIPTS}/lesson-archive.py"
