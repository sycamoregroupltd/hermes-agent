#!/usr/bin/env python3
"""Single source of truth reader for the fleet kanban boards manifest.

Every lifecycle loop (dispatch, GC, review-router, classify, PM triage, sweeps)
should call this instead of hardcoding board slugs. Adding a board to
~/.hermes/kanban/boards-manifest.json brings it into every loop with no script
edits.

Library use:
    from fleet_boards import boards_for, owner_for, manifest
    for b in boards_for("dispatch"): ...

CLI use (shell loops):
    python3 fleet_boards.py dispatch          -> newline-separated slugs
    python3 fleet_boards.py gc --sep ' '      -> space-separated
    python3 fleet_boards.py --owner jarvis-os -> owning PM profile (or empty)
    python3 fleet_boards.py --json            -> whole manifest
    python3 fleet_boards.py --check           -> validate invariants, rc=1 on fail
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

MANIFEST_PATH = Path(
    os.environ.get(
        "HERMES_BOARDS_MANIFEST",
        "/home/frank/.hermes/kanban/boards-manifest.json",
    )
)
BOARD_ROOT = Path(
    os.environ.get("HERMES_KANBAN_BOARD_ROOT", "/home/frank/.hermes/kanban/boards")
)
FLAGS = ("dispatch", "gc", "triage", "sweep")

# Fallback used only if the manifest is missing/corrupt. Deliberately the old
# hardcoded five so a manifest problem degrades to prior behaviour rather than
# dispatching nothing (or, worse, everything including orchestrator-sync).
FALLBACK = ("upero", "sycode-ai", "sycode-trading", "jarvis-os", "yorkstone-supplies")


class ManifestError(RuntimeError):
    pass


def manifest(path: Path | None = None) -> dict:
    p = path or MANIFEST_PATH
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read boards manifest {p}: {exc}") from exc
    if not isinstance(data.get("boards"), dict):
        raise ManifestError(f"boards manifest {p} has no 'boards' object")
    return data


def _boards(path: Path | None = None) -> dict:
    return manifest(path)["boards"]


def boards_for(flag: str, path: Path | None = None, strict: bool = False) -> list[str]:
    """Board slugs with `flag` enabled, in manifest order."""
    if flag not in FLAGS:
        raise ValueError(f"unknown flag {flag!r}; expected one of {FLAGS}")
    try:
        entries = _boards(path)
    except ManifestError:
        if strict:
            raise
        return list(FALLBACK) if flag != "sweep" else list(FALLBACK)
    out = []
    for slug, cfg in entries.items():
        if cfg.get("state") == "denied":
            continue  # denied boards are never enabled, regardless of flags
        if cfg.get(flag) is True:
            out.append(slug)
    return out


def owner_for(board: str, path: Path | None = None) -> str | None:
    try:
        return _boards(path).get(board, {}).get("owner")
    except ManifestError:
        return None


def reviewer_for(board: str, path: Path | None = None) -> str | None:
    """Review-router target profile for review-required handoffs on `board`."""
    try:
        return _boards(path).get(board, {}).get("reviewer")
    except ManifestError:
        return None


def reviewers(path: Path | None = None) -> dict[str, str]:
    """{board: reviewer} for every non-denied board that declares a reviewer."""
    try:
        entries = _boards(path)
    except ManifestError:
        return {}
    return {
        slug: cfg["reviewer"]
        for slug, cfg in entries.items()
        if cfg.get("state") != "denied" and cfg.get("reviewer")
    }


def state_for(board: str, path: Path | None = None) -> str:
    try:
        return _boards(path).get(board, {}).get("state", "unknown")
    except ManifestError:
        return "unknown"


def is_denied(board: str, path: Path | None = None) -> bool:
    return state_for(board, path) == "denied"


def check(path: Path | None = None) -> list[str]:
    """Validate manifest invariants and coverage. Returns list of problems."""
    problems: list[str] = []
    try:
        entries = _boards(path)
    except ManifestError as exc:
        return [str(exc)]

    for slug, cfg in entries.items():
        state = cfg.get("state")
        if state not in ("active", "dormant", "denied"):
            problems.append(f"{slug}: invalid state {state!r}")
        if state in ("dormant", "denied"):
            for flag in ("dispatch", "gc", "triage"):
                if cfg.get(flag):
                    problems.append(f"{slug}: state={state} but {flag}=true")
        if state == "dormant" and not cfg.get("review_date"):
            problems.append(f"{slug}: dormant without review_date")
        if state in ("dormant", "denied") and not cfg.get("reason"):
            problems.append(f"{slug}: state={state} without reason")
        if state == "active" and not cfg.get("owner"):
            problems.append(f"{slug}: active without owner")
        if cfg.get("triage") and not cfg.get("reviewer"):
            problems.append(f"{slug}: triage=true without reviewer (review-router cannot route it)")

    # Coverage: any on-disk board with open work must appear in the manifest.
    if BOARD_ROOT.is_dir():
        import sqlite3

        for d in sorted(BOARD_ROOT.iterdir()):
            if d.name.startswith(".") or ".bak" in d.name:
                continue  # backup / snapshot dirs are not live boards
            db = d / "kanban.db"
            if not db.is_file():
                continue
            if d.name in entries:
                continue
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                n = con.execute(
                    "select count(*) from tasks where status in "
                    "('todo','ready','running','blocked','triage')"
                ).fetchone()[0]
                con.close()
            except sqlite3.Error:
                continue
            if n:
                problems.append(
                    f"{d.name}: {n} open cards but absent from manifest "
                    "(no lifecycle owner — add it as active/dormant/denied)"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("flag", nargs="?", choices=FLAGS, help="list boards with this flag enabled")
    ap.add_argument("--sep", default="\n", help="separator for slug output (default newline)")
    ap.add_argument("--owner", metavar="BOARD", help="print the owning PM profile for BOARD")
    ap.add_argument("--state", metavar="BOARD", help="print the manifest state for BOARD")
    ap.add_argument("--json", action="store_true", help="dump the whole manifest")
    ap.add_argument("--check", action="store_true", help="validate invariants; rc=1 on problems")
    ap.add_argument("--manifest", default=None, help="override manifest path")
    args = ap.parse_args(argv)
    path = Path(args.manifest) if args.manifest else None

    if args.check:
        problems = check(path)
        for p in problems:
            print(f"FAIL {p}")
        if not problems:
            print("OK boards manifest valid; all boards with open work have a declared lifecycle state")
        return 1 if problems else 0
    if args.json:
        print(json.dumps(manifest(path), indent=2))
        return 0
    if args.owner:
        print(owner_for(args.owner, path) or "")
        return 0
    if args.state:
        print(state_for(args.state, path))
        return 0
    if args.flag:
        sys.stdout.write(args.sep.join(boards_for(args.flag, path)))
        if args.sep == "\n":
            sys.stdout.write("\n")
        else:
            sys.stdout.write("\n")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
