#!/usr/bin/env bash
# verify-commit-signatures.sh — REPORT-ONLY commit-signature audit for the Hermes fleet.
#
# Usage:
#   verify-commit-signatures.sh <repo-path> [rev-range]
#     repo-path   path to a git repo or worktree
#     rev-range   any git rev-range (e.g. main..HEAD, HEAD~10..HEAD, abc123..HEAD)
#                 default: last 20 commits reachable from HEAD
#
# Output: one line per commit:
#   <short-hash>  <STATUS>     <principal-or-->  <author-email>  <subject>
# STATUS meanings:
#   GOOD       signed, key listed in allowed_signers (principal shown)
#   UNTRUSTED  signature cryptographically valid but key NOT in allowed_signers
#   BAD        signature present but invalid
#   UNSIGNED   no signature
#   OTHER(x)   anything else (expired/revoked/unverifiable); x = raw git %G? code
#
# THIS SCRIPT ALWAYS EXITS 0 — it is report mode only. Enforcement (blocking a
# merge/deploy on BAD/UNSIGNED) is explicitly OUT OF SCOPE; flipping to blocking
# is a separate policy decision (see kanban card signing-keys-enforcement-gate).
#
# Verification config is passed per-invocation (-c); this script never writes
# any git config, global or local.

set -u

ALLOWED_SIGNERS="${ALLOWED_SIGNERS_FILE:-/home/frank/.hermes/governance/allowed_signers}"

if [ $# -lt 1 ]; then
  sed -n '2,20p' "$0"
  exit 0
fi

REPO="$1"
RANGE="${2:-}"

if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  echo "NOT A GIT REPO: $REPO (report mode: exiting 0)"
  exit 0
fi

if [ ! -r "$ALLOWED_SIGNERS" ]; then
  echo "WARNING: allowed_signers not readable at $ALLOWED_SIGNERS — all signed commits will show UNTRUSTED"
fi

# Build fingerprint -> principal map from allowed_signers.
# Each line: <principal> [namespaces="..."] <keytype> <base64key>
declare -A PRINCIPAL_BY_FP
if [ -r "$ALLOWED_SIGNERS" ]; then
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    principal=$(printf '%s\n' "$line" | awk '{print $1}')
    keymat=$(printf '%s\n' "$line" | awk '{for(i=2;i<=NF;i++) if($i ~ /^(ssh-|ecdsa-|sk-)/){print $i" "$(i+1); exit}}')
    [ -z "$keymat" ] && continue
    fp=$(printf '%s\n' "$keymat" | ssh-keygen -lf /dev/stdin 2>/dev/null | awk '{print $2}')
    [ -n "$fp" ] && PRINCIPAL_BY_FP["$fp"]="$principal"
  done < "$ALLOWED_SIGNERS"
fi

if [ -n "$RANGE" ]; then
  # Fail loudly (but still rc=0) on an unresolvable range instead of silently
  # reporting total=0 — a verifier that reports clean on bad input is a bug.
  if ! git -C "$REPO" rev-list --quiet "$RANGE" -- 2>/dev/null; then
    echo "WARNING: rev-range '$RANGE' did not resolve in $REPO — no commits scanned (report mode: exiting 0)"
    exit 0
  fi
  LOG_ARGS=("$RANGE")
else
  LOG_ARGS=(-20 HEAD)
fi

good=0; untrusted=0; bad=0; unsigned=0; other=0; total=0

# NOTE: delimiter is \x1f (unit separator), NOT tab — tab is IFS-whitespace so
# empty fields (%GK on unsigned commits) would collapse and shift columns.
# NOTE: tformat (not format) — format omits the final newline and `read` would
# silently drop the last commit in the range.
while IFS=$'\x1f' read -r hash code key email subject; do
  total=$((total+1))
  principal="-"
  case "$code" in
    G)
      status="GOOD     "
      principal="${PRINCIPAL_BY_FP[$key]:-listed-key}"
      good=$((good+1)) ;;
    U)
      status="UNTRUSTED"
      untrusted=$((untrusted+1)) ;;
    B)
      status="BAD      "
      bad=$((bad+1)) ;;
    N)
      status="UNSIGNED "
      unsigned=$((unsigned+1)) ;;
    *)
      status="OTHER($code)"
      other=$((other+1)) ;;
  esac
  printf '%s  %s  %-22s  %-28s  %s\n' "$hash" "$status" "$principal" "$email" "$subject"
done < <(git -C "$REPO" -c gpg.ssh.allowedSignersFile="$ALLOWED_SIGNERS" \
           log --pretty=tformat:'%h%x1f%G?%x1f%GK%x1f%ae%x1f%s' "${LOG_ARGS[@]}" 2>/dev/null)

echo "----"
echo "total=$total good=$good untrusted=$untrusted bad=$bad unsigned=$unsigned other=$other"
echo "(report mode: exit 0 regardless of findings; enforcement is a separate future card)"
exit 0
