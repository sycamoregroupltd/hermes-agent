#!/usr/bin/env python3
"""Acceptance test for the fleet boards manifest (kanban t_911a916c).

Proves the acceptance criteria without mutating the live manifest:
  1. Adding a board to the manifest brings it into dispatch/GC/triage/sweep
     with NO edit to any lifecycle script.
  2. orchestrator-sync can never appear in any lifecycle list, even if some
     future edit sets its flags true (state=denied wins).
  3. ai-restaurant is declared dormant with a review date (documented silence).
  4. `fleet_boards.py --check` catches an on-disk board with open work that is
     absent from the manifest.

Run: python3 /home/frank/.hermes/scripts/test_fleet_boards_manifest.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path("/home/frank/.hermes/scripts")
sys.path.insert(0, str(SCRIPTS))
import fleet_boards  # noqa: E402

LIVE = Path("/home/frank/.hermes/kanban/boards-manifest.json")
FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(label)


def consumers(manifest_path: Path) -> dict[str, list[str]]:
    """Board lists as the real lifecycle scripts compute them, with the
    manifest overridden via HERMES_BOARDS_MANIFEST."""
    env = dict(os.environ, HERMES_BOARDS_MANIFEST=str(manifest_path))
    out: dict[str, list[str]] = {}

    # fleet-dispatch.sh derives its loop list from this exact command.
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "fleet_boards.py"), "dispatch", "--sep", " "],
        capture_output=True, text=True, env=env,
    )
    out["fleet-dispatch.sh"] = r.stdout.split()

    probes = {
        "kanban_gc_hygiene_bundle.py": (
            "/home/frank/.hermes/profiles/jarvis/scripts",
            "import kanban_gc_hygiene_bundle as m; print(' '.join(m.DEFAULT_BOARDS))",
        ),
        "needs_input_reporter.py": (
            "/home/frank/.hermes/profiles/jarvis-os-pm/scripts",
            "import needs_input_reporter as m; print(' '.join(m.SCAN_BOARDS))",
        ),
        "kanban_review_required_auto_router.py": (
            "/home/frank/.hermes/profiles/jarvis/scripts",
            "import kanban_review_required_auto_router as m; print(' '.join(m.DEFAULT_ROUTER_BOARDS))",
        ),
        "dgx_board_sweep_staleness.py": (
            str(SCRIPTS),
            "import dgx_board_sweep_staleness as m; print(' '.join(m.BOARDS))",
        ),
        "kanban_classify_failure_recent.py": (
            "/home/frank/.hermes/profiles/jarvis/scripts",
            "import kanban_classify_failure_recent as m; "
            "print(' '.join(m._manifest_boards('triage', [])))",
        ),
    }
    for name, (path, code) in probes.items():
        r = subprocess.run(
            [sys.executable, "-c", f"import sys; sys.path.insert(0, {path!r}); {code}"],
            capture_output=True, text=True, env=env,
        )
        out[name] = r.stdout.split() if r.returncode == 0 else [f"<ERR:{r.stderr.strip()[-120:]}>"]
    return out


def main() -> int:
    base = json.loads(LIVE.read_text())

    print("=== 1. baseline (live manifest) ===")
    live_lists = consumers(LIVE)
    for name, boards in live_lists.items():
        print(f"  {name}: {' '.join(boards)}")
        check(f"{name} excludes orchestrator-sync", "orchestrator-sync" not in boards)
        check(f"{name} excludes dormant ai-restaurant from work loops",
              name == "dgx_board_sweep_staleness.py" or "ai-restaurant" not in boards)

    print("\n=== 2. add a board via MANIFEST ONLY (no script edits) ===")
    with tempfile.TemporaryDirectory() as td:
        mp = Path(td) / "boards-manifest.json"
        mutated = json.loads(json.dumps(base))
        mutated["boards"]["ai-restaurant"] = {
            "state": "active", "owner": "jarvis-os-pm", "reviewer": "os-reviewer",
            "dispatch": True, "gc": True, "triage": True, "sweep": True,
        }
        # adversarial: try to sneak the coordination bus into the loops
        mutated["boards"]["orchestrator-sync"].update(
            {"dispatch": True, "gc": True, "triage": True, "sweep": True}
        )
        mp.write_text(json.dumps(mutated, indent=2))

        new_lists = consumers(mp)
        for name, boards in new_lists.items():
            print(f"  {name}: {' '.join(boards)}")
            check(f"{name} picked up ai-restaurant from manifest alone",
                  "ai-restaurant" in boards)
            check(f"{name} still refuses denied orchestrator-sync",
                  "orchestrator-sync" not in boards,
                  "state=denied must override flags")

    print("\n=== 3. ai-restaurant dormancy is documented ===")
    ar = base["boards"]["ai-restaurant"]
    check("ai-restaurant state=dormant", ar["state"] == "dormant")
    check("ai-restaurant has review_date", bool(ar.get("review_date")), ar.get("review_date", ""))
    check("ai-restaurant has reason", bool(ar.get("reason")))
    check("dormant board has no work flags",
          not (ar["dispatch"] or ar["gc"] or ar["triage"]))

    print("\n=== 4. --check catches an unregistered board with open work ===")
    with tempfile.TemporaryDirectory() as td:
        mp = Path(td) / "boards-manifest.json"
        stripped = json.loads(json.dumps(base))
        stripped["boards"].pop("ai-restaurant")
        mp.write_text(json.dumps(stripped, indent=2))
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "fleet_boards.py"), "--check", "--manifest", str(mp)],
            capture_output=True, text=True,
        )
        print("  " + r.stdout.strip().replace("\n", "\n  "))
        check("--check fails on unregistered board with open work",
              r.returncode == 1 and "ai-restaurant" in r.stdout)

    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "fleet_boards.py"), "--check"],
        capture_output=True, text=True,
    )
    check("live manifest passes --check", r.returncode == 0, r.stdout.strip())

    print(f"\n{'ALL PASS' if not FAILURES else 'FAILURES: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
