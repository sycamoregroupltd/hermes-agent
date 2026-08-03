#!/bin/bash
# cron_report_latest.sh — Resolve the latest cron report artifact for a job ID.
#
# Usage: cron_report_latest.sh <job_id> [output_dir]
#
# Default output_dir: /home/frank/.hermes/profiles/jarvis/cron/output
#
# Resolution order:
#   1. Subdir layout: $output_dir/<id>/ → newest regular file by mtime
#   2. Flat fallback: $output_dir/<id>_*.txt → newest by mtime
#   3. Fail loudly:  stderr message + exit 3 (never returns empty/silent)
#
# Tirith-safe: no ls | grep, no pipe to python3 -c, no eval.

set -eu

id="${1:?usage: cron_report_latest.sh <job_id> [output_dir]}"
outdir="${2:-/home/frank/.hermes/profiles/jarvis/cron/output}"

# --- Subdir layout ---
subdir="$outdir/$id"
if [ -d "$subdir" ]; then
  # Null-delimited pipeline: find, sort by mtime, take last, strip null.
  candidate=$(find "$subdir" -maxdepth 1 -type f -printf '%T@ %p\0' 2>/dev/null \
    | sort -z -t' ' -k1,1n \
    | tail -z -n 1 \
    | tr -d '\0')
  # After tr -d '\0', candidate is "<mtime> <path>"; extract from second field.
  path_only="${candidate#* }"
  if [ -n "$path_only" ] && [ -f "$path_only" ]; then
    printf '%s' "$path_only"
    exit 0
  fi
fi

# --- Flat fallback: ${id}_*.txt, newest by mtime ---
candidate=$(find "$outdir" -maxdepth 1 -type f -name "${id}_*.txt" -printf '%T@ %p\0' 2>/dev/null \
  | sort -z -t' ' -k1,1n \
  | tail -z -n 1 \
  | tr -d '\0')
path_only="${candidate#* }"
if [ -n "$path_only" ] && [ -f "$path_only" ]; then
  printf '%s' "$path_only"
  exit 0
fi

# --- Nothing found — loud fail ---
echo "cron_report_latest.sh: REPORT-MISSING: no artifact for job id '$id' in '$outdir'" >&2
exit 3
