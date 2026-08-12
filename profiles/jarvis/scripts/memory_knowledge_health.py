#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Read-only Hermes memory/knowledge health check."""
from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

HOME = Path('/home/frank')
PROFILE = HOME / '.hermes/profiles/jarvis'
FLEET = HOME / 'obsidian-fleet-vault'
QUANT = HOME / 'obsidian/quant-team'


def run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout.strip()
    except Exception as e:
        return 999, f'{type(e).__name__}: {e}'


def file_chars(path: Path) -> int:
    return len(path.read_text(errors='ignore')) if path.exists() else -1


def wikilink_health(root: Path) -> tuple[int, int]:
    md_files = [p for p in root.rglob('*.md') if '.trash' not in p.parts]
    stems = {p.stem for p in md_files}
    rel_no_ext = {str(p.relative_to(root).with_suffix('')) for p in md_files}
    broken = 0
    links = 0
    for p in md_files:
        text = p.read_text(errors='ignore')
        for m in re.findall(r'\[\[([^\]|#]+)', text):
            links += 1
            target = m.strip()
            if target not in stems and target not in rel_no_ext:
                broken += 1
    return links, broken


def main() -> int:
    issues: list[str] = []
    facts: list[str] = []

    mem = PROFILE / 'memories/MEMORY.md'
    user = PROFILE / 'memories/USER.md'
    mem_chars = file_chars(mem)
    user_chars = file_chars(user)
    facts.append(f'built_in_memory_chars={mem_chars}/2200 user_chars={user_chars}/1375')
    if mem_chars > 1980:
        issues.append(f'MEMORY.md above 90% ({mem_chars}/2200)')
    if user_chars > 1237:
        issues.append(f'USER.md above 90% ({user_chars}/1375)')

    rc, honcho = run(['hermes', 'honcho', 'status'], timeout=60)
    facts.append(f'honcho_status_rc={rc}')
    if rc != 0 or 'OK' not in honcho:
        issues.append('Honcho status not OK')
    if 'Context budget: (uncapped)' in honcho:
        issues.append('Honcho context budget uncapped')
    if 'No peer data yet' in honcho:
        issues.append('Honcho peer data still empty')

    for vault, name in [(FLEET, 'fleet'), (QUANT, 'quant')]:
        for required in ['SCHEMA.md', 'index.md', 'log.md']:
            if not (vault / required).exists():
                issues.append(f'{name} vault missing {required}')
        links, broken = wikilink_health(vault)
        facts.append(f'{name}_wikilinks={links} broken={broken}')
        if broken:
            issues.append(f'{name} vault has {broken} possibly broken wikilinks')

    db_candidates = [PROFILE / 'state.db', HOME / '.hermes/state.db']
    db = next((p for p in db_candidates if p.exists()), None)
    if db:
        try:
            con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
            cur = con.cursor()
            cur.execute("select count(*) from sqlite_master where type='table'")
            facts.append(f'session_db={db} tables={cur.fetchone()[0]}')
            con.close()
        except Exception as e:
            issues.append(f'session DB read failed: {e}')
    else:
        issues.append('No session state.db found')

    if issues:
        print('MEMORY_KNOWLEDGE_HEALTH_WARN')
        for x in facts:
            print('FACT', x)
        for x in issues:
            print('ISSUE', x)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
