#!/usr/bin/env bash
# Parallel ranged fetch of gpt-oss-120b parts 2+3 (IPv4, HTTP/1.1, 6 segments each)
# Each segment loops with append-resume until its exact byte count is present.
set -u
cd /home/frank/models/gpt-oss-120b-GGUF
BASE="https://huggingface.co/ggml-org/gpt-oss-120b-GGUF/resolve/main"

seg_fetch() { # name start end segfile
  local name="$1" start="$2" end="$3" seg="$4"
  local want=$(( end - start + 1 ))
  local tries=0
  while :; do
    local have=0
    [ -f "$seg" ] && have=$(stat -c %s "$seg")
    [ "$have" -ge "$want" ] && return 0
    tries=$(( tries + 1 ))
    [ "$tries" -gt 60 ] && { echo "GIVEUP $seg at $have/$want"; return 1; }
    curl -4 --http1.1 -sL -m 900 --speed-limit 20000 --speed-time 30 \
      -r $(( start + have ))-"$end" -o - "$BASE/$name" >> "$seg"
    sleep 2
  done
}

fetch_part() {
  local name="$1" size="$2" segs=6
  local per=$(( size / segs ))
  local rc=0
  local pids=()
  for i in $(seq 0 $((segs-1))); do
    local start=$(( i * per ))
    local end=$(( (i == segs-1) ? size-1 : start + per - 1 ))
    seg_fetch "$name" "$start" "$end" "$name.seg$i" &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p" || rc=1; done
  [ "$rc" -ne 0 ] && { echo "$name INCOMPLETE"; return 1; }
  cat $(for i in $(seq 0 $((segs-1))); do echo "$name.seg$i"; done) > "$name"
  rm -f "$name".seg*
  echo "$name assembled"
}

fetch_part gpt-oss-120b-mxfp4-00002-of-00003.gguf 31738487200 || exit 1
fetch_part gpt-oss-120b-mxfp4-00003-of-00003.gguf 31635878880 || exit 1
sha256sum -c <(cat <<'EOF'
346492f65891fb27cac5c74a8c07626cbfeb4211cd391ec4de37dbbe3109a93b  gpt-oss-120b-mxfp4-00002-of-00003.gguf
66dca81040933f5a49177e82c479c51319cefb83bd22dad9f06dad45e25f1463  gpt-oss-120b-mxfp4-00003-of-00003.gguf
EOF
)
