#!/usr/bin/env bash
# gpt-oss-120b MXFP4 download — IPv4-forced, resumable, FAIL-CLOSED.
# Why this shape: the July attempt died at 60% from hf-cli parallel segments
# over the DGX's lossy IPv6 egress (~33% drop) + anonymous rate limits.
# Single-stream curl -4 with resume avoids both.
# HARD LESSON 2026-08-02: the first version of this script declared
# "DONE ... 15 bytes" on an HTTP 404 — `curl -sS -L -C -` writes the error
# body and exits 0, so a fabricated success looked like a completed download
# (the filename was guessed as 3 shards; the repo actually ships ONE file:
# gpt-oss-120b-MXFP4.gguf). Every step below now verifies HTTP status, byte
# size, and GGUF magic before claiming success.
# Consumer: llama-server local fallback rung (card on jarvis-os).
set -u
export PATH="/home/frank/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
D=/home/frank/models/gpt-oss-120b-GGUF
F=gpt-oss-120b-MXFP4.gguf
URL="https://huggingface.co/ggml-org/gpt-oss-120b-GGUF/resolve/main/$F"
MIN_BYTES=$((50 * 1024 * 1024 * 1024))   # sanity floor ~50GB (expect ~59-65GB)
mkdir -p "$D"; cd "$D" || exit 1

head=$(curl -4 -sIL --max-time 60 "$URL" 2>&1)
status=$(printf '%s' "$head" | grep -aiE '^HTTP' | tail -1)
len=$(printf '%s' "$head" | grep -aiE '^(content-length|x-linked-size):' | tail -1 | tr -dc '0-9')
case "$status" in
  *200*) : ;;
  *) echo "PREFLIGHT FAILED: $status for $URL"; exit 1 ;;
esac
if [ -z "$len" ] || [ "$len" -lt "$MIN_BYTES" ]; then
  echo "PREFLIGHT FAILED: advertised size '${len:-none}' below floor $MIN_BYTES — wrong file?"; exit 1
fi
echo "PREFLIGHT OK: $F advertises $len bytes"

tries=0
while :; do
  curl -4 -sS -L -C - --retry 8 --retry-delay 30 -o "$F" "$URL"
  have=$(stat -c %s "$F" 2>/dev/null || echo 0)
  [ "$have" -ge "$len" ] && break
  tries=$((tries+1))
  if [ "$tries" -ge 12 ]; then
    echo "FAILED: $F stalled at $have/$len bytes after $tries resume rounds"; exit 1
  fi
  echo "resume round $tries: $have/$len bytes"; sleep 60
done

magic=$(head -c 4 "$F" | tr -d '\0')
if [ "$magic" != "GGUF" ]; then
  echo "FAILED: $F has $have bytes but magic='$magic' (expected GGUF) — corrupt/error body"; exit 1
fi
echo "VERIFIED: $F $have bytes, GGUF magic ok"
echo "NEXT: llama-server -m $D/$F --port 8080 -ngl 999 -c 32768"
