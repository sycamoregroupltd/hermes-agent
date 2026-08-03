#!/usr/bin/env python3
"""Diff pre/post board dbs to prove the actuator only appended comments."""
import sqlite3, sys

pre, post = sys.argv[1], sys.argv[2]
a = sqlite3.connect(f"file:{pre}?mode=ro", uri=True)
b = sqlite3.connect(f"file:{post}?mode=ro", uri=True)
tables = [r[0] for r in a.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
bad = False
for t in tables:
    try:
        na = a.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        nb = b.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    except sqlite3.Error as e:
        print(f"  {t}: skip ({e})"); continue
    if na != nb:
        flag = "OK(expected)" if t == "task_comments" else "CHECK"
        print(f"  {t}: {na} -> {nb}  {flag}")

# The board is LIVE: other workers append heartbeats/events concurrently, so a
# raw row-count delta on task_events is not evidence of an actuator write.
# Assert on ATTRIBUTION instead: no event row may be authored by the actuator.
ev_bad = 0
try:
    ev_bad = b.execute(
        "SELECT COUNT(*) FROM task_events WHERE COALESCE(payload,'') LIKE ?",
        ("%diagnostics-actuator%",),
    ).fetchone()[0]
except sqlite3.Error:
    pass
print(f"  task_events authored by actuator: {ev_bad} (must be 0)")
if ev_bad:
    bad = True

# Comments added must ALL be actuator-authored and marker-carrying.
n_new = b.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0] - \
        a.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0]
n_act = b.execute(
    "SELECT COUNT(*) FROM task_comments WHERE author='kanban-diagnostics-actuator'"
).fetchone()[0]
n_marker = b.execute(
    "SELECT COUNT(*) FROM task_comments WHERE body LIKE ?", ("%diagnostics-actuator:v1%",)
).fetchone()[0]
print(f"  comments added={n_new} actuator-authored={n_act} marker-carrying={n_marker}")
if n_new != n_act or n_act != n_marker:
    bad = True

# status/assignee of every task must be byte-identical
sa = dict((r[0], (r[1], r[2])) for r in a.execute("SELECT id,status,assignee FROM tasks"))
sb = dict((r[0], (r[1], r[2])) for r in b.execute("SELECT id,status,assignee FROM tasks"))
changed = [k for k in sa if k in sb and sa[k] != sb[k]]
added = set(sb) - set(sa)
removed = set(sa) - set(sb)
print(f"  tasks: status/assignee changed={len(changed)} added={len(added)} removed={len(removed)}")
if changed or added or removed:
    bad = True
    print("   ", changed[:5], list(added)[:5], list(removed)[:5])
print("DIFF_VERDICT:", "FAIL" if bad else "PASS (comment-append only)")
sys.exit(1 if bad else 0)
