#!/usr/bin/env bash
# Append a lesson to the fleet collective-memory log.
# Usage: PM_NAME=<pm> log-collective-memory.sh "TAG" "Brief title" "Context ... lesson ..."
set -euo pipefail

MEM_FILE="/home/frank/.hermes/shared-memory/COLLECTIVE_MEMORIES.md"
PM="${PM_NAME:-unknown-pm}"
TAG="${1:?usage: TAG title body}"
TITLE="${2:?usage: TAG title body}"
BODY="${3:?usage: TAG title body}"

{
  echo ""
  echo "## ${TITLE}"
  echo "**Tags:** #${TAG}"
  echo "**From:** ${PM} ($(date -u +%Y-%m-%dT%H:%MZ))"
  echo "**Relevant to:** all"
  echo ""
  echo "${BODY}"
} >> "${MEM_FILE}"
echo "logged: ${TITLE} (${TAG}) by ${PM}"
