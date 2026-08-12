#!/bin/bash
# needs_input_escalation.sh -- wrapper for no_agent cron
# Calls needs_input_reporter.py --escalate-only --threshold-hours 6
# Args cannot be baked into the `script` field of a no_agent cron job because
# _run_job_script treats the entire string as a filename. This shell wrapper
# is the official workaround: no_agent sees a .sh file, bash interprets the args.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/needs_input_reporter.py" --escalate-only --threshold-hours 6
