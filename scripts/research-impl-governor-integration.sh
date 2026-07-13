#!/bin/bash
set -euo pipefail
GAP_ANALYSIS="/home/frank/.hermes/data/research-impl-gap-analysis.md"
CHILD_CREATOR="/home/frank/.hermes/scripts/research-impl-child-creator.py"
BOARD="${HERMES_KANBAN_BOARD:-upero}"
if [ ! -f "$GAP_ANALYSIS" ]; then exit 0; fi
python3 "$CHILD_CREATOR" --board "$BOARD"
EXIT_CODE=$?
if [ $EXIT_CODE -eq 0 ]; then echo "[elon-governor] Child creator completed successfully"
else echo "[elon-governor] Child creator exited with code $EXIT_CODE" >&2; fi
exit $EXIT_CODE
