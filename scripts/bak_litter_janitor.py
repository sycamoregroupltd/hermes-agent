#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Move stale Hermes .bak litter out of live cron/script directories.

Invoker: Jarvis profile no-agent cron `script-bak-litter-janitor` (weekly, deliver=discord:#fleet-reports).
Task: jarvis-os/t_38f2df03.
Policy: only move backup-looking files older than RETENTION_DAYS from cron stores and script dirs into /home/frank/.hermes/scripts/backups/<yyyymm>/, preserving relative paths.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

HERMES = Path('/home/frank/.hermes')
DEST_ROOT = HERMES / 'scripts' / 'backups'
RETENTION_DAYS = 7
TASK_ID = 't_38f2df03'
NOW = datetime.now(timezone.utc)
CUTOFF = NOW.timestamp() - RETENTION_DAYS * 86400
SKIP_PARTS = {'archive', 'backups', 'logs', '__pycache__', 'state-snapshots'}


def is_backup_name(name: str) -> bool:
    return '.bak-' in name or '.bak_' in name or name.endswith('.bak') or '.bak.' in name


def candidate_roots() -> list[Path]:
    roots = [HERMES / 'scripts']
    profiles = HERMES / 'profiles'
    if profiles.exists():
        seen_real = set()
        for profile in sorted(p for p in profiles.iterdir() if p.is_dir()):
            # Dedupe symlinked profile stores (e.g. sycode-trading -> sycode-trading-pm)
            # so one physical cron/scripts tree is walked exactly once.
            real = profile.resolve()
            if str(real) in seen_real:
                continue
            seen_real.add(str(real))
            for sub in ('cron', 'scripts'):
                p = profile / sub
                if p.exists():
                    roots.append(p)
    return roots


def under_skipped_dir(path: Path) -> bool:
    rel = path.relative_to(HERMES)
    return any(part in SKIP_PARTS for part in rel.parts)


def unique_dest(src: Path) -> Path:
    ym = datetime.fromtimestamp(src.stat().st_mtime, timezone.utc).strftime('%Y%m')
    rel = src.relative_to(HERMES)
    dest = DEST_ROOT / ym / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        return dest
    stem = dest.name
    for i in range(1, 1000):
        alt = dest.with_name(f'{stem}.dup{i}')
        if not alt.exists():
            return alt
    raise RuntimeError(f'Could not allocate unique destination for {src}')


def main() -> int:
    moved: list[dict] = []
    errors: list[str] = []
    for root in candidate_roots():
        for dirpath, dirnames, filenames in os.walk(root):
            # Do not sweep our own archive/backup/log/state trees.
            dirnames[:] = [d for d in dirnames if d not in SKIP_PARTS]
            for fn in filenames:
                src = Path(dirpath) / fn
                try:
                    if under_skipped_dir(src):
                        continue
                    if not is_backup_name(src.name):
                        continue
                    st = src.stat()
                    if st.st_mtime >= CUTOFF:
                        continue
                    dest = unique_dest(src)
                    shutil.move(str(src), str(dest))
                    moved.append({
                        'source': str(src),
                        'dest': str(dest),
                        'mtime_utc': datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                        'bytes': st.st_size,
                    })
                except Exception as exc:  # fail visibly; no-agent delivery should alert on stdout/stderr.
                    errors.append(f'{src}: {exc}')
    state_dir = HERMES / 'scripts' / 'state'
    state_dir.mkdir(parents=True, exist_ok=True)
    state = {
        'task_id': TASK_ID,
        'ran_at_utc': NOW.isoformat(),
        'retention_days': RETENTION_DAYS,
        'moved_count': len(moved),
        'error_count': len(errors),
        'moved': moved,
        'errors': errors,
    }
    (state_dir / 'bak_litter_janitor_last.json').write_text(json.dumps(state, indent=2, sort_keys=True), encoding='utf-8')
    if moved or errors:
        print(f'SCRIPT_BAK_LITTER_JANITOR task={TASK_ID} moved={len(moved)} errors={len(errors)} retention_days={RETENTION_DAYS}')
        for item in moved[:50]:
            print(f"moved {item['source']} -> {item['dest']}")
        if len(moved) > 50:
            print(f'... {len(moved) - 50} additional moves omitted; see {state_dir / "bak_litter_janitor_last.json"}')
        for err in errors:
            print(f'ERROR {err}')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
