#!/usr/bin/env python3
"""Post-deploy Session Bus seat proof (t_fab72502, PR #109).

For the first N kanban runs started at or after a deploy epoch (across every
board), check that the Session Bus has a seat for ``hermes-<profile>-<task_id>``
enrolled within [run start - 5s, run start + window].

Read-only: opens bus.db and every board kanban.db with mode=ro.

Usage:
    post_deploy_seat_check.py <deploy_epoch> [N=10] [window_s=120]

Exit codes: 0 = N/N seated; 1 = fewer than N seated; 2 = no runs since epoch.

Known limit: the seat table is upserted by session name, so a later run of the
same card can overwrite the enrolled_at of an earlier one. A NO-SEAT line
therefore means "no enrolment row in the window at check time", not proof the
enrol path failed. Read the runs' task_events before concluding.

Paths can be overridden with SEAT_CHECK_BUS_DB and SEAT_CHECK_BOARDS_GLOB.
"""
import datetime
import glob
import os
import sqlite3
import sys

DEPLOY = int(sys.argv[1]) if len(sys.argv) > 1 else 0
N = int(sys.argv[2]) if len(sys.argv) > 2 else 10
WINDOW = int(sys.argv[3]) if len(sys.argv) > 3 else 120
BUS = os.environ.get("SEAT_CHECK_BUS_DB", "/home/frank/.hermes/session-bus/bus.db")
BOARDS = os.environ.get(
    "SEAT_CHECK_BOARDS_GLOB", "/home/frank/.hermes/kanban/boards/*/kanban.db"
)


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def ts(x):
    return datetime.datetime.fromtimestamp(int(x), datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


runs = []
for db in glob.glob(BOARDS):
    try:
        kc = ro(db)
        for rid, task, prof, started in kc.execute(
            "select id, task_id, profile, started_at from task_runs "
            "where started_at >= ? and started_at is not null order by started_at",
            (DEPLOY,),
        ):
            runs.append((int(started), db.split(os.sep)[-2], rid, task, prof))
    except Exception as e:  # board DB missing or schema drift: report, don't hide
        print("SKIP", db, e)

runs.sort()
runs = runs[:N]
if not runs:
    print(f"NO RUNS since deploy epoch {DEPLOY} ({ts(DEPLOY) if DEPLOY else 'n/a'})")
    sys.exit(2)

bus = ro(BUS)
ok = 0
for started, board, rid, task, prof in runs:
    row = bus.execute(
        "select enrolled_at from seat where session = ? and enrolled_at between ? and ? "
        "order by enrolled_at limit 1",
        (f"hermes-{prof}-{task}", started - 5, started + WINDOW),
    ).fetchone()
    status = "SEATED" if row else "NO-SEAT"
    ok += bool(row)
    print(f"{status:8} {board}/{task} run={rid} profile={prof} started={ts(started)}")

print(f"RESULT {ok}/{len(runs)} seated (need {N}/{N})")
sys.exit(0 if ok == len(runs) == N else 1)
