#!/usr/bin/env python3
"""
Governance-to-Kanban Task Bridge — scans Obsidian fleet vault governance notes,
extracts all kanban task references, queries each board via SQLite for status,
and reports linkage gaps.

Usage:
  python3 governance-task-bridge.py [--report] [--verbose]

Outputs JSON summary to stdout; readable report with --report.
"""
import json, os, re, sqlite3, sys, yaml
from pathlib import Path

# Config
VAULT = Path(os.environ.get('OBSIDIAN_FLEET_VAULT', '/home/frank/obsidian-fleet-vault'))
KANBAN_HOME = Path(os.environ.get('HERMES_KANBAN_HOME', '/home/frank/.hermes/kanban'))
GOV_DIR = VAULT / 'Governance'

# Known boards and their DB paths
BOARDS = {}
for bd in sorted(KANBAN_HOME.glob('boards/*')):
    db = bd / 'kanban.db'
    if db.exists():
        BOARDS[bd.name] = db

def parse_frontmatter(text):
    m = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if not m: return {}
    try: return yaml.safe_load(m.group(1)) or {}
    except: return {}

def extract_task_ids(text):
    """Extract t_XXXX from YAML, wikilinks, and body text."""
    ids = set()
    # YAML kanban_task field (can be jarvis-os/t_XXXX or just t_XXXX)
    fm = parse_frontmatter(text)
    kt = fm.get('kanban_task', '') or ''
    if isinstance(kt, list):
        [ids.add(k.split('/')[-1]) for k in kt]
    elif kt:
        ids.add(kt.split('/')[-1])
    # related_tasks
    rt = fm.get('related_tasks', []) or []
    if isinstance(rt, list):
        for t in rt:
            if isinstance(t, dict):
                # some notes have {task_id: ..., title: ...}
                v = t.get('task_id', '') or t.get('title', '') or ''
                if v: ids.add(v.split('/')[-1])
            elif isinstance(t, str):
                ids.add(t.split('/')[-1])
    elif isinstance(rt, str):
        ids.add(rt.split('/')[-1])
    # Body wikilinks — matches both [[t_XXXX]] and [[board/t_XXXX]]
    for m in re.finditer(r'(?:^|\s|\[\[)(t_[a-zA-Z0-9]{4,12})(?:\]\]|$|\s)', text):
        ids.add(m.group(1))
    return sorted(ids)

def query_task_status(db_path, task_id):
    """Query a single task's status from a kanban SQLite DB."""
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        cur = conn.cursor()
        cur.execute("SELECT id, title, status, assignee FROM tasks WHERE id=?", (task_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            return {'id': row[0], 'title': row[1], 'status': row[2], 'assignee': row[3]}
        return None
    except Exception as e:
        return {'error': str(e)}

def main():
    verbose = '--verbose' in sys.argv
    report_mode = '--report' in sys.argv

    # 1. Scan governance notes
    notes = []
    for f in sorted(GOV_DIR.glob('*.md')):
        text = f.read_text()
        fm = parse_frontmatter(text)
        task_ids = extract_task_ids(text)
        notes.append({
            'file': f.name,
            'title': fm.get('title', '').strip(),
            'date': str(fm.get('date', '')),
            'status': fm.get('status', '') or '',
            'type': fm.get('type', '') or 'note',
            'task_ids': task_ids,
        })

    # 2. Cross-reference each task against boards
    all_tasks_hit = 0
    matching_tasks = 0
    unmatched_notes = []
    status_counts = {'done': 0, 'running': 0, 'blocked': 0, 'todo': 0, 'ready': 0, 'triage': 0, 'unknown': 0}
    board_counts = {}
    findings = []

    for note in notes:
        note_hit = 0
        for ref in note['task_ids']:
            all_tasks_hit += 1
            found = False
            for bname, dbp in BOARDS.items():
                info = query_task_status(dbp, ref)
                if info and 'error' not in info and info['status']:
                    found = True
                    matching_tasks += 1
                    note_hit += 1
                    status_counts[info['status']] = status_counts.get(info['status'], 0) + 1
                    board_counts.setdefault(bname, {})
                    board_counts[bname][info['status']] = board_counts[bname].get(info['status'], 0) + 1
                    if info['status'] != 'done':
                        findings.append({
                            'board': bname,
                            'task_id': ref,
                            'status': info['status'],
                            'assignee': info.get('assignee',''),
                            'title': info.get('title',''),
                            'governance_note': note['file'],
                        })
                    break
            if not found:
                unmatched_notes.append({
                    'file': note['file'],
                    'task_id': ref,
                    'title': note['title'],
                })

        if verbose and note_hit == 0 and note['task_ids']:
            print(f'  NOTE {note["file"]}: {len(note["task_ids"])} refs, 0 matched on any board', file=sys.stderr)

    # 3. Report
    result = {
        'scan_date': __import__('datetime').datetime.utcnow().isoformat(),
        'boards_inventoried': sorted(BOARDS.keys()),
        'governance_notes_scanned': len(notes),
        'total_task_references': all_tasks_hit,
        'matched_on_board': matching_tasks,
        'unmatched_refs': len(unmatched_notes),
        'status_summary': status_counts,
        'board_status': {k: v for k, v in sorted(board_counts.items())},
        'open_findings': findings,
        'unmatched': unmatched_notes,
    }

    if report_mode:
        print(f"\n{'='*60}")
        print(f"GOVERNANCE TASK BRIDGE REPORT — {result['scan_date']}")
        print(f"{'='*60}")
        print(f"Governance notes scanned:  {result['governance_notes_scanned']}")
        print(f"Total task refs extracted: {result['total_task_references']}")
        print(f"Matched on kanban boards:  {result['matched_on_board']}")
        print(f"Unmatched refs:            {result['unmatched_refs']}")
        print(f"Boards inventoried:        {', '.join(result['boards_inventoried'])}")
        print(f"\n--- Status distribution ---")
        for s, c in sorted(result['status_summary'].items()):
            print(f"  {s:12s}: {c}")
        print(f"\n--- Per-board breakdown ---")
        for b, st in sorted(result['board_status'].items()):
            print(f"  {b}:")
            for s, c in sorted(st.items()):
                print(f"    {s:12s}: {c}")
        print(f"\n--- Open findings (linked but not done) ---")
        for f_ in result['open_findings']:
            print(f"  [{f_['status']:8s}] {f_['board']}/{f_['task_id']} — {f_['title'][:80]}")
            print(f"          Governance note: {f_['governance_note']}")
        print(f"\n--- Unmatched refs ---")
        for u in result['unmatched'][:20]:
            print(f"  {u['task_id']} in {u['file']} — \"{u['title'][:80]}\"")
        if len(result['unmatched']) > 20:
            print(f"  ... and {len(result['unmatched']) - 20} more")
        print(f"\nVerification: matched={result['matched_on_board']}/{result['total_task_references']}, unmatched={result['unmatched_refs']}")

    print(json.dumps(result, indent=2))
    return result

if __name__ == '__main__':
    main()
