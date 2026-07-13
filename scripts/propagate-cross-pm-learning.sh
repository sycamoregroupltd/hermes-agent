#!/usr/bin/env bash
# Nightly propagation: skills from global tree & memory digest to profiles
# Run via cron so the fleet stays in sync without manual intervention.

set -eo pipefail

SHARED_MEMORY="/home/frank/.hermes/shared-memory"
LOG_DIR="/home/frank/.hermes/cron/output"
mkdir -p "${LOG_DIR}"

LOG="${LOG_DIR}/propagate-$(date +%Y%m%d-%H%M%S).log"
exec >> "${LOG}" 2>&1

echo "=== $(date) — propagate-cross-pm-learning started ==="

# 1. Skill propagation
echo "--- skills ---"
PROPAGATE_SKILLS="/home/frank/.hermes/shared-memory/propagate-skills.sh"
if [[ -x "${PROPAGATE_SKILLS}" ]]; then
    bash "${PROPAGATE_SKILLS}" --apply
else
    # Original lived in deleted kanban workspace t_0ec9d0db; recreation is a tracked follow-up (2026-06-10).
    echo "  WARNING: propagate-skills.sh missing — skill propagation SKIPPED"
fi

echo ""
echo "--- collective-memories digest ---"
MEM_FILE="${SHARED_MEMORY}/COLLECTIVE_MEMORIES.md"
if [[ -f "${MEM_FILE}" ]]; then
    entries=$(grep -c "^## " "${MEM_FILE}" || true)
    echo "  ${entries} entries in COLLECTIVE_MEMORIES.md"
    # TODO: parse and inject into per-PM context when auto-inject is safe
else
    echo "  MEM_FILE missing — skipping digest"
fi

echo ""
echo "=== $(date) — done ==="
# Keep last 10 logs
ls -1t "${LOG_DIR}"/propagate-*.log 2>/dev/null | tail -n +11 | xargs -r rm -f
