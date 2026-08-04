#!/usr/bin/env python3
"""Kanban diagnostics ACTUATOR — the missing consumer for board diagnostics.

Gap filled (t_9e4789df): `hermes kanban diagnostics` emits hundreds of
diagnostics per board and the only consumer (kanban_classify_failure_recent.py)
is a REPORTER that truncates to the top-10 per board and never mutates
anything. Whole classes -- prose_phantom_refs, stranded_in_ready,
stuck_in_blocked, review_lane_dependency_inversion -- have literally zero
consumers.

DESIGN (verdict-router lessons from t_65a0c080 applied):

* Dry-run is the DEFAULT. `--apply` is required to mutate anything.
* The ONLY mutation this actuator ever performs is appending an idempotent
  routing comment, plus (optionally, --allow-card) creating ONE bounded
  per-board-per-day PM triage card. It never reassigns, unblocks, changes
  status, changes block_kind, archives, or deletes.
* Every comment carries a durable marker (MARKER) and is written at most once
  per (task, class) -- re-runs are no-ops.
* No acting on quoted/narrated text: classification is driven by the
  STRUCTURED `data` payload of each diagnostic (age_seconds, assignee,
  phantom_refs, ...), never by regex over prose.
* Cross-board resolution: prose_phantom_refs is 93% false-positive because the
  diagnostic resolves ids against ONE board's db. This actuator builds a
  fleet-wide id index first and only routes refs that resolve NOWHERE. Refs
  that exist on another board are counted and suppressed as `cross_board`.
* Anything needing judgment (why is this blocked, should this be reassigned)
  becomes a comment/card for a human or PM -- never an automatic mutation.

Empty stdout when there is nothing to say (watchdog convention).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _resolve_fleet_home() -> Path:
    """Locate the FLEET hermes root (the one that owns kanban/boards).

    HERMES_HOME is per-profile when a worker runs under a profile
    (e.g. /home/frank/.hermes/profiles/devops), and boards live only at the
    fleet root. Walk up out of any profiles/<name> suffix, then fall back to
    the conventional root.
    """
    env = os.environ.get("HERMES_HOME")
    candidates: list[Path] = []
    if env:
        p = Path(env)
        candidates.append(p)
        # .../.hermes/profiles/<name> -> .../.hermes
        if p.parent.name == "profiles":
            candidates.append(p.parent.parent)
    candidates.append(Path("/home/frank/.hermes"))
    for cand in candidates:
        if (cand / "kanban" / "boards").is_dir():
            return cand
    return candidates[0]


HERMES_HOME = _resolve_fleet_home()
BOARDS_DIR = HERMES_HOME / "kanban" / "boards"

MARKER_PREFIX = "diagnostics-actuator:v1"
AUTHOR = "kanban-diagnostics-actuator"

DEFAULT_BOARDS = ("jarvis-os", "sycode-trading")

# Per-class thresholds. Deliberately conservative: only act once the class is
# unambiguously stale, so the actuator never races a live worker.
STRANDED_MIN_HOURS = 6.0
BLOCKED_MIN_HOURS = 48.0

# Classes this actuator owns. Everything else is counted and reported but
# left strictly alone (repeated_failures / repeated_crashes already have
# dispatcher-side owners; double-actuating them causes respawn storms).
OWNED_CLASSES = (
    "stranded_in_ready",
    "stuck_in_blocked",
    "prose_phantom_refs",
    "review_lane_dependency_inversion",
)

PM_PROFILE_BY_BOARD = {
    "jarvis-os": "jarvis-os-pm",
    "sycode-trading": "sycode-trading-pm",
    "sycode-ai": "sycode-ai-pm",
    "upero": "upero-pm",
    "yorkstone-supplies": "yorkstone-supplies-pm",
}


# ---------------------------------------------------------------- board io


def board_db(board: str) -> Path:
    return BOARDS_DIR / board / "kanban.db"


def connect_ro(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def connect_rw(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db), timeout=30)
    con.row_factory = sqlite3.Row
    return con


def fleet_task_index(boards_dir: Path = BOARDS_DIR) -> dict[str, set[str]]:
    """Map task_id -> set of boards it exists on, across EVERY board db.

    This is what makes prose_phantom_refs actionable: the per-board diagnostic
    cannot see cross-board ids, so 93% of its hits are benign cross-references.
    """
    index: dict[str, set[str]] = defaultdict(set)
    if not boards_dir.exists():
        return index
    for entry in sorted(boards_dir.iterdir()):
        db = entry / "kanban.db"
        if not entry.is_dir() or not db.exists():
            continue
        try:
            con = connect_ro(db)
        except sqlite3.Error:
            continue
        try:
            for row in con.execute("SELECT id FROM tasks"):
                index[str(row[0])].add(entry.name)
        except sqlite3.Error:
            pass
        finally:
            con.close()
    return index


def run_diagnostics(board: str, timeout: int = 300) -> list[dict[str, Any]]:
    """Invoke the native diagnostics CLI for one board.

    HERMES_KANBAN_DB / HERMES_KANBAN_BOARD in the ambient env OVERRIDE --board
    (this bit a survey during development: both boards returned identical
    counts). Strip them so --board is authoritative.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD")}
    env["HERMES_HOME"] = str(HERMES_HOME)
    proc = subprocess.run(
        ["hermes", "kanban", "--board", board, "diagnostics", "--json"],
        capture_output=True, text=True, timeout=timeout, env=env, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"diagnostics failed board={board} rc={proc.returncode}: "
            f"{(proc.stderr or proc.stdout)[:300]}"
        )
    data = json.loads(proc.stdout)
    if isinstance(data, dict):
        data = data.get("diagnostics", data.get("results", []))
    return list(data)


# ---------------------------------------------------------------- planning


def _marker(cls: str, task_id: str) -> str:
    return f"{MARKER_PREFIX}:{cls}:{task_id}"


def plan_stranded_in_ready(task: dict, diag: dict, ctx: dict) -> dict | None:
    data = diag.get("data") or {}
    age_h = float(data.get("age_seconds", 0)) / 3600.0
    if age_h < STRANDED_MIN_HOURS:
        return None
    assignee = str(data.get("assignee") or "")
    # Matrix trigger requires a non-empty assignee: with no assignee there is
    # no structurally classifiable cause, so never plan.
    if not assignee:
        return None
    # Structural sub-classification, no prose parsing.
    known = ctx["known_profiles"]
    if assignee.startswith("external-"):
        sub, note = "external_lane", (
            "Assignee is an external (non-Hermes) worker lane; the Hermes "
            "dispatcher will never claim it. Either the external runner is "
            "down or the lane is complete and the card should be closed."
        )
    elif assignee not in known:
        sub, note = "unknown_profile", (
            f"Assignee {assignee!r} does not match any installed Hermes "
            "profile, so the dispatcher silently skips this card forever. "
            "Fix the assignee or retire the card."
        )
    else:
        sub, note = "profile_not_polling", (
            f"Assignee {assignee!r} is a real profile but nothing has claimed "
            "this card. Check that its gateway is running and the dispatcher "
            "is pointed at this board."
        )
    return {
        "cls": "stranded_in_ready",
        "sub": sub,
        "task_id": task["task_id"],
        "board": ctx["board"],
        "assignee": assignee,
        "age_hours": round(age_h, 1),
        "action": "comment+pm_route",
        "note": note,
    }


def plan_stuck_in_blocked(task: dict, diag: dict, ctx: dict) -> dict | None:
    data = diag.get("data") or {}
    age_h = float(data.get("age_hours", 0))
    if age_h < BLOCKED_MIN_HOURS:
        return None
    return {
        "cls": "stuck_in_blocked",
        "sub": "stale_block",
        "task_id": task["task_id"],
        "board": ctx["board"],
        "assignee": task.get("assignee") or "",
        "age_hours": round(age_h, 1),
        # NOT "comment+pm_route": _rule_stuck_in_blocked clears on ANY comment
        # after the block event, so an actuator comment would silence its own
        # signal and fake the week-over-week acceptance metric. This class is
        # routed to the PM card only -- the card is the durable record.
        "action": "pm_route_only",
        "note": (
            f"Blocked for {age_h:.0f}h with no comment or unblock since. "
            "Surfacing to the PM queue: read the block reason and either "
            "answer it, unblock with delegated evidence, or retire the card. "
            "This actuator does NOT unblock -- unblocking requires judgment."
        ),
    }


def plan_prose_phantom_refs(task: dict, diag: dict, ctx: dict) -> dict | None:
    data = diag.get("data") or {}
    refs = [str(r) for r in (data.get("phantom_refs") or [])]
    index = ctx["fleet_index"]
    cross: list[str] = []
    truly_missing: list[str] = []
    for ref in refs:
        if not ref.startswith("t_"):
            # e.g. "fix/t_66a9d5c0", "wt/t_ffd2508c" -- branch names the
            # extractor mis-parsed, not card references at all.
            continue
        if index.get(ref):
            cross.append(ref)
        else:
            truly_missing.append(ref)
    ctx["counters"]["phantom_cross_board_suppressed"] += len(cross)
    if not truly_missing:
        return None
    return {
        "cls": "prose_phantom_refs",
        "sub": "unresolvable_ref",
        "task_id": task["task_id"],
        "board": ctx["board"],
        "assignee": task.get("assignee") or "",
        "refs": truly_missing,
        "cross_board_refs": cross,
        # Matrix §2.3: comment + pm_route — the PM triage card is the durable
        # record that a human investigates AND resolves the reference.
        "action": "comment+pm_route",
        "note": (
            "Completion summary cites task ids that resolve on NO board in "
            f"the fleet: {', '.join(truly_missing)}. "
            + (f"({len(cross)} further refs do resolve on other boards and "
               "are benign cross-references.) " if cross else "")
            + "The completion stands; this comment exists so downstream "
            "consumers parsing the summary do not chase cards that never "
            "existed."
        ),
    }


def plan_review_lane_dependency_inversion(task: dict, diag: dict, ctx: dict) -> dict | None:
    data = diag.get("data") or {}
    # Matrix §2.4 trigger: data.source_task_id must be present. Without it the
    # routing comment would cite a source it cannot name — never plan.
    if not data.get("source_task_id"):
        return None
    return {
        "cls": "review_lane_dependency_inversion",
        "sub": "inverted_parent_edge",
        "task_id": task["task_id"],
        "board": ctx["board"],
        "assignee": task.get("assignee") or "",
        "source_task_id": data.get("source_task_id"),
        "source_status": data.get("source_status"),
        "action": "comment+pm_route",
        "note": (
            f"Reviewer lane is parented to the blocked source it must review "
            f"({data.get('source_task_id')}, status={data.get('source_status')}). "
            "The dependency gate is correct; the graph shape is inverted, so "
            "this card can never run. Repair requires deciding whether a "
            "duplicate review already landed -- that is a judgment call, so "
            "it is routed, not auto-applied."
        ),
    }


PLANNERS = {
    "stranded_in_ready": plan_stranded_in_ready,
    "stuck_in_blocked": plan_stuck_in_blocked,
    "prose_phantom_refs": plan_prose_phantom_refs,
    "review_lane_dependency_inversion": plan_review_lane_dependency_inversion,
}


def known_profiles() -> set[str]:
    root = HERMES_HOME / "profiles"
    if not root.exists():
        return set()
    return {p.name for p in root.iterdir() if p.is_dir()}


def build_plans(board: str, *, fleet_index: dict[str, set[str]],
                profiles: set[str], limit: int | None = None,
                diagnostics: list[dict] | None = None) -> tuple[list[dict], dict]:
    diags = run_diagnostics(board) if diagnostics is None else diagnostics
    ctx = {
        "board": board,
        "fleet_index": fleet_index,
        "known_profiles": profiles,
        "counters": Counter(),
    }
    plans: list[dict] = []
    seen = Counter()
    unowned = Counter()
    for task in diags:
        for diag in task.get("diagnostics", []):
            cls = diag.get("kind", "")
            seen[cls] += 1
            planner = PLANNERS.get(cls)
            if planner is None:
                unowned[cls] += 1
                continue
            plan = planner(task, diag, ctx)
            if plan is not None:
                plans.append(plan)
                if limit is not None and len(plans) >= limit:
                    break
        if limit is not None and len(plans) >= limit:
            break
    metrics = {
        "board": board,
        "diagnostics_seen": sum(seen.values()),
        "by_class_seen": dict(seen),
        "unowned_classes": dict(unowned),
        "plans": len(plans),
        "by_class_planned": dict(Counter(p["cls"] for p in plans)),
        "by_subclass_planned": dict(Counter(f"{p['cls']}/{p['sub']}" for p in plans)),
        **dict(ctx["counters"]),
    }
    return plans, metrics


# ------------------------------------------------------------------ apply


def comment_body(plan: dict) -> str:
    return (
        f"[{_marker(plan['cls'], plan['task_id'])}] "
        f"class={plan['cls']} sub={plan['sub']} action={plan['action']}\n\n"
        f"{plan['note']}\n\n"
        "Emitted by kanban_diagnostics_actuator.py (comment-only: no status, "
        "assignee, block_kind, or graph mutation)."
    )


def already_marked(con: sqlite3.Connection, task_id: str, cls: str) -> bool:
    try:
        row = con.execute(
            "SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE ? LIMIT 1",
            (task_id, f"%{_marker(cls, task_id)}%"),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def apply_plans(board: str, plans: list[dict]) -> dict[str, int]:
    counts: Counter = Counter()
    # Only plans whose action includes a comment ever touch the db. Plans with
    # action="pm_route_only" are deliberately comment-free (see
    # plan_stuck_in_blocked) and are carried by the PM card instead.
    commentable = [p for p in plans if "comment" in p.get("action", "")]
    counts["skipped-no-comment-action"] = len(plans) - len(commentable)
    if not commentable:
        return {k: v for k, v in counts.items() if v}
    db = board_db(board)
    now = int(time.time())
    con = connect_rw(db)
    try:
        for plan in commentable:
            tid = plan["task_id"]
            if already_marked(con, tid, plan["cls"]):
                counts["already-present"] += 1
                continue
            con.execute(
                "INSERT INTO task_comments(task_id, author, body, created_at) "
                "VALUES (?,?,?,?)",
                (tid, AUTHOR, comment_body(plan), now),
            )
            counts["comment-added"] += 1
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return dict(counts)


def maybe_create_pm_card(board: str, plans: list[dict], *, apply: bool) -> str:
    """One bounded PM card per board per day summarising routed classes."""
    routed = [p for p in plans if p["action"] in ("comment+pm_route", "pm_route_only")]
    if not routed:
        return ""
    pm = PM_PROFILE_BY_BOARD.get(board)
    if not pm:
        return ""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"diagnostics-actuator:{board}:{today}"
    by_class = Counter(f"{p['cls']}/{p['sub']}" for p in routed)
    title = f"DIAGNOSTICS TRIAGE: {board} routed lifecycle diagnostics {today}"
    lines = [
        f"Automated consumer for `hermes kanban diagnostics` on `{board}`.",
        "Source: kanban_diagnostics_actuator.py (gap card t_9e4789df).",
        "",
        f"{len(routed)} diagnostics routed to this PM queue:",
    ]
    for k, v in by_class.most_common():
        lines.append(f"- {k}: {v}")
    lines += [
        "",
        "Each routed card already carries an idempotent actuator comment "
        "explaining the class and why it needs a human/PM decision.",
        "",
        "Acceptance:",
        "1. Work the routed classes in age order; the actuator never mutates "
        "status/assignee/graph, so every one of these still needs a decision.",
        "2. stranded_in_ready/unknown_profile and /external_lane are usually "
        "retire-or-reassign; stuck_in_blocked is answer-or-retire.",
        "3. Preserve hard gates: no credentials, live trading, prod deploys, "
        "irreversible data ops, or new spend.",
    ]
    body = "\n".join(lines)
    cmd = [
        "hermes", "kanban", "--board", board, "create", title,
        "--assignee", pm, "--priority", "70",
        "--idempotency-key", key,
        "--created-by", "kanban-diagnostics-actuator",
        "--body", body, "--json",
    ]
    if not apply:
        return f"DRY_RUN would create pm card board={board} key={key} routed={len(routed)}"
    env = {k: v for k, v in os.environ.items()
           if k not in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD")}
    env["HERMES_HOME"] = str(HERMES_HOME)
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                        env=env, check=False)
    if cp.returncode != 0:
        raise RuntimeError(f"pm card create failed board={board}: "
                           f"{(cp.stderr or cp.stdout)[:300]}")
    try:
        tid = json.loads(cp.stdout).get("id", cp.stdout.strip())
    except Exception:
        tid = cp.stdout.strip()[:200]
    return f"PM_CARD board={board} task={tid} key={key} routed={len(routed)}"


# ------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--boards", nargs="*", default=list(DEFAULT_BOARDS))
    ap.add_argument("--apply", action="store_true",
                    help="write idempotent routing comments (default: dry-run)")
    ap.add_argument("--allow-card", action="store_true",
                    help="also create the bounded per-board-per-day PM triage card")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap plans per board (canary runs)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--log", type=Path,
                    default=HERMES_HOME / "var" / "log" / "kanban-diagnostics-actuator.jsonl",
                    help="append one JSON line per run; '' to disable")
    args = ap.parse_args(argv)

    fleet_index = fleet_task_index()
    profiles = known_profiles()
    all_metrics: list[dict] = []
    all_plans: list[dict] = []
    applied: dict[str, Any] = {}
    cards: list[str] = []
    errors: list[str] = []

    for board in args.boards:
        if not board_db(board).exists():
            errors.append(f"board db missing: {board}")
            continue
        try:
            plans, metrics = build_plans(
                board, fleet_index=fleet_index, profiles=profiles, limit=args.limit)
        except Exception as exc:
            errors.append(f"{board}: {exc}")
            continue
        all_metrics.append(metrics)
        all_plans.extend(plans)
        if args.apply:
            applied[board] = apply_plans(board, plans)
        if args.allow_card:
            try:
                out = maybe_create_pm_card(board, plans, apply=args.apply)
                if out:
                    cards.append(out)
            except Exception as exc:
                errors.append(f"{board} pm-card: {exc}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "owned_classes": list(OWNED_CLASSES),
        "thresholds": {
            "stranded_min_hours": STRANDED_MIN_HOURS,
            "blocked_min_hours": BLOCKED_MIN_HOURS,
        },
        "metrics": all_metrics,
        "applied": applied,
        "pm_cards": cards,
        "errors": errors,
        "plans": all_plans,
    }

    if args.log and str(args.log):
        try:
            args.log.parent.mkdir(parents=True, exist_ok=True)
            slim = {k: v for k, v in payload.items() if k != "plans"}
            slim["plan_count"] = len(all_plans)
            # Per-action log requirement (task t_5d9221fd): every planned
            # action in both modes carries diagnostic id, class, board,
            # action, and timestamp, so the week-over-week metric can be
            # measured from this log (matrix A6).
            slim["actions"] = [
                {
                    "ts": payload["generated_at"],
                    "task_id": p["task_id"],
                    "class": p["cls"],
                    "board": p["board"],
                    "action": p["action"],
                    "sub": p.get("sub"),
                }
                for p in all_plans
            ]
            with args.log.open("a") as fh:
                fh.write(json.dumps(slim, sort_keys=True) + "\n")
        except OSError as exc:
            errors.append(f"log write failed: {exc}")

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if errors else 0

    # Watchdog convention: silent when there is nothing to say.
    if not all_plans and not errors:
        return 0
    head = (f"KANBAN_DIAGNOSTICS_ACTUATOR mode={payload['mode']} "
            f"boards={','.join(args.boards)} plans={len(all_plans)}")
    print(head)
    for m in all_metrics:
        print(f"  {m['board']}: seen={m['diagnostics_seen']} "
              f"planned={m['plans']} "
              f"by_sub={json.dumps(m['by_subclass_planned'], sort_keys=True)} "
              f"cross_board_suppressed={m.get('phantom_cross_board_suppressed', 0)} "
              f"unowned={json.dumps(m['unowned_classes'], sort_keys=True)}")
    if applied:
        print(f"  applied={json.dumps(applied, sort_keys=True)}")
    for c in cards:
        print(f"  {c}")
    for e in errors:
        print(f"  ERROR {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
