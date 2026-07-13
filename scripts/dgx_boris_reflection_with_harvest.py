#!/usr/bin/env python3
# CANONICAL SOURCE — dgx_boris_reflection_with_harvest.py
# Part of the cross-PM learning fabric (kanban t_65a992ed).
#
# Wires the harvest tail-step onto the existing dgx-boris-reflection cron (jarvis
# profile): run the canonical Boris reflection collector, then harvest any new
# verified lessons from BORIS-EVIDENCE-QUEUE.md into the shared-memory/lessons/
# store. Reversible: repoint the cron 'script' back to dgx_boris_reflection.py.
import subprocess
import sys

CANON = "/home/frank/.hermes/scripts/dgx_boris_reflection.py"
HARVEST = "/home/frank/.hermes/scripts/harvest-boris-to-lesson.sh"

rc = subprocess.run([sys.executable, CANON, *sys.argv[1:]], timeout=900).returncode
if rc != 0:
    # Reflection collector non-fatal: still harvest whatever the queue holds.
    print(f"[warn] dgx_boris_reflection exited {rc}; proceeding to harvest anyway")

r = subprocess.run(["bash", HARVEST], timeout=300)
sys.exit(r.returncode)
