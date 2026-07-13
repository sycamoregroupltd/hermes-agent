#!/usr/bin/env python3
"""cross-pm-fanout.py — propagate cross-PM lessons onto target boards as
lightweight pointer COMMENTS (never cards, never task-state mutation).

For each lesson in lessons/INDEX.jsonl with propagation_state == 'new' whose
`relevant_to` includes a PM:
  - resolve PM -> board (jarvis-os-pm->jarvis-os, sycode-trading-pm->sycode-trading,
    upero-pm->upero, sycode-ai-pm->sycode-ai)
  - pick the most tag/token-relevant OPEN task on that board
  - append a one-line pointer COMMENT (lesson_id + title + rule + path)
  - record landed_in[board] = <task_id> and bump propagation_state to 'landed'

Idempotency: per-lesson content hash stored as `fanout_hash`. Re-runs are no-ops
unless the lesson file content changed (hash differs). Once all relevant boards
have a recorded landing, the lesson is 'landed' and skipped.

Guards (SOUL hard gates):
  - Touches ONLY kanban comments + the shared-memory/lessons tree.
  - Never mutates task state, credentials, gateway, or prod data.
  - No new service/DB. Reuses shared-memory + existing kanban CLI.
  - sycode-trading lessons: comments are read-only pointers; no trade action.

Usage:
  cross-pm-fanout.py [--dry-run] [--index PATH] [--once]
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

HERMES = "/home/frank/.local/bin/hermes"
HERMES_HOME = "/home/frank/.hermes"
DEFAULT_INDEX = "/home/frank/.hermes/shared-memory/lessons/INDEX.jsonl"
METRICS_PATH = "/home/frank/.hermes/shared-memory/lessons/metrics.jsonl"

PM_TO_BOARD = {
    "jarvis-os-pm": "jarvis-os",
    "sycode-trading-pm": "sycode-trading",
    "upero-pm": "upero",
    "sycode-ai-pm": "sycode-ai",
}
OPEN_STATUSES = ["running", "ready", "todo", "blocked", "triage", "scheduled", "review"]
STOPWORDS = set(
    "the a an of to in for on and or with that this is are be by from at as it its not no if when then can will should must may do does done into out up down over under between within which what who how why all any each".split()
)


def _run_hermes(args, dry_run=False):
    """Run a hermes CLI command. In dry-run, print and skip side effects that
    mutate (we still may call read-only list/show, but never comment)."""
    if dry_run and args[:1] == ["kanban"] and "comment" in args:
        print("  [dry-run] WOULD COMMENT: hermes " + " ".join(args))
        return 0, "", ""
    env = dict(os.environ)
    env["HERMES_HOME"] = HERMES_HOME
    p = subprocess.run([HERMES] + args, capture_output=True, text=True, env=env, timeout=120)
    return p.returncode, p.stdout, p.stderr


def content_hash(lesson):
    blob = "\n".join([
        lesson.get("lesson_id", ""),
        lesson.get("root_cause", ""),
        lesson.get("rule", ""),
        "\n".join(lesson.get("tags") or []),
    ])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def tokenize(text):
    if not text:
        return set()
    toks = re.findall(r"[a-z0-9_]+", text.lower())
    return {t for t in toks if t not in STOPWORDS and len(t) > 1}


def fetch_open_tasks(board):
    tasks = []
    for st in OPEN_STATUSES:
        rc, out, err = _run_hermes(["kanban", "--board", board, "list", "--status", st, "--json"])
        if rc != 0 or not out.strip():
            continue
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            continue
        rows = data if isinstance(data, list) else data.get("tasks", [])
        tasks.extend(rows)
    return tasks


def pick_target_task(board, lesson):
    tasks = fetch_open_tasks(board)
    if not tasks:
        return None
    ltokens = tokenize(" ".join([
        lesson.get("title", ""),
        lesson.get("rule", ""),
        " ".join(lesson.get("tags") or []),
        lesson.get("root_cause", ""),
    ]))
    best, best_sc = None, -1
    for t in tasks:
        ttext = " ".join([str(t.get("title", "") or ""), str(t.get("body", "") or "")])
        sc = len(ltokens & tokenize(ttext))
        # tie-break by priority so high-priority tasks win on equal overlap
        prio = t.get("priority") or 0
        sc = sc * 1000 + (prio if isinstance(prio, int) else 0)
        if sc > best_sc:
            best, best_sc = t, sc
    return best


def comment_body(lesson):
    return "[Cross-PM lesson] {lid} — {title} — {rule} — {path}".format(
        lid=lesson.get("lesson_id", "?"),
        title=" ".join((lesson.get("title") or "").split()),
        rule=" ".join((lesson.get("rule") or "").split()),
        path=lesson.get("path", ""),
    )


def write_metrics(board, fanned, landed):
    line = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "board": board,
        "lessons_fanned": fanned,
        "lessons_landed_in_task": landed,
    })
    try:
        with open(METRICS_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as e:
        print(f"  [warn] metrics write failed: {e}", file=sys.stderr)


def already_commented(task_id, body, board):
    """Scan the target task's existing comments for an identical pointer.
    Defense-in-depth idempotency even if INDEX state is stale."""
    rc, out, _ = _run_hermes(["kanban", "--board", board, "show", task_id, "--json"])
    if rc != 0:
        return False
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return False
    for c in d.get("comments", []) or []:
        if c.get("body", "").strip() == body.strip():
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description="Cross-PM lesson fan-out (comments only).")
    ap.add_argument("--dry-run", action="store_true", help="Plan only; post no comments, mutate no INDEX.")
    ap.add_argument("--index", default=DEFAULT_INDEX, help="Path to INDEX.jsonl.")
    ap.add_argument("--once", action="store_true", help="Process once (default behaviour; kept for clarity).")
    args = ap.parse_args()

    try:
        with open(args.index, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        print("INDEX not found:", args.index, file=sys.stderr)
        return 1

    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    changed_any = False
    for lesson in entries:
        if lesson.get("propagation_state") != "new":
            continue
        rt = lesson.get("relevant_to") or []
        pms = [p for p in rt if p in PM_TO_BOARD]
        if not pms:
            continue

        cur_hash = content_hash(lesson)
        landed = dict(lesson.get("landed_in") or {})
        fanout_hash = lesson.get("fanout_hash")

        # Full no-op: already fanned and content unchanged
        if fanout_hash == cur_hash and landed:
            continue

        body = comment_body(lesson)
        total_fanned = 0
        total_landed = 0
        for pm in pms:
            board = PM_TO_BOARD[pm]
            # skip boards already landed with matching content
            if board in landed and fanout_hash == cur_hash:
                continue
            target = pick_target_task(board, lesson)
            if not target:
                print(f"  [skip] {lesson.get('lesson_id')} -> {board}: no open task to land on")
                continue
            tid = target.get("id")
            if args.dry_run:
                print(f"  [dry-run] target task for {lesson.get('lesson_id')} on {board}: {tid}")
            total_fanned += 1
            if already_commented(tid, body, board):
                print(f"  [noop] {lesson.get('lesson_id')} already on {tid} ({board})")
                landed[board] = tid
                total_landed += 1
                continue
            rc, out, err = _run_hermes(["kanban", "--board", board, "comment", tid, body], dry_run=args.dry_run)
            if rc == 0:
                print(f"  [ok] {lesson.get('lesson_id')} -> comment on {tid} ({board})")
                landed[board] = tid
                total_landed += 1
            else:
                print(f"  [ERR] comment failed for {tid}: {err.strip()}", file=sys.stderr)

        if total_fanned > 0 and not args.dry_run:
            write_metrics(",".join(pms), total_fanned, total_landed)
        # record landing + hash; mark landed once it reached >=1 board
        lesson["landed_in"] = landed
        lesson["fanout_hash"] = cur_hash
        if landed:
            lesson["propagation_state"] = "landed"
        changed_any = True

    if changed_any and not args.dry_run:
        with open(args.index, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        print("INDEX updated.")
    elif args.dry_run:
        print("DRY-RUN: no INDEX mutation.")
    else:
        print("Nothing to fan out (all lessons landed or not 'new').")
    return 0


if __name__ == "__main__":
    sys.exit(main())
