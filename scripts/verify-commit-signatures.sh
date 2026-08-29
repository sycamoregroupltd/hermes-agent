#!/usr/bin/env bash
# verify-commit-signatures.sh — commit-signature audit for the Hermes fleet.
#
# Usage:
#   verify-commit-signatures.sh <repo-path> [rev-range] [--bypass <reason>]
#     repo-path   path to a git repo or worktree
#     rev-range   any git rev-range (e.g. main..HEAD, HEAD~10..HEAD, abc123..HEAD)
#                 default: last 20 commits reachable from HEAD
#     --bypass <reason>   (or --bypass=<reason>) emergency override — forces exit 0
#                 and logs the bypass. NOT the default. Requires a reason.
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
# EXIT STATUS (report is always printed first, then this decides the code):
#   0  verification passed — no BAD, UNTRUSTED, or UNSIGNED commits
#      (GOOD and OTHER(x) commits do not fail verification)
#   1  one or more BAD or UNTRUSTED commits (reported first — takes precedence
#      over UNSIGNED-only)
#   2  no BAD/UNTRUSTED commits, but one or more UNSIGNED commits
#   3  usage error (e.g. --bypass without a reason)
#
# EMERGENCY BYPASS:
#   Pass --bypass <reason> (or set VERIFY_SIGNATURES_BYPASS=<reason>) to force
#   exit 0 regardless of findings. The bypass reason is logged to stderr and
#   appended to VERIFY_SIGNATURES_LOG (default:
#   /home/frank/.hermes/var/log/verify-signatures-bypass.log). Bypass is never
#   the default and always requires a reason, so it is auditable.
#
# NOTE ON BEHAVIOUR: this script REPORTS by default (it never writes git config
# and is invoked with -c per call). The exit codes above are the enforcement
# signal. Callers, CI jobs, hooks, and deployment config that invoke this
# helper are deliberately NOT changed here — wiring any of them to act on the
# nonzero exit is a separate policy task (see signing-keys-enforcement-gate).

set -u

ALLOWED_SIGNERS="${ALLOWED_SIGNERS_FILE:-/home/frank/.hermes/governance/allowed_signers}"
BYPASS_LOG="${VERIFY_SIGNATURES_LOG:-/home/frank/.hermes/var/log/verify-signatures-bypass.log}"

# --- argument parsing: extract optional --bypass, keep positional repo/range ---
BYPASS_REASON=""
if [ -n "${VERIFY_SIGNATURES_BYPASS:-}" ]; then
  BYPASS_REASON="$VERIFY_SIGNATURES_BYPASS"
fi
POSITIONAL=()
i=1
while [ $i -le $# ]; do
  arg="${!i}"
  case "$arg" in
    --bypass=*)
      BYPASS_REASON="${arg#--bypass=}" ;;
    --bypass)
      i=$((i+1))
      if [ $i -gt $# ]; then
        echo "ERROR: --bypass requires a reason (e.g. --bypass 'incident 1234')" >&2
        exit 3
      fi
      BYPASS_REASON="${!i}" ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0 ;;
    *)
      POSITIONAL+=("$arg") ;;
  esac
  i=$((i+1))
done

if [ "${#POSITIONAL[@]}" -lt 1 ]; then
  sed -n '2,20p' "$0"
  exit 0
fi

REPO="${POSITIONAL[0]}"
RANGE="${POSITIONAL[1]:-}"

if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  echo "NOT A GIT REPO: $REPO (no commits scanned: exit 0)"
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
    echo "WARNING: rev-range '$RANGE' did not resolve in $REPO — no commits scanned (exit 0)"
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

# --- emergency bypass: logged, never the default, forces exit 0 ---
if [ -n "$BYPASS_REASON" ]; then
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)
  msg="verify-commit-signatures BYPASS reason='$BYPASS_REASON' repo='$REPO' range='${RANGE:-default}' findings(total=$total good=$good untrusted=$untrusted bad=$bad unsigned=$unsigned other=$other) at $ts"
  echo "BYPASS: $msg" >&2
  if [ -n "$BYPASS_LOG" ]; then
    if printf '%s\n' "$msg" >> "$BYPASS_LOG" 2>/dev/null; then
      echo "BYPASS: logged to $BYPASS_LOG" >&2
    else
      echo "BYPASS WARNING: could not append to $BYPASS_LOG (stderr above is the audit record)" >&2
    fi
  fi
  echo "(bypass active: exit 0 regardless of findings)"
  exit 0
fi

# --- exit code: precedence BAD/UNTRUSTED > UNSIGNED > all-trusted ---
if [ "$bad" -gt 0 ] || [ "$untrusted" -gt 0 ]; then
  echo "VERIFY FAILED: $((bad+untrusted)) BAD/UNTRUSTED commit(s) — exit 1"
  exit 1
fi
if [ "$unsigned" -gt 0 ]; then
  echo "VERIFY FAILED: $unsigned UNSIGNED commit(s) — exit 2"
  exit 2
fi
echo "VERIFY OK: all commits trusted — exit 0"
exit 0
