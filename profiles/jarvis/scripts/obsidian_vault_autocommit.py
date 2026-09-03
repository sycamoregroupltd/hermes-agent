#!/usr/bin/env python3
"""Auto-commit Frank's Obsidian vaults for history + rollback.

Designed for Hermes cron no_agent mode: stay silent when nothing changes; print
one line per commit or actionable failure.

When a vault has an `origin` remote, push HEAD after a successful commit, and
also push when there is nothing new to commit but local HEAD is still ahead.
A failing push is always printed so it cannot go silent, and the two kinds are
scored differently (t_61312ab9 review F1): PUSH-FAILED (network/auth/hook — may
self-heal next tick) is advisory and leaves rc=0; PUSH-DIVERGED (remote history
moved under us: non-fast-forward / fetch first) never self-heals without a human,
so it is a FAILURE_MARKER and exits 1 — the exit code is the only liveness signal
a Deliver:local no_agent cron has. No force push, ever.

Staged content is scanned with the tight second-brain secret patterns before
commit. A hit blocks the commit and does not push. A false positive is cleared by
adding a regex line to `<vault>/.gitleaks-allowlist` — the same file, and the same
one-regex-per-line format, the fleet pre-commit hook uses.

A failure in one vault never stops the others, and every failure raises a board
card as well as printing. See main()'s FAILURE POLICY before changing exit codes.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
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

# The escape hatch the fleet pre-commit hook (t_b8125973) advertises in its own
# error text: one regex per line, `#` comments and blanks ignored. Until
# 2026-08-29 THIS scanner never read it, so a false positive here could only be
# cleared by editing the live script — the message pointed at a lever that did
# nothing for the autocommit path.
ALLOWLIST_NAME = '.gitleaks-allowlist'

# Trees whose markdown is vendor documentation, not vault notes (18 site-packages
# LICENSE.md files in investments alone). Excluded from the ignored-note report so
# the real signal is not drowned.
VENDOR_SEGMENTS = (
    '/venv/', '/.venv/', '/node_modules/', '/site-packages/', '/__pycache__/',
    '/.tox/', '/.nox/', '/.direnv/', '/virtualenv/', '/.eggs/',
    '/.pytest_cache/', '/.mypy_cache/', '/.ruff_cache/', '/.ipynb_checkpoints/',
    '/.obsidian/', '/.trash/', '/.tmp/', '/.git/',
)
NOTE_GLOBS = ['*.md', '*.canvas', '*.excalidraw']

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

# Environment / dependency / build trees. These are NEVER vault content, and
# staging them is what breaks the backup: 2026-08-21..08-28 a 387MB venv/ appeared
# in obsidian/investments, `git add -A` staged its 12,849 site-packages files,
# TIGHT_SECRET_RE matched ccxt / cryptography / pyjwt sources, and that vault went
# 7 days uncommitted AND unpushed while the liveness watchdog still read green.
# ensure_repo() rewrites this constant into EVERY vault on EVERY run (verified
# t_de78cf24), so a rule added here protects the whole fleet. A per-vault
# .gitignore edit does NOT survive (it is overwritten within one run), and a
# .git/info/exclude band-aid protects exactly one clone and is never committed.
venv/
.venv/
env/
ENV/
virtualenv/
.direnv/
node_modules/
__pycache__/
*.pyc
*.pyo
*.pyd
*.py[cod]
*.so
site-packages/
.eggs/
*.egg-info/
.mypy_cache/
.pytest_cache/
.ruff_cache/
.tox/
.nox/
.ipynb_checkpoints/
.gradle/
.terraform/

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


def load_allowlist(vault: Path) -> tuple[list[re.Pattern[str]], list[str]]:
    """Parse <vault>/.gitleaks-allowlist -> (compiled patterns, invalid lines).

    Invalid lines are returned rather than skipped: an allowlist entry that does
    not compile is a rule the operator BELIEVES is in force. Dropping it quietly
    would leave a permanently-blocked vault with a file that looks like the fix.
    """
    path = vault / ALLOWLIST_NAME
    patterns: list[re.Pattern[str]] = []
    invalid: list[str] = []
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return patterns, invalid
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        try:
            patterns.append(re.compile(line))
        except re.error:
            invalid.append(line[:120])
    return patterns, invalid


def secret_scan_staged(vault: Path, allow: list[re.Pattern[str]]) -> list[str]:
    diff = run(['git', 'diff', '--cached', '-U0', '--no-color'], vault, check=False)
    hits: list[str] = []
    current = '?'
    for line in diff.stdout.splitlines():
        if line.startswith('+++ b/'):
            current = line[6:]
            continue
        if line.startswith('+') and not line.startswith('+++') and TIGHT_SECRET_RE.search(line):
            if any(pat.search(line) for pat in allow):
                continue
            hits.append(current)
    return sorted(set(hits))


def ignored_notes(vault: Path) -> list[str]:
    """Notes the ignore rules are swallowing — the blind spot the ignore list creates.

    Broad directory rules (env/, ENV/, .gradle/ ...) buy immunity from 387MB venvs
    at the cost of a NEW silent-loss mode: a genuine note folder that happens to be
    called env/ would stop being committed with no signal whatsoever. This turns
    that into a loud line instead of a second black hole.
    """
    res = run(
        ['git', 'ls-files', '--others', '--ignored', '--exclude-standard', '--'] + NOTE_GLOBS,
        vault, check=False,
    )
    out: list[str] = []
    for path in res.stdout.splitlines():
        path = path.strip()
        if not path:
            continue
        if any(seg in '/' + path for seg in VENDOR_SEGMENTS):
            continue
        out.append(path)
    return sorted(out)


def raise_alert(key: str, subject: str, body: str) -> None:
    """Put a vault failure on the BOARD — the channel Frank actually reads.

    This job is `Deliver: local`, so its stdout reaches nobody: before this, a
    blocked vault was visible only to whoever ran `hermes cron runs`. Additive and
    never fatal — a backup run that dies because its card write failed is strictly
    worse than a missed card.
    """
    script = Path('/home/frank/.hermes/scripts/fleet-alert-card.sh')
    if not script.exists():
        return
    try:
        subprocess.run(
            [str(script), key, subject, body],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except Exception:  # noqa: BLE001 - alerting must never break the backup
        pass


ALERT_STATE = Path('/home/frank/.hermes/state/host-cron-alert-cards.json')
ALERT_STATE_LOCK = ALERT_STATE.with_name(ALERT_STATE.name + '.lock')


def _clear_alert_pointer(key: str) -> str | None:
    """Drop one pointer under the shared alert-state lock, atomically.

    The slow kanban completion happens before this helper. Re-reading while
    holding the lock ensures concurrent alert producers' keys are preserved.
    """
    lock_fh = None
    tmp_name = None
    try:
        ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
        lock_fh = ALERT_STATE_LOCK.open('a+')
        deadline = time.monotonic() + 20
        while True:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    return 'state-lock-timeout'
                time.sleep(0.25)
        try:
            latest = json.loads(ALERT_STATE.read_text())
        except Exception as exc:  # noqa: BLE001 - never overwrite unreadable state
            return f'state-reread-failed {exc!r}'
        if not isinstance(latest, dict):
            return 'state-reread-failed non-object JSON'
        latest.pop(key, None)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(ALERT_STATE.parent), prefix=f'.{ALERT_STATE.name}.',
        )
        with os.fdopen(fd, 'w', encoding='utf-8') as tmp:
            json.dump(latest, tmp, indent=1, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, ALERT_STATE)
        tmp_name = None
        return None
    except OSError as exc:
        return f'state-write-failed {exc!r}'
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except OSError:
                pass
            lock_fh.close()


def clear_alert(key: str) -> str | None:
    """Close the board card for `key` once that vault is healthy again.

    An alert with no clear path silently becomes a pile: this board already
    carries 12 orphaned '[host-alert] vault autocommit STALE' cards because the
    only cleanup was fleet-alert-card.sh's supersede-on-next-alert, which never
    runs once the fault is fixed. Returns a message if the card could NOT be
    closed — `hermes kanban complete` refuses while a parent is open and says so
    only on stderr, and swallowing that is how the pile grew.
    """
    try:
        import json
        state = json.loads(ALERT_STATE.read_text())
    except Exception:  # noqa: BLE001 - no state file yet is the normal case
        return None
    entry = state.get(key)
    if not isinstance(entry, dict) or not entry.get('card_id'):
        return None
    card = entry['card_id']
    board = entry.get('board', 'jarvis-os')
    try:
        done = subprocess.run(
            ['hermes', 'kanban', '--board', board, 'complete', card,
             '--summary', 'Resolved: the vault committed cleanly on a later autocommit pass.'],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return f'ALERT-CARD-STUCK {key} card={card} {exc!r}'
    cleared_note: str | None = None
    if done.returncode != 0:
        err = (done.stderr or done.stdout).strip().replace('\n', ' ')[:200]
        # `complete` prints ONE message — "unknown id or terminal state" — for three
        # different situations: the card is already done/archived (pointer is
        # stale, e.g. an --idempotency-key create handed back a DONE card, which
        # is how vaultcommit_obsidian-fleet-vault -> t_bb78e42d logged
        # ALERT-CARD-STUCK on every run, review F5), the id no longer exists, OR
        # the card is live but has an open parent. Only the first two mean
        # "already resolved", so read the card's status before dropping the
        # pointer: popping a live card's pointer would orphan it (the pile again).
        if 'terminal state' in err or 'unknown id' in err:
            gone = alert_card_gone(card, board)
            if gone is None:
                return f'ALERT-CARD-STUCK {key} card={card} rc={done.returncode} {err}'
            cleared_note = f'ALERT-POINTER-CLEARED {key} card={card} ({gone})'
        else:
            return f'ALERT-CARD-STUCK {key} card={card} rc={done.returncode} {err}'
    write_error = _clear_alert_pointer(key)
    if write_error:
        return f'ALERT-CARD-STUCK {key} card={card} {write_error}'
    return cleared_note


# Only done/archived cards are safe to forget; triage/todo/scheduled and
# unknown future statuses remain live pointers for operator recovery.

def alert_card_gone(card: str, board: str) -> str | None:
    """Why a stale alert pointer may be dropped, or None if the card is still live.

    Returns 'status=done' / 'no such task' when the card is terminal or missing,
    None when it is open (an open parent is the usual cause) or when its status
    cannot be read (a dead CLI must not look like a resolved alert).
    """
    try:
        shown = subprocess.run(
            ['hermes', 'kanban', '--board', board, 'show', card],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except Exception:  # noqa: BLE001
        return None
    text = (shown.stdout or '') + (shown.stderr or '')
    if shown.returncode != 0:
        return 'no such task' if 'no such task' in text else None
    match = re.search(r'^\s*status:\s*(\S+)', text, re.M)
    if not match:
        return None
    status = match.group(1)
    return f'status={status}' if status in ('done', 'archived') else None


def resolved_vaults() -> list[Path]:
    """VAULTS deduplicated by real path — the CANONICAL entry per repo.

    /home/frank/obsidian/sycode-trading is a SYMLINK to /home/frank/obsidian/quant-team,
    so the raw list ran the same repo twice every tick — wasted work, and a single
    fault printed twice, which reads like two broken vaults. The first VAULTS entry
    for a real path is canonical (quant-team); the later ones are aliases, which
    vault_aliases() reports so the push-state file carries BOTH names (review F3:
    a digest keyed on 'sycode-trading' must not read the vault as never-pushed).
    """
    seen: set[Path] = set()
    out: list[Path] = []
    for vault in VAULTS:
        try:
            key = vault.resolve()
        except OSError:
            key = vault
        if key in seen:
            continue
        seen.add(key)
        out.append(vault)
    return out


def vault_aliases(vault: Path) -> list[Path]:
    """Every OTHER VAULTS entry that resolves to the same repo as `vault`."""
    try:
        target = vault.resolve()
    except OSError:
        return []
    out: list[Path] = []
    for other in VAULTS:
        if other == vault:
            continue
        try:
            if other.resolve() == target:
                out.append(other)
        except OSError:
            continue
    return out


PUSH_STATE = Path('/home/frank/.hermes/state/vault-autocommit-push-state.json')


def record_push_state(vault: Path, pushed: bool) -> str | None:
    """Persist per-vault push state without affecting the backup run.

    One entry per VAULTS name (aliases get a copy tagged `alias_of`), each a dict:
      pushed_at  — last time THIS script pushed commits (unchanged semantics)
      synced_at  — last time HEAD was verified equal to @{upstream}, whether by a
                   push just now or by finding 0 ahead. Without it an unchanging
                   vault never got a key and a "last successful push" digest would
                   read a healthy vault as never-pushed (review F3).
    A legacy bare-string value (pre-F3 format) is upgraded to {pushed_at: value}.
    """
    try:
        try:
            state = json.loads(PUSH_STATE.read_text(encoding='utf-8'))
        except FileNotFoundError:
            state = {}
        if not isinstance(state, dict):
            state = {}
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        entry = state.get(str(vault))
        if isinstance(entry, str):
            entry = {'pushed_at': entry}
        elif not isinstance(entry, dict):
            entry = {}
        if pushed:
            entry['pushed_at'] = now
        entry['synced_at'] = now
        entry.pop('alias_of', None)
        state[str(vault)] = entry
        for alias in vault_aliases(vault):
            state[str(alias)] = dict(entry, alias_of=str(vault))
        PUSH_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PUSH_STATE.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        tmp.replace(PUSH_STATE)
    except OSError as exc:
        return f'PUSH-METRIC-FAILED {vault} {exc}'
    return None


def maybe_push(vault: Path) -> str | None:
    remotes = run(['git', 'remote'], vault, check=False).stdout.split()
    if 'origin' not in remotes:
        return f'NO-REMOTE {vault} — push skipped'
    ahead = run(
        ['git', 'rev-list', '--count', '@{upstream}..HEAD'],
        vault,
        check=False,
    )
    # No upstream yet (first push) still needs a push.
    if ahead.returncode == 0 and ahead.stdout.strip() == '0':
        # In sync: nothing to push, but record that we VERIFIED it (review F3).
        return record_push_state(vault, pushed=False)
    result = run(['git', 'push', '-u', 'origin', 'HEAD'], vault, check=False)
    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip().replace('\n', ' ')[:300]
        # Divergence = the REMOTE history moved and ours is not a descendant. Git
        # says exactly one of: "! [rejected] ... (non-fast-forward)" or
        # "... (fetch first)". A "[remote rejected] ... (pre-receive hook declined)"
        # or push-protection refusal is NOT divergence (review F4) — that stays
        # PUSH-FAILED, so a bare 'rejected' substring must not be matched here.
        lowered = err.lower()
        diverged = (
            'non-fast-forward' in lowered
            or 'fetch first' in lowered
            or '[rejected]' in lowered
        )
        marker = 'PUSH-DIVERGED' if diverged else 'PUSH-FAILED'
        return f'{marker} {vault} rc={result.returncode} {err}'
    metric_error = record_push_state(vault, pushed=True)
    return '\n'.join(filter(None, [f'PUSHED {vault}', metric_error]))


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
        allow, invalid = load_allowlist(vault)
        if invalid:
            # Never silent: an entry that does not compile is a rule the operator
            # thinks is in force, and the vault stays blocked while the file looks fixed.
            messages.append(f'ALLOWLIST-INVALID {vault} {ALLOWLIST_NAME} lines={invalid[:5]}')
        swallowed = ignored_notes(vault)
        if swallowed:
            messages.append(
                f'IGNORED-NOTES {vault} n={len(swallowed)} files={swallowed[:10]} '
                f'— these notes match an ignore rule and are NOT being backed up'
            )
        if diff.returncode != 0:
            blocked = secret_scan_staged(vault, allow)
            if blocked:
                return '\n'.join(messages + [
                    f'SECRET-SCAN-BLOCKED {vault} files={blocked[:20]} '
                    f'| this vault did NOT commit; the other vaults were unaffected '
                    f'| false positive? add a regex line to {vault / ALLOWLIST_NAME} '
                    f'(one regex per line, # comments) — this scanner now reads it'
                ])
            stamp = _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            commit = run(
                ['git', 'commit', '-m', f'chore(obsidian): auto-commit vault snapshot {stamp}'],
                vault, check=False,
            )
            if commit.returncode != 0:
                # A pre-commit hook rejection lands here. Report the hook's own words:
                # the 2026-08-28 fleet-vault outage was an unquoted `#` in the hook's
                # PATTERNS array making the literal string "GitHub" a secret pattern,
                # and the raw hook text is what made that diagnosable.
                err = (commit.stderr or commit.stdout).strip().replace('\n', ' ')[:400]
                return '\n'.join(messages + [
                    f'COMMIT-FAILED {vault} rc={commit.returncode} {err} '
                    f'| this vault did NOT commit; the other vaults were unaffected'
                ])
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


# rc=1 = the backup CHAIN failed for this vault. Keep this list to real chain
# deaths: the exit code is the only liveness signal Hermes no_agent cron reads, so
# widening it to advisories would make a permanently-red job that means nothing.
# PUSH-DIVERGED is a chain death (t_61312ab9 review F1): the off-box copy has
# stopped advancing and will not resume without a human, and the notice-card path
# had never fired for a vaultnotice_* key — so rc=0 here read as GREEN everywhere.
FAILURE_MARKERS = (
    'SECRET-SCAN-BLOCKED', 'COMMIT-FAILED', 'ERROR ', 'MISSING ', 'PUSH-DIVERGED',
)

# Worth a board card, but NOT a chain death — these do not touch rc. Without this
# split, an ignored README.md in a pytest cache would hold the job red forever and
# the exit code would stop meaning "the backup is broken". PUSH-FAILED stays here:
# a network/auth/hook refusal usually clears on the next 30-minute tick.
NOTICE_MARKERS = (
    'ALLOWLIST-INVALID', 'IGNORED-NOTES', 'NO-REMOTE',
    'PUSH-FAILED', 'PUSH-METRIC-FAILED',
)


def main() -> int:
    """One pass over every vault.

    FAILURE POLICY (deliberate, t_de78cf24 2026-08-29 — read before "simplifying"):

    1. ISOLATION. Every vault is attempted independently and a failure in one is
       returned, not raised past the loop, so a blocked vault can never stop the
       vaults after it from backing up. Verified by fault injection, not by reading.

    2. THE EXIT CODE STAYS 1 ON A HANDLED FAILURE. Hermes `no_agent` cron treats the
       process exit code as the ONLY liveness signal — stdout is delivered and stored
       but never parsed, and the mechanism matrix marks any last_status not in
       (None, "ok") DEAD. Exiting 0 on a blocked vault would convert an honestly-dead
       backup into a fabricated GREEN row, which is strictly worse than the noise.
       So visibility is NOT bought by softening the exit code.

    3. VISIBILITY COMES FROM THE BOARD INSTEAD. This job is `Deliver: local`, i.e.
       its stdout reaches nobody; a blocked vault used to be visible only to whoever
       ran `hermes cron runs`. Every failing vault now also raises a board card via
       fleet-alert-card.sh (one card per vault key, superseding its predecessor) —
       the channel Frank actually reads.

    4. THE SUMMARY LINE IS COUNTED, NOT BOOLEAN, so one blocked vault cannot be read
       as "the backup is down" — and so a silent drop from 3 healthy vaults to 1 is
       visible even when nothing errored.
    """
    messages: list[str] = []
    rc = 0
    vaults = resolved_vaults()
    ok_vaults = 0
    failed: list[str] = []
    for vault in vaults:
        try:
            msg = autocommit(vault)
        except subprocess.CalledProcessError as exc:
            msg = (
                f'ERROR {vault}: {exc.cmd} rc={exc.returncode} '
                f'stdout={(exc.stdout or "").strip()[:300]} stderr={(exc.stderr or "").strip()[:300]}'
            )
        except Exception as exc:  # noqa: BLE001 - cron watchdog should fail visibly.
            msg = f'ERROR {vault}: {exc!r}'
        if msg and any(marker in msg for marker in FAILURE_MARKERS):
            rc = 1
            failed.append(vault.name)
            raise_alert(
                f'vaultcommit_{vault.name}',
                f'🚨 vault autocommit FAILED: {vault.name}',
                f'{msg}\n\nThe other vaults in this pass were NOT stopped — '
                f'{len(vaults) - len(failed)} of {len(vaults)} still committed.\n'
                f'Script: /home/frank/.hermes/scripts/obsidian_vault_autocommit.py\n'
                f'Job: hermes cron b2536429e954 (obsidian-vault-git-autocommit, every 30m)\n'
                f'False-positive secret hit? add a regex line to {vault / ALLOWLIST_NAME}.',
            )
        else:
            ok_vaults += 1
            stuck = clear_alert(f'vaultcommit_{vault.name}')
            if stuck:
                messages.append(stuck)
            if not (msg and any(marker in msg for marker in NOTICE_MARKERS)):
                # Same lesson as the failure card: an alert with no clear path
                # becomes a pile. Clear the notice card once the notice stops.
                stuck_notice = clear_alert(f'vaultnotice_{vault.name}')
                if stuck_notice:
                    messages.append(stuck_notice)
            else:
                raise_alert(
                    f'vaultnotice_{vault.name}',
                    f'⚠️ vault autocommit notice: {vault.name}',
                    f'{msg}\n\nThe backup chain itself is HEALTHY for this vault '
                    f'(it committed/pushed normally) — this is a scope warning, which '
                    f'is why the cron exit code stays 0 for it.\n'
                    f'Script: /home/frank/.hermes/scripts/obsidian_vault_autocommit.py',
                )
        if msg:
            messages.append(msg)
    if failed:
        messages.append(
            f'SUMMARY vaults={len(vaults)} healthy={ok_vaults} failed={len(failed)} '
            f'({", ".join(failed)}) — failures are isolated per vault, the rest still backed up'
        )
    if messages:
        print('\n'.join(messages))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
