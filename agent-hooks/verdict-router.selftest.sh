#!/usr/bin/env bash
# Deterministic REVIEW_VERDICT router harness. Uses fixtures + in-memory planning;
# does not read or mutate live kanban boards.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$script_dir/verdict-router-harness.py" --fixtures "$script_dir/verdict-router.fixtures.json" "$@"
python3 "$script_dir/verdict-router-harness.py" --fixtures "$script_dir/verdict-router.fixtures.json" --mutation-planning "$@"
python3 "$script_dir/verdict-router-harness-api.selftest.py"
