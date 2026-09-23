#!/usr/bin/env python3
"""
pipeline-divergence-apply.py — PM-gated STOP step for the pipeline-divergence
detector (t_9894ccf9). Mirrors board-failure-reclaim-apply.py: detection is
automated and read-only (pipeline-divergence-detector.py); blocking is MANUAL
and PM/Frank-gated.

STOP actions supported (dry-run by default; --apply required to mutate):
  DIVERGENT_ORCHESTRATOR / SHADOW_DONE
      Cards that reached done/archived with zero native Hermes runs and no
      review gate were concluded OUTSIDE the pipeline by a second orchestrator.
      STOP: if the card is still 'done' (not yet landed / contract not
      fulfilled / a review or merger gate remains), move it to `blocked`
      (kind `transient`, reason `pipeline-bypass`) so no downstream consumer
      treats it as authoritative, until a PM or reviewer disposes of it
      (re-route through a real Hermes lane → verify → Review → Merge, or
      explicitly archive with a dated disposition note).
      Cards already `archived` are left untouched (the archive is the accepted
      disposition; flagging is historical/tracked, no need to re-block).
  SHADOW_SPAWN
      A real OWNED card had a spawned run with no matching claim. STOP: block
      the card (kind `transient`, reason `pipeline-bypass-shadow-spawn`) so the
      unclaimed worker is not treated as authoritative and the owner profile
      can re-claim atomically. (Only applies to non-mock owned cards — the
      detector already excludes factory/mock scaffolding.)

Safety / isolation constraints:
  - READ-ONLY unless --apply (matches canonical board-failure-reclaim gate).
  - No hermes update, no money, no deploy. Idempotent (re-blocking is a no-op
    via status check).
  - Blocking uses the same `blocked` (kind transient) mechanism the reclaim
    executor uses — safe, reversible, PM-visible on the board.
  - This script NEVER touches git, branches, worktrees, or credentials.
"""
import argparse, json, os, sqlite3, sys, time


def connect(db):
    if not os.path.exists(db):
        raise SystemExit(f"DB not found: {db}")
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def load_bypass(events_path):
    with open(events_path) as fh:
        return json.load(fh)


def plan(c, events):
    """Return actionable STOP list: only cards we may gate without overreach."""
    actions = []
    for e in events:
        if e['class'] == 'SHADOW_SPAWN':
            card = c.execute("select id,status from tasks where id=?", (e['task_id'],)).fetchone()
            if card and card['status'] in ('running', 'ready', 'done'):
                actions.append({
                    'task_id': e['task_id'], 'class': e['class'],
                    'action': 'block', 'reason': 'pipeline-bypass-shadow-spawn',
                })
        elif e['class'] == 'DIVERGENT_ORCHESTRATOR':
            # Only gate cards still 'done' (not yet archived). Archived cards are
            # the accepted historical disposition — flag, don't re-block.
            card = c.execute("select id,status from tasks where id=?", (e['task_id'],)).fetchone()
            if card and card['status'] == 'done':
                actions.append({
                    'task_id': e['task_id'], 'class': e['class'],
                    'action': 'block', 'reason': 'pipeline-bypass',
                })
    return actions


def apply_block(c, task_id, reason):
    c.execute(
        "update tasks set status='blocked', block_kind='transient', "
        "last_failure_error=? where id=?",
        (reason, task_id))
    c.execute(
        "insert into task_events (task_id, run_id, kind, payload, created_at) "
        "values (?, NULL, 'commented', ?, ?)",
        (task_id, json.dumps({'author': 'pipeline-divergence-apply',
                              'note': 'STOP: pipeline-bypass — work concluded '
                                      'outside the Voice/Jarvis→Kanban→lane→'
                                      'verify→Review→Merge lifecycle. PM to '
                                      're-route through a real lane + review '
                                      'gate, or dispose.'}),
         int(time.time())))
    c.execute(
        "insert into task_events (task_id, run_id, kind, payload, created_at) "
        "values (?, NULL, 'blocked', ?, ?)",
        (task_id, json.dumps({'reason': reason, 'kind': 'transient',
                              'source_status': 'done'}), int(time.time())))


def main():
    ap = argparse.ArgumentParser(description='Pipeline-divergence STOP (PM-gated)')
    ap.add_argument('events', help='JSON events file from pipeline-divergence-detector.py --json')
    ap.add_argument('--db', default=os.path.expanduser(
        '~/.hermes/kanban/boards/jarvis-os/kanban.db'))
    ap.add_argument('--apply', action='store_true',
                    help='REQUIRED to mutate the board (block cards). Default is dry-run.')
    args = ap.parse_args()

    c = connect(args.db)
    events = load_bypass(args.events)
    actions = plan(c, events)

    print(f"bypass events: {len(events)}; actionable STOP targets: {len(actions)}")
    for a in actions:
        print("  STOP {0} {1} -> block({2})".format(a['class'], a['task_id'], a['reason']))

    if not args.apply:
        print("\nDRY-RUN — no board mutation. Re-run with --apply to block listed cards.")
        return 0

    if not actions:
        print("nothing to apply.")
        return 0

    for a in actions:
        apply_block(c, a['task_id'], a['reason'])
        print(f"  blocked {a['task_id']} ({a['reason']})")
    c.commit()
    print(f"\nAPPLIED {len(actions)} block(s). PM to triage each card.")
    return 0


if __name__ == '__main__':
    sys.exit(main())