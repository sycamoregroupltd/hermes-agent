#!/usr/bin/env python3
"""validate_assignees.py — PM specify/decompose/auto-assign gate + board-row sweep.

Part 1 — PM decomposition/specify gate (original purpose, t_40d8eaca):
  Rejects any kanban card whose assignee is a NON-SPAWNABLE profile: a profile
  directory that exists on disk but which the dispatcher/PM routing layer must
  never auto-assign because it cannot actually run fleet work.

  This script is the ROOT FIX at the routing/assignment layer: a decomposition
  or specify output that hands cards to a non-spawnable profile is rejected
  with a clear error instead of silently producing stranded ready cards.

Part 2 — board-row sweep (added for t_97819b7d, child of t_c17d0998):
  Nothing ever validated rows ALREADY sitting on a board, which is exactly how
  five cards sat on phantom profile 'reviewer' for weeks with zero runs and zero
  operator signal (silent dispatcher drop). `--board-rows` scans EVERY board under
  ~/.hermes/kanban/boards/*/kanban.db and reports any DISPATCHABLE row (status in
  ready/todo/triage/scheduled) whose assignee is an UNROUTABLE phantom:
    - not a real profile directory, AND
    - not a recognized terminal lane, AND
    - has ZERO rows in task_runs (never actually ran = silent drop, not a live seat).
  It distinguishes PHANTOM (the real failure) from BENIGN (a named lane that has
  real run history, or a recognized terminal lane) so the daily sweep is NOT noisy.

NON-SPAWNABLE SET (authoritative source: /home/frank/uaa-rules/PROFILE-CATALOG.md):
  - workforce-scaler   — STUB, never booted, not dispatch-ready
  - nim-deepseek, nim-gemini3, nim-glm52, nim-qwen35 — NVIDIA NIM provider
    bench probes (souled 2026-07-05); not project workers.

  NOTE: this is SEPARATE from the dispatcher's `skipped_nonspawnable` (which
  covers assignees that are not a profile directory at all, e.g. 'worker',
  'dev', 'pm'). Non-spawnable profiles exist on disk but are explicitly
  reserved, so they must be named here. Update this constant when the catalog
  changes.

RECOGNIZED TERMINAL LANES (valid assignees in explicit multi-lane setups, never
part of the non-spawnable set, and NOT flagged by the board-row sweep):
  Exact:  fable, codex, grok          (Frank-activated terminal seats)
  Prefix: orion-*, codex-*, external-* (Claude Code / control-plane lanes,
           instance-suffixed Codex seats e.g. codex-root-orchestrator-019fbe09,
           and external-* lanes e.g. external-native-quarantine-hold /
           external-claude-ci-*-maker). These are intentional multi-lane setups;
           flagging them would be pure noise. The board-row sweep ALSO suppresses
           any assignee with >=1 task_runs row (proven-live seat) as a belt-and-
           braces rule, so even an unrecognised-but-real seat is never a false
           positive.

USAGE:
  # Validate a JSON decomposition/specify output (list of card dicts with
  # an 'assignee' key, or a single dict with 'assignee'):
  python3 validate_assignees.py --json decomposition.json
  cat decomposition.json | python3 validate_assignees.py --stdin

  # Validate an inline list of assignee strings:
  python3 validate_assignees.py --assignees workforce-scaler builder

  # Board-row sweep across ALL boards (t_97819b7d):
  python3 validate_assignees.py --board-rows            # human report
  python3 validate_assignees.py --board-rows --board-json   # machine report
  #   Exit code 0 = no phantom assignees; exit code 1 = >=1 phantom found.

CONTRACT: if PROFILE_CATALOG_PATH is readable, the script ALSO warns when a
named non-spawnable profile is absent from the on-disk catalog, so the
constant cannot silently drift from reality.
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys

PROFILE_CATALOG_PATH = os.environ.get(
    "PROFILE_CATALOG_PATH",
    "/home/frank/uaa-rules/PROFILE-CATALOG.md",
)

# Authoritative non-spawnable assignee set (see module docstring).
NON_SPAWNABLE = {
    "workforce-scaler",
    "nim-deepseek",
    "nim-gemini3",
    "nim-glm52",
    "nim-qwen35",
}

# Recognized terminal lanes: valid assignees in explicit multi-lane setups,
# never part of the non-spawnable set (mirrors nonspawnable-fleet-alert-guard).
# 'operator' = Frank-operator lane (registered jarvis-os/t_e08cdceb, 2026-08-02):
#   work only Frank (or a blessed operator seat) can do — an intentional lane,
#   not a phantom.
EXCLUDED_TERMINAL_LANES_EXACT = {"fable", "codex", "grok", "operator"}
EXCLUDED_TERMINAL_LANES_PREFIX = ("orion-", "codex-", "external-")

# Dispatchable statuses the board-row sweep inspects.
DISPATCHABLE_STATUSES = ("ready", "todo", "triage", "scheduled")

DEFAULT_BOARDS_DIR = os.environ.get(
    "HERMES_KANBAN_BOARDS", "/home/frank/.hermes/kanban/boards"
)
DEFAULT_PROFILES_ROOT = os.environ.get(
    "HERMES_PROFILES_ROOT", "/home/frank/.hermes/profiles"
)


def _normalize(assignee: str) -> str:
    return (assignee or "").strip().lower()


def is_non_spawnable(assignee: str) -> bool:
    """True if assignee is in the non-spawnable set (case-insensitive)."""
    return _normalize(assignee) in {p.lower() for p in NON_SPAWNABLE}


def is_recognized_terminal_lane(assignee: str) -> bool:
    al = _normalize(assignee)
    if not al:
        return False
    if al in EXCLUDED_TERMINAL_LANES_EXACT:
        return True
    return any(al.startswith(p) for p in EXCLUDED_TERMINAL_LANES_PREFIX)


def _catalog_pairs() -> set[str]:
    """Return the set of profile names listed in PROFILE-CATALOG.md (lowercased)."""
    out: set[str] = set()
    try:
        with open(PROFILE_CATALOG_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                # Catalog rows look like: | name | ...
                if not line.strip().startswith("|"):
                    continue
                parts = [p.strip() for p in line.strip().strip("|").split("|")]
                if not parts:
                    continue
                name = parts[0]
                if name and not name.startswith("-"):
                    out.add(name.lower())
    except FileNotFoundError:
        pass
    return out


def _catalog_drift_warnings() -> list[str]:
    """Return WARN lines for NON_SPAWNABLE names absent from a readable catalog.

    Contract (module docstring): if PROFILE_CATALOG_PATH is readable, warn when a
    named non-spawnable profile is absent from the on-disk catalog, so the
    NON_SPAWNABLE constant cannot silently drift from reality. If the catalog is
    NOT readable, emit nothing (drift cannot be assessed) — returning [] here
    avoids spuriously flagging every entry when the file is missing.
    """
    if not os.access(PROFILE_CATALOG_PATH, os.R_OK):
        return []
    catalog = _catalog_pairs()
    if not catalog:
        # File readable but no profile rows parsed — treat as unassessable
        # rather than flagging every entry as drifted.
        return []
    warnings: list[str] = []
    for name in sorted(NON_SPAWNABLE):
        if name.lower() not in catalog:
            warnings.append(
                f"validate_assignees: WARN — non-spawnable profile '{name}' is "
                f"absent from {PROFILE_CATALOG_PATH}; NON_SPAWNABLE constant may "
                "have drifted from the catalog (verify and update)."
            )
    return warnings


def validate_assignees(assignees: list[str]) -> tuple[list[str], list[str]]:
    """Return (rejected, terminal_lane_skipped) for a list of assignee strings.

    - rejected: non-spawnable profiles that must never be auto-assigned.
    - terminal_lane_skipped: recognized terminal lanes (valid, not rejected).
    """
    rejected: list[str] = []
    terminal: list[str] = []
    seen = set()
    for a in assignees:
        if a is None:
            continue
        n = _normalize(a)
        if not n or n in seen:
            seen.add(n)
            continue
        seen.add(n)
        if is_non_spawnable(a):
            rejected.append(a)
        elif is_recognized_terminal_lane(a):
            terminal.append(a)
    return rejected, terminal


def _extract_assignees(obj) -> list[str]:
    """Pull assignee strings from a decomposition/specify structure."""
    out: list[str] = []
    if isinstance(obj, dict):
        if "assignee" in obj and obj["assignee"] is not None:
            out.append(obj["assignee"])
        for v in obj.values():
            out.extend(_extract_assignees(v))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(_extract_assignees(item))
    return out


# ---------------------------------------------------------------------------
# Part 2 — board-row sweep (t_97819b7d)
# ---------------------------------------------------------------------------


def _board_db_paths(boards_dir: str = DEFAULT_BOARDS_DIR) -> list[str]:
    """All <boards_dir>/<slug>/kanban.db files (glob, sorted)."""
    return sorted(glob.glob(os.path.join(boards_dir, "*", "kanban.db")))


def _classify_assignee(assignee, profiles_root: str, runs_set: set[str]) -> tuple[str, str]:
    """Classify a dispatchable row's assignee.

    Returns (category, reason) where category is one of:
      'profile'       — real profile directory (dispatchable, not an offender)
      'terminal_lane' — recognized terminal lane (benign, expected)
      'proven_live'   — has >=1 task_runs row (a real, running seat; benign)
      'phantom'       — no profile, no recognized lane, no run history
                        (the silent-drop failure the sweep exists to catch)
    """
    if assignee is None:
        return ("unassigned", "null-assignee")
    a = _normalize(assignee)
    if not a:
        return ("unassigned", "empty-assignee")
    if os.path.isdir(os.path.join(profiles_root, a)):
        return ("profile", "real-profile-dir")
    if is_recognized_terminal_lane(assignee):
        return ("terminal_lane", "recognized-terminal-lane")
    if assignee in runs_set:
        return ("proven_live", "has-task-runs-history")
    return ("phantom", "no-profile-no-lane-no-runs")


def scan_board_rows(
    boards_dir: str = DEFAULT_BOARDS_DIR,
    profiles_root: str = DEFAULT_PROFILES_ROOT,
) -> tuple[list[dict], dict]:
    """Scan every board for unroutable PHANTOM dispatchable rows.

    Returns (phantoms, summary) where phantoms is a list of finding dicts
    (board, id, assignee, status, priority, category, reason) and summary holds
    counts + any per-board scan errors.
    """
    phantoms: list[dict] = []
    total_dispatchable = 0
    benign = 0
    unassigned = 0
    errors: list[dict] = []
    boards = _board_db_paths(boards_dir)
    placeholders = ",".join("?" for _ in DISPATCHABLE_STATUSES)
    for db in boards:
        slug = os.path.basename(os.path.dirname(db))
        con = None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            # Precompute the set of assignees that have ever run on this board.
            runs_set: set[str] = set()
            try:
                for r in cur.execute("SELECT DISTINCT profile FROM task_runs"):
                    if r["profile"] is not None:
                        runs_set.add(r["profile"])
            except sqlite3.Error:
                # Board with no task_runs table — treat as empty run history.
                pass
            rows = cur.execute(
                f"SELECT id, assignee, status, priority FROM tasks "
                f"WHERE status IN ({placeholders})",
                DISPATCHABLE_STATUSES,
            ).fetchall()
        except (sqlite3.Error, OSError) as ex:
            errors.append({"board": slug, "error": str(ex)})
            continue
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
        for r in rows:
            total_dispatchable += 1
            cat, reason = _classify_assignee(r["assignee"], profiles_root, runs_set)
            if cat in ("profile", "terminal_lane", "proven_live"):
                benign += 1
                continue
            if cat == "unassigned":
                # No assignee to resolve — the dispatcher applies default_assignee
                # to unassigned dispatchable rows, so this is a DIFFERENT (handled)
                # class, NOT a silent-drop phantom. Tracked for visibility only.
                unassigned += 1
                continue
            phantoms.append(
                {
                    "board": slug,
                    "id": r["id"],
                    "assignee": r["assignee"],
                    "status": r["status"],
                    "priority": r["priority"],
                    "category": "phantom",
                    "reason": reason,
                }
            )
    summary = {
        "boards_scanned": len(boards),
        "total_dispatchable": total_dispatchable,
        "benign": benign,
        "unassigned": unassigned,
        "phantoms": len(phantoms),
        "errors": errors,
    }
    return phantoms, summary


def _board_rows_human_report(phantoms: list[dict], summary: dict) -> str:
    lines = [
        "validate_assignees: BOARD-ROW SWEEP",
        f"  boards scanned     : {summary['boards_scanned']}",
        f"  dispatchable rows  : {summary['total_dispatchable']}",
        f"  benign (profile/lane/proven-live): {summary['benign']}",
        f"  unassigned (handled by default_assignee): {summary['unassigned']}",
        f"  PHANTOM (silent-drop): {summary['phantoms']}",
    ]
    if phantoms:
        lines.append("  --- phantoms ---")
        for p in phantoms:
            lines.append(
                f"    [{p['board']}] {p['id']} assignee={p['assignee']} "
                f"status={p['status']} pri={p['priority']}"
            )
    if summary["errors"]:
        lines.append("  --- scan errors (non-fatal) ---")
        for e in summary["errors"]:
            lines.append(f"    {e['board']}: {e['error']}")
    if not phantoms:
        lines.append("  VERDICT: GREEN — no unroutable phantom assignees.")
    else:
        lines.append(
            "  VERDICT: PHANTOMS FOUND — these rows will be silently dropped by "
            "the dispatcher. Reassign to a real profile or confirm the lane."
        )
    return "\n".join(lines)


def board_rows_main(boards_dir: str, profiles_root: str, as_json: bool) -> int:
    phantoms, summary = scan_board_rows(boards_dir, profiles_root)
    if as_json:
        print(json.dumps({"clean": not phantoms, "phantoms": phantoms, "summary": summary}))
    else:
        print(_board_rows_human_report(phantoms, summary))
    return 1 if phantoms else 0


def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Validate kanban assignees are spawnable.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--json", metavar="PATH", help="Path to a JSON file with assignees")
    src.add_argument("--stdin", action="store_true", help="Read JSON from stdin")
    src.add_argument("--assignees", nargs="+", metavar="NAME", help="Inline assignee list")
    src.add_argument(
        "--board-rows",
        action="store_true",
        help="Scan all board DBs for dispatchable rows with unroutable assignees (t_97819b7d)",
    )
    # --board-rows options
    p.add_argument("--boards-dir", default=DEFAULT_BOARDS_DIR, help="Override boards root")
    p.add_argument("--profiles-root", default=DEFAULT_PROFILES_ROOT, help="Override profiles root")
    p.add_argument(
        "--board-json",
        action="store_true",
        help="With --board-rows, emit a machine-readable JSON report",
    )
    args = p.parse_args(argv)

    if args.board_rows:
        return board_rows_main(args.boards_dir, args.profiles_root, as_json=args.board_json)

    if args.assignees is not None:
        assignees = args.assignees
    else:
        raw = (
            sys.stdin.read()
            if args.stdin
            else open(os.path.expanduser(args.json), "r", encoding="utf-8").read()
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as ex:
            print(f"validate_assignees: JSON parse error: {ex}", file=sys.stderr)
            return 2
        assignees = _extract_assignees(data)

    rejected, terminal = validate_assignees(assignees)

    for warning in _catalog_drift_warnings():
        print(warning, file=sys.stderr)

    if terminal:
        print(
            "validate_assignees: note — recognized terminal lane assignee(s): "
            + ", ".join(sorted(set(terminal)))
            + " (valid in explicit multi-lane setups)"
        )

    if not rejected:
        print("validate_assignees: OK — all assignees are spawnable.")
        return 0

    print(
        "validate_assignees: REJECTED — decomposition assigns cards to non-spawnable "
        "profile(s):\n  - "
        + "\n  - ".join(rejected)
        + "\nThese profiles must never be auto-assigned (non-project / not dispatch-ready). "
        "Reassign to a real worker profile (e.g. builder, os-reviewer, the owning "
        "*-pm). See kanban-orchestrator skill 'Non-spawnable default-assignee gate'.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
