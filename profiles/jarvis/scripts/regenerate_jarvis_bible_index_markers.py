#!/usr/bin/env python3
"""Regenerate marker-bounded mechanical sections in JARVIS-BIBLE-INDEX.md.

Only the generated source-count and kanban-board tables are rewritten. Narrative
runbook/doctrine sections remain hand-curated.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

INDEX = Path('/home/frank/uaa-rules/JARVIS-BIBLE-INDEX.md')
DELTA = Path('/home/frank/uaa-rules/JARVIS-BIBLE-INDEX.delta.md')
MANIFEST = Path('/home/frank/uaa-rules/JARVIS-BIBLE-INDEX.manifest.json')
UAA = Path('/home/frank/uaa-rules')
SKILLS = Path('/home/frank/.hermes/skills')
BOARDS = Path('/home/frank/.hermes/kanban/boards')
TASK_SOURCE = 't_f77e74a6'

SOURCE_BEGIN = '<!-- BEGIN AUTO:SOURCE_COUNTS -->'
SOURCE_END = '<!-- END AUTO:SOURCE_COUNTS -->'
BOARD_BEGIN = '<!-- BEGIN AUTO:KANBAN_BOARD_LINKS -->'
BOARD_END = '<!-- END AUTO:KANBAN_BOARD_LINKS -->'

PM_PROTOCOL_SKILLS = [
    'app-verification', 'cross-pm-memory', 'dgx-migration', 'fleet-cron-operations',
    'hermes-profile-auditing', 'jarvis-watch', 'kanban', 'kanban-orchestrator',
    'kanban-voice-escalation', 'kanban-worker', 'land-after-approve',
    'meta-boris-harness-evolution', 'new-project', 'proactive-pm-protocol',
    'scheduled-agent-operations',
]
DGX_DOCS = [
    Path('/home/frank/uaa-rules/MISSION-CONTROL-SPEC.md'),
    Path('/home/frank/uaa-rules/DGX-MIGRATION-PLAN.md'),
    Path('/home/frank/jarvis/workspace/mission_control/latest.md'),
    Path('/home/frank/jarvis/workspace/memory/artifacts/dgx-voice-final-architecture-runbook.md'),
    Path('/home/frank/jarvis/workspace/memory/artifacts/dgx-elevenlabs-delegation-report.md'),
    Path('/home/frank/jarvis/workspace/memory/artifacts/elevenlabs-phone-bridge-runbook.md'),
    Path('/home/frank/jarvis/workspace/memory/artifacts/elevenlabs-voice-status.md'),
]

def read(path: Path) -> str:
    return path.read_text(errors='ignore') if path.exists() else ''

def title(path: Path) -> str:
    text = read(path)
    for line in text.splitlines():
        if line.startswith('# '):
            return line[2:].strip()
    return path.stem.replace('-', ' ')

def status_counts(db: Path):
    conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        counts = dict(conn.execute("select status, count(*) from tasks group by status").fetchall())
        week_ago = int(datetime.now(timezone.utc).timestamp()) - 7 * 86400
        done7 = conn.execute("select count(*) from tasks where status='done' and completed_at >= ?", (week_ago,)).fetchone()[0]
        north = conn.execute("select id from tasks where coalesce(status,'') != 'archived' and (upper(title) like '%NORTH STAR%' or upper(body) like '%NORTH STAR%') order by created_at limit 8").fetchall()
        return counts, done7, [r[0] for r in north]
    finally:
        conn.close()

def count_sources():
    uaa_top = [p for p in UAA.glob('*.md') if p.is_file()]
    proposals = [p for p in (UAA / 'proposals').glob('*.md')] if (UAA / 'proposals').exists() else []
    known = [p for p in (UAA / 'known-fixes').glob('*.md')] if (UAA / 'known-fixes').exists() else []
    skill_files = [p for p in SKILLS.rglob('SKILL.md') if p.is_file()]
    categories = Counter()
    for p in skill_files:
        try:
            rel = p.relative_to(SKILLS)
            categories[rel.parts[0] if len(rel.parts) > 1 else '(root)'] += 1
        except ValueError:
            pass
    pm_existing = [s for s in PM_PROTOCOL_SKILLS if list(SKILLS.rglob(f'{s}/SKILL.md'))]
    dgx_existing = [p for p in DGX_DOCS if p.exists()]
    board_dbs = sorted(BOARDS.glob('*/kanban.db'))
    north_count = 0
    for db in board_dbs:
        try:
            _, _, north = status_counts(db)
            north_count += len(north)
        except Exception:
            pass
    return {
        'uaa_top': len(uaa_top), 'proposals': len(proposals), 'known': len(known),
        'skills': len(skill_files), 'categories': categories, 'pm_protocol': len(pm_existing),
        'dgx_docs': len(dgx_existing), 'boards': len(board_dbs), 'north': north_count,
    }

def source_table(stats) -> str:
    cat = ', '.join(f'{k}={v}' for k, v in stats['categories'].most_common(8))
    rows = [
        '| Source set | Count | Notes |',
        '|---|---:|---|',
        f"| `/home/frank/uaa-rules/**/*.md` | {stats['uaa_top'] + stats['proposals'] + stats['known']} | {stats['uaa_top']} top-level docs, {stats['proposals']} proposals, {stats['known']} known-fixes docs; includes this index and its delta log. |",
        f"| Shared Hermes skills at `/home/frank/.hermes/skills/**/SKILL.md` | {stats['skills']} | {cat}. |",
        f"| PM / fleet protocol skills indexed | {stats['pm_protocol']} | {', '.join(PM_PROTOCOL_SKILLS)}. |",
        f"| DGX / Mission Control docs indexed | {stats['dgx_docs']} | UAA Mission Control/DGX docs plus DGX voice/delegation artifacts that exist on this host. |",
        f"| Kanban board DBs sampled | {stats['boards']} | `/home/frank/.hermes/kanban/boards/*/kanban.db`, read-only SQLite. |",
        f"| North-star task references found | {stats['north']} | Active, non-archived tasks whose title/body mentions `NORTH STAR`. |",
    ]
    return '\n'.join(rows)

def board_table():
    rows = ['| Board | DB path | Open shape | done/7d | North-star IDs |', '|---|---|---:|---:|---|']
    for db in sorted(BOARDS.glob('*/kanban.db')):
        board = db.parent.name
        try:
            counts, done7, north = status_counts(db)
            open_shape = ', '.join(f'{k} {v}' for k, v in sorted(counts.items()) if k != 'archived') or 'no tasks sampled'
            north_text = ', '.join(f'`{n}`' for n in north) if north else '—'
            rows.append(f'| `{board}` | `{db}` | {open_shape} | {done7} | {north_text} |')
        except Exception as exc:
            rows.append(f'| `{board}` | `{db}` | scan-error: {exc} | 0 | — |')
    return '\n'.join(rows)

def replace_section(text: str, heading: str, begin: str, end: str, body: str) -> str:
    block = f'{begin}\n{body}\n{end}'
    if begin in text and end in text:
        start = text.index(begin)
        stop = text.index(end, start) + len(end)
        return text[:start] + block + text[stop:]
    h = text.index(heading)
    next_h = text.find('\n## ', h + len(heading))
    if next_h == -1:
        next_h = len(text)
    prefix = text[: h + len(heading)]
    suffix = text[next_h:]
    return prefix.rstrip() + '\n\n' + block + '\n' + suffix

def main():
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    old_text = read(INDEX)
    if not old_text:
        raise SystemExit(f'missing {INDEX}')
    stats = count_sources()
    new_text = old_text
    new_text = new_text.replace(old_text.splitlines()[2], f'Generated: {now}', 1) if old_text.splitlines()[2].startswith('Generated:') else new_text
    new_text = replace_section(new_text, '## Source counts', SOURCE_BEGIN, SOURCE_END, source_table(stats))
    new_text = replace_section(new_text, '## Kanban board links', BOARD_BEGIN, BOARD_END, board_table())
    INDEX.write_text(new_text)
    manifest = {'generated_at': now, 'task_source': TASK_SOURCE, 'stats': {k: (dict(v) if isinstance(v, Counter) else v) for k, v in stats.items()}, 'index': str(INDEX)}
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    DELTA.parent.mkdir(parents=True, exist_ok=True)
    with DELTA.open('a') as f:
        f.write(f"- {now} — marker-bounded mechanical refresh: uaa_docs={stats['uaa_top'] + stats['proposals'] + stats['known']}; shared_skills={stats['skills']}; kanban_boards={stats['boards']}; north_star_tasks={stats['north']} (task `{TASK_SOURCE}`)\n")
    print(f'JARVIS_BIBLE_INDEX_REFRESH_OK index={INDEX} manifest={MANIFEST} task={TASK_SOURCE}')

if __name__ == '__main__':
    main()
