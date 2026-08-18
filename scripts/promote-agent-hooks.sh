#!/usr/bin/env bash
# promote-agent-hooks.sh — copy named hook files from a reviewed git SHA into
# the LIVE /home/frank/.hermes/agent-hooks tree. Never checks out the shared
# dirty .hermes branch. Created 2026-08-13 after t_7a68db5d (manual copy-promote
# of 6226b7b) had to be done by a terminal seat.
#
# Usage:
#   promote-agent-hooks.sh <sha> [file ...]
#   promote-agent-hooks.sh --check <sha>     # compare live vs sha, exit 1 on drift
# Default files: gate-kanban-complete-classifier.py gate-kanban-complete.fixtures.json
set -euo pipefail
REPO=/home/frank/.hermes
LIVE="$REPO/agent-hooks"
DEFAULT_FILES=(
  gate-kanban-complete-classifier.py
  gate-kanban-complete.fixtures.json
)

CHECK=0
if [[ "${1:-}" == "--check" ]]; then CHECK=1; shift; fi
SHA="${1:-}"
[[ -n "$SHA" ]] || { echo "usage: $0 [--check] <sha> [file ...]" >&2; exit 2; }
shift || true
FILES=("$@")
(( ${#FILES[@]} > 0 )) || FILES=("${DEFAULT_FILES[@]}")

git -C "$REPO" cat-file -e "${SHA}^{commit}" 2>/dev/null || {
  echo "FAIL: $SHA is not a commit in $REPO" >&2
  exit 2
}

drift=0
for f in "${FILES[@]}"; do
  src_ok=$(git -C "$REPO" cat-file -e "$SHA:agent-hooks/$f" && echo 1 || echo 0)
  [[ "$src_ok" == 1 ]] || { echo "FAIL: $SHA has no agent-hooks/$f" >&2; exit 2; }
  want=$(git -C "$REPO" show "$SHA:agent-hooks/$f" | sha256sum | awk '{print $1}')
  have="MISSING"
  [[ -f "$LIVE/$f" ]] && have=$(sha256sum "$LIVE/$f" | awk '{print $1}')
  if [[ "$have" == "$want" ]]; then
    echo "OK   $f  $want"
    continue
  fi
  echo "DRIFT $f  live=$have  sha=$want"
  drift=1
  if [[ "$CHECK" -eq 1 ]]; then
    continue
  fi
  bak="/tmp/hook-promote/bak-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$bak"
  [[ -f "$LIVE/$f" ]] && cp -a "$LIVE/$f" "$bak/"
  git -C "$REPO" show "$SHA:agent-hooks/$f" > "$LIVE/$f"
  echo "PROMOTED $f from $SHA  backup=$bak"
done

if [[ "$CHECK" -eq 1 && "$drift" -eq 1 ]]; then
  exit 1
fi
exit 0
