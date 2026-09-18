#!/bin/bash
# Resumable snapshot puller — parameterized. For lossy DGX<->Mac link.
# Usage: dgx-pull-snapshot.sh <stamp> <dgx_ts_ip>
# Handles: curl exit codes, HTTP-33 (no byte ranges), file-missing, checksum retry, locale noise.
set -u
SNAP="${1:?Usage: dgx-pull-snapshot.sh <stamp> <dgx_ts_ip>}"
SRC="http://${2:?Usage: dgx-pull-snapshot.sh <stamp> <dgx_ts_ip>}:18888"
DEST_DIR="$HOME/dgx-fleet-backups/$SNAP"
mkdir -p "$DEST_DIR"
cd "$DEST_DIR" || exit 1

# Silence locale noise from mac perl/curl on DGX-sourced env
export LC_ALL=C LANG=C

echo "DGX snapshot puller — $(date)"
echo "Source: $SRC  Dest: $DEST_DIR"
echo "---------------------------------------------------------------"

ok=0; fail=0

pull () {
  local f="$1" sha="$2"
  local url="$SRC/$f"
  local attempt=0 max_attempts=100
  local use_resume=1   # start by trying resume; turn off if server lacks ranges
  echo ""
  echo ">>> $f  (expect sha256 $sha)"
  while :; do
    attempt=$((attempt+1))
    if [ $attempt -ge $max_attempts ]; then
      echo "  GIVE UP $f after $attempt attempts"
      fail=$((fail+1)); return 1
    fi
    echo "  attempt $attempt: curl (resume=$use_resume) ..."
    local resume_flag=""
    [ $use_resume -eq 1 ] && resume_flag="-C -"
    curl -fsSL $resume_flag \
         --connect-timeout 15 \
         --max-time 600 \
         --retry 3 --retry-delay 2 --retry-all-errors \
         --speed-time 30 --speed-limit 100 \
         -o "$f" "$url"
    rc=$?

    if [ $rc -eq 33 ]; then
      # HTTP 416 / "byte ranges not supported" — server lacks -C -; drop partial, restart from scratch
      echo "  server lacks byte ranges, discarding partial and re-downloading from scratch..."
      rm -f "$f"
      use_resume=0
      sleep 2
      continue
    fi
    if [ $rc -ne 0 ]; then
      echo "  curl exit=$rc, retrying in 5s..."
      sleep 5
      continue
    fi
    if [ ! -s "$f" ]; then
      echo "  file missing/empty after successful curl, retrying in 5s..."
      sleep 5
      continue
    fi
    local got
    got=$(shasum -a 256 "$f" | awk '{print $1}')
    if [ "$got" = "$sha" ]; then
      echo "  OK $f ($(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null) bytes)"
      ok=$((ok+1)); return 0
    else
      echo "  checksum mismatch (got $got), size $(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null)), re-resuming..."
      rm -f "$f"
      use_resume=0
      sleep 3
    fi
  done
}

# Read expected checksums from the server's SHA256SUMS file
SUMS_URL="$SRC/SHA256SUMS"
SUMS_FILE="$DEST_DIR/SHA256SUMS.expected"
if curl -fsSL --connect-timeout 10 --max-time 30 -o "$SUMS_FILE" "$SUMS_URL" 2>/dev/null; then
  echo "Fetched SHA256SUMS from server"
else
  echo "WARNING: could not fetch SHA256SUMS from server" >&2
  echo "ERROR: refusing to pull without authoritative checksums — remove this fallback rather than retry against stale hashes" >&2
  exit 1
fi

# Pull each file listed in the checksums
while IFS= read -r line; do
  [ -z "$line" ] && continue
  sha=$(echo "$line" | awk '{print $1}')
  f=$(echo "$line" | awk '{print $2}')
  [ -z "$f" ] || [ -z "$sha" ] && continue
  pull "$f" "$sha" || true
done < "$SUMS_FILE"

# Also pull any additional files the server lists that aren't in SHA256SUMS
echo ""
echo "================================================"
echo "Summary: $ok OK, $fail FAILED"
[ $fail -eq 0 ] && echo "ALL FILES VERIFIED" || echo "SOME FILES FAILED"
exit $fail
