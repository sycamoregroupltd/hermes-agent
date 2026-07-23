#!/usr/bin/env python3
"""Auto-commit Frank's Obsidian vaults for history + rollback.

Designed for Hermes cron no_agent mode: stay silent when nothing changes; print
one line per commit or actionable failure.
"""
from __future__ import annotations

import datetime as _dt
import os
import subprocess
from pathlib import Path

VAULTS = [
    Path('/home/frank/obsidian-fleet-vault'),
    Path('/home/frank/obsidian/quant-team'),
]

# A lock older than this cannot belong to a live run (a run commits in seconds),
# so it was left behind by a crash/killed process and must not block commits forever.
LOCK_STALE_SECONDS = 3600

GITIGNORE = """# Obsidian local workspace/cache state
.obsidian/workspace.json
.obsidian/workspace-mobile.json
.obsidian/cache/
.obsidian/.trash/
.trash/

# OS/editor cruft
.DS_Store
Thumbs.db
*.swp
*.swo
*~

# Logs and transient exports
*.log
*.tmp
*.temp

# Large/generated binary archives and media (keep notes/history lightweight)
*.zip
*.tar
*.tar.gz
*.tgz
*.7z
*.rar
*.mp4
*.mov
*.avi
*.mkv
*.wav
*.mp3
*.flac
*.aiff
*.iso
*.dmg

# Local automation artifacts
.obsidian-git-autocommit.lock
"""


def run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True, check=check)


def ensure_repo(vault: Path) -> None:
    if not (vault / '.git').exists():
        run(['git', 'init', '-b', 'main'], vault)
    run(['git', 'config', 'user.name', 'Hermes Obsidian Autocommit'], vault)
    run(['git', 'config', 'user.email', 'hermes-obsidian-autocommit@example.invalid'], vault)
    gi = vault / '.gitignore'
    if not gi.exists() or gi.read_text() != GITIGNORE:
        gi.write_text(GITIGNORE)


def autocommit(vault: Path) -> str | None:
    if not vault.exists():
        return f'MISSING {vault}'
    lock = vault / '.obsidian-git-autocommit.lock'
    # Recover a stale lock: a lock older than LOCK_STALE_SECONDS cannot belong to
    # a live run, so it was left by a crash/kill and must not block future commits.
    if lock.exists():
        try:
            age = _dt.datetime.now().timestamp() - lock.stat().st_mtime
        except OSError:
            age = 0.0
        if age > LOCK_STALE_SECONDS:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        return f'SKIP locked {vault}'
    try:
        ensure_repo(vault)
        run(['git', 'add', '-A'], vault)
        diff = run(['git', 'diff', '--cached', '--quiet'], vault, check=False)
        if diff.returncode == 0:
            return None
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        run(['git', 'commit', '-m', f'chore(obsidian): auto-commit vault snapshot {stamp}'], vault)
        sha = run(['git', 'rev-parse', '--short', 'HEAD'], vault).stdout.strip()
        return f'COMMITTED {vault} {sha}'
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    messages: list[str] = []
    rc = 0
    for vault in VAULTS:
        try:
            msg = autocommit(vault)
            if msg:
                messages.append(msg)
        except subprocess.CalledProcessError as exc:
            rc = 1
            messages.append(
                f'ERROR {vault}: {exc.cmd} rc={exc.returncode} stdout={exc.stdout.strip()} stderr={exc.stderr.strip()}'
            )
        except Exception as exc:  # noqa: BLE001 - cron watchdog should fail visibly.
            rc = 1
            messages.append(f'ERROR {vault}: {exc!r}')
    if messages:
        print('\n'.join(messages))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
