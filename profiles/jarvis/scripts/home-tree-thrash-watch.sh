#!/usr/bin/env bash
# home-tree-thrash-watch.sh — fleet-governor / cron helper.
# When swap used >= 14Gi HOT, SIGTERM (then SIGKILL) unbounded du|find over
# /home/frank (Pulse class 2026-09-11). Prefer cached census; no hermes update.
#
# Env:
#   HOME_TREE_THRASH_SWAP_GIB   threshold GiB (default 14)
#   DRY_RUN=1                   report only
#   HOME_TREE_THRASH_MIN_AGE_S  only kill procs older than N seconds (default 20)
# Usage: home-tree-thrash-watch.sh [--dry-run]
set -euo pipefail

DRY_RUN="${DRY_RUN:-0}"
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
  esac
done

THR_GIB="${HOME_TREE_THRASH_SWAP_GIB:-14}"
MIN_AGE="${HOME_TREE_THRASH_MIN_AGE_S:-20}"
LOG="${HOME_TREE_THRASH_LOG:-/home/frank/.hermes/cron/state/home-tree-thrash-watch.log}"
CENSUS="/home/frank/obsidian-fleet-vault/Operations/2026-09-08-t_16b4cb51-disk-census.md"
mkdir -p "$(dirname "$LOG")"

swap_used_kib=0
swap_total_kib=0
while read -r key val unit; do
  case "$key" in
    SwapTotal:) swap_total_kib=$val ;;
    SwapFree:) swap_free_kib=$val ;;
  esac
done < /proc/meminfo
swap_free_kib=${swap_free_kib:-0}
swap_used_kib=$((swap_total_kib - swap_free_kib))
if [ "$swap_used_kib" -lt 0 ]; then swap_used_kib=0; fi
used_gib=$(awk -v u="$swap_used_kib" 'BEGIN { printf "%.2f", u/1024/1024 }')
thr_kib=$(awk -v g="$THR_GIB" 'BEGIN { printf "%d", g*1024*1024 }')

if [ "$swap_used_kib" -lt "$thr_kib" ]; then
  exit 0
fi

printf 'HOME_TREE_THRASH_WATCH swap_used_gib=%s threshold_gib=%s dry_run=%s prefer_census=%s\n' \
  "$used_gib" "$THR_GIB" "$DRY_RUN" "$CENSUS"

# Scan processes: cmdline contains du|find and /home/frank (or bare home glob thrash)
now=$(date +%s)
killed=0
while IFS= read -r -d '' proc; do
  pid="${proc##*/}"
  [[ "$pid" =~ ^[0-9]+$ ]] || continue
  cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
  [ -n "$cmdline" ] || continue
  # Must look like du or find
  echo "$cmdline" | grep -qiE '(^|/)(du|find)([[:space:]]|$)' || continue
  # Home-tree target
  echo "$cmdline" | grep -qiE '/home/frank|/home/frankspencer|\$HOME|~frank' || continue
  # Skip bounded
  if echo "$cmdline" | grep -qiE -- '--max-depth[= ]+[0-3]([[:space:]]|$)|-maxdepth[[:space:]]+[0-3]([[:space:]]|$)'; then
    continue
  fi
  # Age check via starttime (approx from /proc/pid/stat field 22)
  etimes=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ' || echo 0)
  etimes=${etimes:-0}
  if [ "$etimes" -lt "$MIN_AGE" ]; then
    continue
  fi
  # Never touch PID 1 or this script's shell
  if [ "$pid" = "1" ] || [ "$pid" = "$$" ]; then
    continue
  fi
  msg="$(date -u +%FT%TZ) swap=${used_gib}Gi pid=$pid etimes=${etimes}s cmd=$(echo "$cmdline" | cut -c1-180)"
  echo "$msg" | tee -a "$LOG"
  if [ "$DRY_RUN" = "1" ]; then
    continue
  fi
  kill -TERM "$pid" 2>/dev/null || true
  sleep 2
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
    echo "$(date -u +%FT%TZ) SIGKILL pid=$pid" >> "$LOG"
  fi
  killed=$((killed + 1))
done < <(find /proc -maxdepth 1 -type d -name '[0-9]*' -print0 2>/dev/null)

printf 'HOME_TREE_THRASH_WATCH done killed=%s\n' "$killed"
exit 0
