#!/usr/bin/env bash
# Tests for scripts/verify-commit-signatures.sh:
#   - exit-code precedence (BAD/UNTRUSTED > UNSIGNED > all-trusted)
#   - emergency bypass (env + flag forms) and its audit logging
#   - usage error when --bypass lacks a reason
# Builds throwaway git repos with real ed25519 ssh signatures in a temp dir.
set -u

HELPER="${1:-$(cd "$(dirname "$0")/.." && pwd)/verify-commit-signatures.sh}"
if [ ! -f "$HELPER" ]; then
  echo "FAIL: helper not found at $HELPER"; exit 1
fi
if ! command -v ssh-keygen >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then
  echo "FAIL: ssh-keygen and git required"; exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0

AL="$TMP/allowed_signers"
KEYDIR="$TMP/keys"; mkdir -p "$KEYDIR"
BYPASS_LOG="$TMP/bypass.log"

check() { # name expected actual
  if [ "$3" = "$2" ]; then PASS=$((PASS+1)); echo "PASS: $1 (exit=$3)"
  else FAIL=$((FAIL+1)); echo "FAIL: $1 expected exit=$2 got=$3"; fi
}

# --- fixtures: ed25519 keys; trusted key enrolled in allowed_signers, rogue not ---
ssh-keygen -q -t ed25519 -N '' -f "$KEYDIR/trusted" >/dev/null 2>&1
ssh-keygen -q -t ed25519 -N '' -f "$KEYDIR/rogue"   >/dev/null 2>&1
echo "test@trusted $(cat "$KEYDIR/trusted.pub")" > "$AL"

mkrepo() { # name email -> repo path (signed commit pre-enabled)
  local d="$TMP/repo-$1"; rm -rf "$d"; mkdir -p "$d"
  git -C "$d" init -q
  git -C "$d" config user.name "Test"
  git -C "$d" config user.email "$2"
  git -C "$d" config gpg.format ssh
  git -C "$d" config commit.gpgsign true
  echo "$d"
}
c_unsigned() { git -C "$1" -c commit.gpgsign=false commit -q --allow-empty -m "$2"; }
c_signed()   { git -C "$1" config user.signingkey "$KEYDIR/$2.pub"; git -C "$1" commit -q --allow-empty -m "$3"; }

run_verifier() { # repo al [bypass_reason] -> exit code
  local d="$1" al="$2" bp="${3:-}" rc
  if [ -n "$bp" ]; then
    ALLOWED_SIGNERS_FILE="$al" VERIFY_SIGNATURES_LOG="$BYPASS_LOG" \
      VERIFY_SIGNATURES_BYPASS="$bp" bash "$HELPER" "$d" >/dev/null 2>&1
  else
    ALLOWED_SIGNERS_FILE="$al" VERIFY_SIGNATURES_LOG="$BYPASS_LOG" bash "$HELPER" "$d" >/dev/null 2>&1
  fi
  rc=$?; echo "$rc"
}

echo "== BAD/UNTRUSTED present -> exit 1 (takes precedence over UNSIGNED) =="
d=$(mkrepo untrusted frank@test)
c_unsigned "$d" "u1"            # UNSIGNED
c_signed   "$d" rogue "r1"      # UNTRUSTED (valid sig, key not in allowed_signers)
rc=$(run_verifier "$d" "$AL")
check "UNTRUSTED + UNSIGNED -> exit 1 (not 2)" 1 "$rc"

echo "== BAD signature present -> exit 1 =="
d=$(mkrepo bad frank@test)
c_signed "$d" trusted "b1"      # valid signed commit, then corrupt its signature
br="$(git -C "$d" symbolic-ref --short HEAD)"
git -C "$d" cat-file commit HEAD > "$TMP/bad.raw"
python3 - "$TMP/bad.raw" <<'PY'
import re,sys
p=sys.argv[1]; raw=open(p).read()
# Corrupt one base64 character inside the SSH signature body (indented lines
# between the BEGIN/END markers) so the signature no longer verifies.
def corrupt(m): return m.group(0).replace('A','B',1)
new=re.sub(r'(?m)^ (?:[A-Za-z0-9+/=]{4})*[A-Za-z0-9+/=]{1,3}$', corrupt, raw, count=1)
assert new!=raw, "could not locate SSH signature base64 body to corrupt"
open(p,'w').write(new)
PY
newobj="$(git -C "$d" hash-object -t commit -w "$TMP/bad.raw")"
git -C "$d" update-ref "refs/heads/$br" "$newobj"
rc=$(run_verifier "$d" "$AL")
check "BAD signature -> exit 1" 1 "$rc"

echo "== UNSIGNED-only -> exit 2 =="
d=$(mkrepo unsigned frank@test)
c_unsigned "$d" "u1"; c_unsigned "$d" "u2"
rc=$(run_verifier "$d" "$AL")
check "UNSIGNED-only -> exit 2" 2 "$rc"

echo "== all trusted (GOOD) -> exit 0 =="
d=$(mkrepo good frank@test)
c_signed "$d" trusted "g1"
rc=$(run_verifier "$d" "$AL")
check "all trusted -> exit 0" 0 "$rc"

echo "== emergency bypass =="
d=$(mkrepo bypass frank@test)
c_unsigned "$d" "u1"; c_signed "$d" rogue "r1"   # would be exit 1 without bypass
: > "$BYPASS_LOG"
rc=$(run_verifier "$d" "$AL" "incident-42")
check "bypass env forces exit 0 despite BAD/UNTRUSTED" 0 "$rc"
if grep -q "incident-42" "$BYPASS_LOG"; then PASS=$((PASS+1)); echo "PASS: bypass logged with reason to $BYPASS_LOG"
else FAIL=$((FAIL+1)); echo "FAIL: bypass reason not logged"; fi

echo "== bypass flag forms =="
rc=$(ALLOWED_SIGNERS_FILE="$AL" VERIFY_SIGNATURES_LOG="$BYPASS_LOG" bash "$HELPER" "$d" --bypass "manual-override" >/dev/null 2>&1; echo $?)
check "--bypass <reason> -> exit 0" 0 "$rc"
rc=$(ALLOWED_SIGNERS_FILE="$AL" bash "$HELPER" "$d" --bypass=inline-reason >/dev/null 2>&1; echo $?)
check "--bypass=<reason> -> exit 0" 0 "$rc"
rc=$(ALLOWED_SIGNERS_FILE="$AL" bash "$HELPER" "$d" --bypass >/dev/null 2>&1; echo $?)
check "--bypass without reason -> exit 3 (usage error)" 3 "$rc"

echo
echo "==== RESULT: PASS=$PASS FAIL=$FAIL ===="
[ "$FAIL" -eq 0 ]
