#!/usr/bin/env python3
"""Selftest for kanban_diagnostics_actuator.py — hermetic, no live boards."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

SCRIPT = Path(__file__).with_name("kanban_diagnostics_actuator.py")
spec = importlib.util.spec_from_file_location("act", SCRIPT)
act = importlib.util.module_from_spec(spec)
spec.loader.exec_module(act)  # type: ignore[union-attr]

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {extra}")
        FAILS.append(name)


def make_board(root: Path, board: str, task_ids: list[str]) -> Path:
    d = root / board
    d.mkdir(parents=True, exist_ok=True)
    db = d / "kanban.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT)")
    con.execute("CREATE TABLE task_comments(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " task_id TEXT, author TEXT, body TEXT, created_at INTEGER)")
    for t in task_ids:
        con.execute("INSERT INTO tasks VALUES(?,?,?,?)", (t, "t", "ready", "devops"))
    con.commit()
    con.close()
    return db


def diag(kind, data, severity="warning"):
    return {"kind": kind, "severity": severity, "title": "", "detail": "",
            "actions": [], "count": 1, "data": data}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="actsel-"))
    act.HERMES_HOME = tmp
    act.BOARDS_DIR = tmp / "kanban" / "boards"
    boards = act.BOARDS_DIR
    make_board(boards, "alpha", ["t_aaa1", "t_aaa2", "t_aaa3", "t_aaa4"])
    make_board(boards, "beta", ["t_bbb1"])
    (tmp / "profiles" / "devops").mkdir(parents=True)
    (tmp / "profiles" / "jarvis-os-pm").mkdir(parents=True)
    act.PM_PROFILE_BY_BOARD = {"alpha": "jarvis-os-pm"}
    profiles = act.known_profiles()

    print("== fleet index")
    idx = act.fleet_task_index(boards)
    check("indexes both boards", idx["t_aaa1"] == {"alpha"} and idx["t_bbb1"] == {"beta"},
          str(dict(idx)))

    now_h = 3600.0
    fixture = [
        # stranded, too young -> no plan
        {"task_id": "t_aaa1", "status": "ready", "assignee": "devops",
         "diagnostics": [diag("stranded_in_ready",
                              {"age_seconds": 2 * now_h, "assignee": "devops"}, "warning")]},
        # stranded, old, unknown profile -> plan/unknown_profile
        {"task_id": "t_aaa2", "status": "ready", "assignee": "ghost",
         "diagnostics": [diag("stranded_in_ready",
                              {"age_seconds": 20 * now_h, "assignee": "ghost"}, "critical")]},
        # stranded, old, external lane
        {"task_id": "t_aaa3", "status": "ready", "assignee": "external-x",
         "diagnostics": [diag("stranded_in_ready",
                              {"age_seconds": 20 * now_h, "assignee": "external-x"}, "critical")]},
        # phantom refs: one cross-board (t_bbb1), one branch-name, one truly missing
        {"task_id": "t_aaa4", "status": "done", "assignee": "devops",
         "diagnostics": [diag("prose_phantom_refs",
                              {"phantom_refs": ["t_bbb1", "wt/t_aaa1", "t_nope"]})]},
        # inversion: source task id present -> plan comment+pm_route
        {"task_id": "t_aaa4", "status": "running", "assignee": "devops",
         "diagnostics": [diag("review_lane_dependency_inversion",
                              {"source_task_id": "t_bbb1", "source_status": "blocked"})]},
        # inversion: source task id MISSING -> trigger not satisfied, no plan
        {"task_id": "t_aaa3", "status": "running", "assignee": "devops",
         "diagnostics": [diag("review_lane_dependency_inversion",
                              {"source_status": "blocked"})]},
        # blocked 10h -> below 48h threshold, no plan
        {"task_id": "t_aaa1", "status": "blocked", "assignee": "devops",
         "diagnostics": [diag("stuck_in_blocked", {"age_hours": 10})]},
        # unowned class must be counted, never planned
        {"task_id": "t_aaa2", "status": "blocked", "assignee": "devops",
         "diagnostics": [diag("repeated_crashes", {"consecutive": 9}, "critical")]},
    ]

    plans, metrics = act.build_plans("alpha", fleet_index=idx, profiles=profiles,
                                     diagnostics=fixture)
    by_sub = metrics["by_subclass_planned"]
    print("== planning", json.dumps(metrics, sort_keys=True))
    check("young stranded suppressed", "stranded_in_ready/profile_not_polling" not in by_sub, str(by_sub))
    check("unknown profile classified", by_sub.get("stranded_in_ready/unknown_profile") == 1, str(by_sub))
    check("external lane classified", by_sub.get("stranded_in_ready/external_lane") == 1, str(by_sub))
    check("young block suppressed", "stuck_in_blocked/stale_block" not in by_sub, str(by_sub))
    check("unowned class not planned", metrics["unowned_classes"] == {"repeated_crashes": 1},
          str(metrics["unowned_classes"]))
    check("cross-board refs suppressed", metrics.get("phantom_cross_board_suppressed") == 1,
          str(metrics))
    phantom = [p for p in plans if p["cls"] == "prose_phantom_refs"]
    check("only unresolvable ref routed",
          len(phantom) == 1 and phantom[0]["refs"] == ["t_nope"], str(phantom))
    check("phantom refs action is comment+pm_route (matrix §2.3)",
          len(phantom) == 1 and phantom[0]["action"] == "comment+pm_route", str(phantom))
    check("branch-name ref ignored",
          all("wt/t_aaa1" not in p.get("refs", []) for p in plans), str(plans))
    inv = [p for p in plans if p["cls"] == "review_lane_dependency_inversion"]
    check("inversion with source id planned as comment+pm_route",
          len(inv) == 1 and inv[0]["source_task_id"] == "t_bbb1"
          and inv[0]["action"] == "comment+pm_route", str(inv))
    check("inversion without source id suppressed",
          metrics["by_subclass_planned"].get(
              "review_lane_dependency_inversion/inverted_parent_edge") == 1,
          str(metrics["by_subclass_planned"]))

    print("== old block routed")
    fx2 = [{"task_id": "t_aaa1", "status": "blocked", "assignee": "devops",
            "diagnostics": [diag("stuck_in_blocked", {"age_hours": 118})]}]
    p2, m2 = act.build_plans("alpha", fleet_index=idx, profiles=profiles, diagnostics=fx2)
    check("118h block routed", m2["by_subclass_planned"].get("stuck_in_blocked/stale_block") == 1,
          str(m2))
    check("blocked plan is pm_route_only (no self-silencing comment)",
          p2 and p2[0]["action"] == "pm_route_only", str(p2))
    r0 = act.apply_plans("alpha", p2)
    con = sqlite3.connect(act.board_db("alpha"))
    nb = con.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0]
    con.close()
    check("stuck_in_blocked writes NO comment", nb == 0 and r0.get("comment-added", 0) == 0,
          f"{nb} {r0}")
    check("blocked plan still reaches pm card",
          act.maybe_create_pm_card("alpha", p2, apply=False).startswith("DRY_RUN"), "")
    check("board with no PM mapping never creates a card",
          act.maybe_create_pm_card("beta", p2, apply=False) == "", "")

    print("== dry-run writes nothing")
    db = act.board_db("alpha")
    con = sqlite3.connect(db)
    before = con.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0]
    con.close()
    check("no comments before apply", before == 0, str(before))

    print("== apply is idempotent")
    r1 = act.apply_plans("alpha", plans)
    r2 = act.apply_plans("alpha", plans)
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0]
    bodies = [r[0] for r in con.execute("SELECT body FROM task_comments")]
    statuses = [r[0] for r in con.execute("SELECT status FROM tasks")]
    con.close()
    check("first apply wrote all plans", r1.get("comment-added") == len(plans), str(r1))
    check("second apply wrote nothing", r2.get("comment-added", 0) == 0, str(r2))
    check("comment count == plan count", n == len(plans), f"{n} vs {len(plans)}")
    check("every comment carries marker",
          all(act.MARKER_PREFIX in b for b in bodies), str(bodies[:1]))
    check("no status mutation", set(statuses) == {"ready"}, str(statuses))

    print("== pm card is dry-run safe")
    out = act.maybe_create_pm_card("alpha", plans, apply=False)
    check("pm card dry-run only prints", out == "" or out.startswith("DRY_RUN"), out)

    print("== dry-run smoke test (full main, seeded sample, zero mutations)")
    smoke_tmp = Path(tempfile.mkdtemp(prefix="actsmoke-"))
    act.HERMES_HOME = smoke_tmp
    act.BOARDS_DIR = smoke_tmp / "kanban" / "boards"
    sb = act.BOARDS_DIR / "alpha" / "kanban.db"
    sb.parent.mkdir(parents=True)
    con = sqlite3.connect(sb)
    con.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT)")
    con.execute("CREATE TABLE task_comments(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " task_id TEXT, author TEXT, body TEXT, created_at INTEGER)")
    con.execute("CREATE TABLE task_events(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER)")
    con.execute("INSERT INTO tasks VALUES(?,?,?,?)",
                ("t_smoke1", "t", "ready", "ghost"))
    con.commit()
    pre_comments = con.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0]
    pre_events = con.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    pre_tasks = con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    con.close()

    real_run_diagnostics = act.run_diagnostics
    smoke_fixture = [
        {"task_id": "t_smoke1", "status": "ready", "assignee": "ghost",
         "diagnostics": [diag("stranded_in_ready",
                              {"age_seconds": 20 * 3600, "assignee": "ghost"},
                              "critical")]},
        {"task_id": "t_smoke1", "status": "blocked", "assignee": "devops",
         "diagnostics": [diag("stuck_in_blocked", {"age_hours": 118})]},
        {"task_id": "t_smoke1", "status": "done", "assignee": "devops",
         "diagnostics": [diag("prose_phantom_refs",
                              {"phantom_refs": ["t_nope2"]})]},
    ]
    act.run_diagnostics = lambda board, timeout=300: smoke_fixture
    smoke_log = smoke_tmp / "actuator.jsonl"
    try:
        rc = act.main(["--boards", "alpha", "--json", "--log", str(smoke_log)])
    finally:
        act.run_diagnostics = real_run_diagnostics
    check("smoke main dry-run rc=0", rc == 0, str(rc))
    con = sqlite3.connect(sb)
    n_comments = con.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0]
    n_events = con.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    n_tasks = con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    con.close()
    check("smoke dry-run wrote ZERO comments",
          n_comments == pre_comments == 0, f"{n_comments} vs {pre_comments}")
    check("smoke dry-run wrote ZERO events",
          n_events == pre_events == 0, f"{n_events} vs {pre_events}")
    check("smoke dry-run touched ZERO task rows",
          n_tasks == pre_tasks == 1, f"{n_tasks} vs {pre_tasks}")
    lines = smoke_log.read_text().strip().splitlines()
    check("smoke dry-run wrote log line", len(lines) >= 1, str(len(lines)))
    if lines:
        rec = json.loads(lines[-1])
        check("smoke log mode=dry-run", rec.get("mode") == "dry-run", str(rec.get("mode")))
        acts = rec.get("actions") or []
        check("smoke log has per-action entries with id/class/board/action/ts",
              len(acts) == 3 and all(
                  {"task_id", "class", "board", "action", "ts"} <= set(a)
                  for a in acts) and
              {a["action"] for a in acts} == {"comment+pm_route", "pm_route_only"},
              json.dumps(acts, sort_keys=True))

    print()
    if FAILS:
        print(f"SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
