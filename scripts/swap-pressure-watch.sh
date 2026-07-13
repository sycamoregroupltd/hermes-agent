#!/usr/bin/env bash
set -euo pipefail

threshold_pct="${SWAP_PRESSURE_THRESHOLD_PCT:-80}"

used_pct=$(free | awk '/^Swap:/ { if ($2 == 0) print 0; else printf "%d", ($3*100)/$2 }')
if [ -z "${used_pct:-}" ]; then
  used_pct=0
fi

if [ "$used_pct" -lt "$threshold_pct" ]; then
  exit 0
fi

printf 'SWAP_PRESSURE used_pct=%s threshold_pct=%s\n' "$used_pct" "$threshold_pct"
python3 - <<'PY'
import glob
import os

rows = []
for proc_path in glob.glob('/proc/[0-9]*'):
    pid = os.path.basename(proc_path)
    try:
        with open(f'{proc_path}/status', 'r', errors='ignore') as handle:
            status = handle.read().splitlines()
        swap = int(next((line.split()[1] for line in status if line.startswith('VmSwap:')), '0'))
        if not swap:
            continue
        name = next((line.split(':', 1)[1].strip() for line in status if line.startswith('Name:')), '?')
        ppid = next((line.split(':', 1)[1].strip() for line in status if line.startswith('PPid:')), '?')
        rows.append((swap, int(pid), ppid, name))
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, OSError):
        continue

print(f'total_process_swap_kib={sum(row[0] for row in rows)} processes_with_swap={len(rows)}')
for swap, pid, ppid, name in sorted(rows, reverse=True)[:20]:
    print(f'{swap:>10} KiB pid={pid} ppid={ppid} name={name}')
PY
