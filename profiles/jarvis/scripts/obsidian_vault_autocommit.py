#!/usr/bin/env python3
"""Auto-commit Frank's Obsidian vaults for history + rollback.

Designed for Hermes cron no_agent mode: stay silent when nothing changes; print
one line per commit or actionable failure.

When a vault has an `origin` remote, push HEAD after a successful commit, and
also push when there is nothing new to commit but local HEAD is still ahead.
A failing push is always printed (PUSH-FAILED) so it cannot go silent.

Staged content is scanned with the tight second-brain secret patterns before
commit. A hit blocks the commit and does not push.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
from pathlib import Path

# All four vaults are git repos with GitHub remotes; git IS the off-box backup.
# sycode-trading and investments were MISSING from this list until 2026-08-28 —
# `investments` was found carrying 2 unpushed commits + 6 uncommitted files, i.e. it
# had no off-box copy at all once the Mac tar was retired (Frank: "I don't need a
# backup to the mac"). Every vault that git protects must be listed here, or the
# vault-autocommit-liveness watchdog reports green on a vault nothing is committing.
VAULTS = [
    Path('/home/frank/obsidian-fleet-vault'),
    Path('/home/frank/obsidian/quant-team'),
    Path('/home/frank/obsidian/sycode-trading'),
    Path('/home/frank/obsidian/investments'),
]

# A lock older than this cannot belong to a live run (a run commits in seconds),
# so it was left behind by a crash/killed process and must not block commits forever.
LOCK_STALE_SECONDS = 3600

# Tight patterns — same shapes as fleet System/Scripts/audit_second_brain.py
# (2026-08-13). Do not re-introduce hyphenated sk-* prose matches.
TIGHT_SECRET_RE = re.compile(
    r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}\b"
    r"|\bsk-[A-Za-z0-9]{24,}\b"
    r"|\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"
)

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

# Research dumps stay on disk + nightly Mac tarball. Not the git wiki.
*.parquet
*.feather
*.arrow
*.pkl
*.joblib
*.npy
*.npz
*.h5
*.hdf5
*.sqlite
*.db
*.csv.gz
*.jsonl.gz
analytics/clean-epoch-ledger/*.jsonl
research/artifacts/**/*.csv
research/artifacts/**/*.jsonl
research/world-class-trading-loop/artifacts/**/*.csv
research/world-class-trading-loop/artifacts/**/*.jsonl

# Local automation artifacts
.obsidian-git-autocommit.lock
"""


def run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    # errors='replace' is LOAD-BEARING. `git diff` emits raw file bytes, so a single
    # non-UTF-8 byte anywhere in the diff (a .pyc, an image, a parquet) made the
    # default strict decode raise UnicodeDecodeError and abort the whole vault pass
    # before any commit. That is exactly what happened to obsidian/investments: a
    # venv/ was git-added on 2026-08-21, and every autocommit run from then until
    # 2026-08-28 died on its bytecode — the vault silently went 7 days uncommitted
    # and unpushed while the watchdog still reported the job as running.
    # Replacement chars only affect the SCAN text; commits use git's own bytes.
    return subprocess.run(
        cmd, cwd=str(cwd), text=True, capture_output=True, check=check,
        encoding="utf-8", errors="replace",
    )


def ensure_repo(vault: Path) -> None:
    if not (vault / '.git').exists():
        run(['git', 'init', '-b', 'main'], vault)
    run(['git', 'config', 'user.name', 'Hermes Obsidian Autocommit'], vault)
    run(['git', 'config', 'user.email', 'hermes-obsidian-autocommit@example.invalid'], vault)
    gi = vault / '.gitignore'
    if not gi.exists() or gi.read_text() != GITIGNORE:
        gi.write_text(GITIGNORE)


def secret_scan_staged(vault: Path) -> list[str]:
    diff = run(['git', 'diff', '--cached', '-U0', '--no-color'], vault, check=False)
    hits: list[str] = []
    current = '?'
    for line in diff.stdout.splitlines():
        if line.startswith('+++ b/'):
            current = line[6:]
            continue
        if line.startswith('+') and not line.startswith('+++') and TIGHT_SECRET_RE.search(line):
            hits.append(current)
    return sorted(set(hits))


def maybe_push(vault: Path) -> str | None:
    remotes = run(['git', 'remote'], vault, check=False).stdout.split()
    if 'origin' not in remotes:
        return None
    ahead = run(
        ['git', 'rev-list', '--count', '@{upstream}..HEAD'],
        vault,
        check=False,
    )
    # No upstream yet (first push) still needs a push.
    if ahead.returncode == 0 and ahead.stdout.strip() == '0':
        return None
    result = run(['git', 'push', '-u', 'origin', 'HEAD'], vault, check=False)
    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip().replace('\n', ' ')[:300]
        return f'PUSH-FAILED {vault} rc={result.returncode} {err}'
    return f'PUSHED {vault}'


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
        messages: list[str] = []
        if diff.returncode != 0:
            blocked = secret_scan_staged(vault)
            if blocked:
                return f'SECRET-SCAN-BLOCKED {vault} files={blocked[:20]}'
            stamp = _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            run(['git', 'commit', '-m', f'chore(obsidian): auto-commit vault snapshot {stamp}'], vault)
            sha = run(['git', 'rev-parse', '--short', 'HEAD'], vault).stdout.strip()
            messages.append(f'COMMITTED {vault} {sha}')
        push = maybe_push(vault)
        if push:
            messages.append(push)
        return '\n'.join(messages) if messages else None
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
                if msg.startswith('PUSH-FAILED') or msg.startswith('SECRET-SCAN-BLOCKED') or 'PUSH-FAILED' in msg:
                    rc = 1
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
