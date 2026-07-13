#!/bin/bash
set -euo pipefail

# This script is the entry point for Elon's proactive, never-idle governor loop.
# It's triggered by a cron job and is responsible for initiating a productive action.

/home/frank/.local/bin/hermes -p elon --prompt-file /home/frank/.hermes/profiles/elon/prompts/proactive_governor.txt run
